"""
shutdown — queue trigger.

Re-evaluates the gates against the LIVE state before acting. This is not
redundant with the scheduler: between "decided" and "executed" a developer can
add a keep-alive, a release can promote the resource, or someone can tag it
environment=prod. The gate check is cheap; being wrong is not.

On success it writes the action to Cosmos and stamps `lastAction` on the resource
so the next tick's idempotency gate sees it.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import azure.functions as func
from azure.identity.aio import DefaultAzureCredential

from shared.actions import stop
from shared.armclient import ArmClient
from shared.gates import Resource, evaluate
from shared.store import ActionStore

log = logging.getLogger("cost.shutdown")

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


async def main(msg: func.QueueMessage) -> None:
    payload = json.loads(msg.get_body().decode("utf-8"))
    # The scheduler batches commands; accept both a single object and a list.
    commands = payload if isinstance(payload, list) else [payload]

    credential = DefaultAzureCredential()
    arm = ArmClient(credential)
    store = ActionStore(credential)
    acted = 0
    refused = 0

    try:
        for command in commands:
            resource_id = command["resourceId"]
            try:
                live = await arm.get(resource_id)
            except Exception as exc:  # noqa: BLE001
                log.error("could not read %s: %s — skipping", resource_id, exc)
                continue

            gate_resource = Resource(
                id=resource_id,
                name=live["name"],
                type=live["type"],
                resource_group=live["resourceGroup"],
                tags=live.get("tags") or {},
                current_state=live["state"],
                current_capacity=live.get("capacity"),
            )

            report = evaluate(gate_resource, "stop", now=datetime.now(timezone.utc))
            if not report.should_act:
                refused += 1
                log.info("refused to stop %s: %s — %s",
                         gate_resource.name, report.blocked_by, report.reason)
                await store.record_action(command, {
                    "operation": "none", "succeeded": True,
                    "detail": f"gate refused: {report.blocked_by}: {report.reason}",
                })
                continue

            if DRY_RUN:
                log.info("DRY RUN: would deallocate %s", gate_resource.name)
                continue

            try:
                result = await stop(arm, resource_id, gate_resource.type)
                result["succeeded"] = True
                # Stamp the resource so the next tick's idempotency gate is cheap
                # and the portal shows what the watchdog did.
                await arm.tag(resource_id, {
                    "lastAction": "stopped",
                    "lastActionAt": datetime.now(timezone.utc).isoformat(),
                    "lastActionBy": "cost-watchdog",
                })
                acted += 1
                log.info("stopped %s via %s", gate_resource.name, result.get("operation"))
            except Exception as exc:  # noqa: BLE001
                result = {"operation": "failed", "succeeded": False, "detail": str(exc)}
                log.exception("failed to stop %s", gate_resource.name)
                # Re-raise so the queue retries with backoff and eventually
                # dead-letters — a failed shutdown must not vanish.
                await store.record_action(command, result)
                raise

            await store.record_action(command, result)

        log.info("shutdown batch complete: acted=%d refused=%d", acted, refused)
    finally:
        await arm.close()
        await store.close()
        await credential.close()
