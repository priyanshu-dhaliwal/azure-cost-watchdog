"""
startup — queue trigger.

Brings a resource back up, restoring the capacity it had before it was stopped.

The gates here are intentionally minimal: the gates exist to prevent harm from
*stopping* things, and none of them apply to starting something. Only idempotency
applies. A prod resource stopped out-of-band must be startable by this tool.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import azure.functions as func
from azure.identity.aio import DefaultAzureCredential

from shared.actions import start
from shared.armclient import ArmClient
from shared.gates import Resource, evaluate
from shared.store import ActionStore

log = logging.getLogger("cost.startup")


async def main(msg: func.QueueMessage) -> None:
    payload = json.loads(msg.get_body().decode("utf-8"))
    commands = payload if isinstance(payload, list) else [payload]

    credential = DefaultAzureCredential()
    arm = ArmClient(credential)
    store = ActionStore(credential)
    acted = 0

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

            report = evaluate(gate_resource, "start", now=datetime.now(timezone.utc))
            if not report.should_act:
                log.info("%s already %s — nothing to start", gate_resource.name, live["state"])
                continue

            # Prefer the capacity recorded at shutdown time; fall back to whatever
            # the command carried, then to 1.
            capacity = live.get("capacity") or command.get("capacity") or 1
            try:
                result = await start(arm, resource_id, gate_resource.type, capacity)
                result["succeeded"] = True
                await arm.tag(resource_id, {
                    "lastAction": "started",
                    "lastActionAt": datetime.now(timezone.utc).isoformat(),
                    "lastActionBy": "cost-watchdog",
                })
                acted += 1
                log.info("started %s (capacity=%s)", gate_resource.name, capacity)
            except Exception as exc:  # noqa: BLE001
                result = {"operation": "failed", "succeeded": False, "detail": str(exc)}
                await store.record_action(command, result)
                log.exception("failed to start %s", gate_resource.name)
                raise

            await store.record_action(command, result)

        log.info("startup batch complete: acted=%d", acted)
    finally:
        await arm.close()
        await store.close()
        await credential.close()
