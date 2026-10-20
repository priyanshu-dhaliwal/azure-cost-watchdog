"""
scheduler — timer, every 15 minutes.

Decides, it does not act. For every resource carrying `autoShutdown=true` it:

  1. parses the schedule tag and asks "should this be running now?"
  2. builds the command (stop or start)
  3. evaluates the five gates
  4. enqueues only the commands that passed, and logs every decision

Putting the decision in one place and the execution in another means a bug in
the scheduler cannot stop anything directly: the worst it can do is enqueue a
command that the shutdown worker's gates will refuse.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import azure.functions as func
from azure.identity.aio import DefaultAzureCredential

from shared.gates import Resource, evaluate
from shared.resourcegraph import list_candidates
from shared.schedule import ScheduleError, local_now, parse
from shared.store import ActionStore

log = logging.getLogger("cost.scheduler")

TZ = os.environ.get("POLICY_TIMEZONE", "Asia/Kolkata")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"


async def main(timer: func.TimerRequest, stopQueue: func.Out[str],
               startQueue: func.Out[str]) -> None:
    if timer.past_due:
        log.warning("scheduler past due — decisions may be late, not wrong")

    now_utc = datetime.now(timezone.utc)
    local = local_now(TZ, now_utc)
    log.info("scheduler tick at %s local (%s UTC)", local.isoformat(), now_utc.isoformat())

    credential = DefaultAzureCredential()
    store = ActionStore(credential)
    stop_commands: list[str] = []
    start_commands: list[str] = []
    summary = {"stop": 0, "start": 0, "skip": 0, "abort": 0, "errors": 0}

    try:
        candidates = await list_candidates(credential)
        log.info("found %d candidate resource(s) with autoShutdown=true", len(candidates))

        for res in candidates:
            schedule_tag = _tag(res.tags, "autoShutdownSchedule") or "mon-fri 09:00-19:00"
            try:
                schedule = parse(schedule_tag)
            except ScheduleError as exc:
                # A malformed schedule is a configuration bug. Do nothing and
                # record it — never guess what the author meant.
                summary["errors"] += 1
                await store.record_decision(res, "none", "error", f"bad schedule '{schedule_tag}': {exc}")
                log.error("resource %s has an invalid schedule tag: %s", res.name, exc)
                continue

            should_run = schedule.is_running_at(local)
            action = "start" if should_run else "stop"

            # A stopped resource outside its window is the common case: skip it
            # before even running the gates, so the log stays readable.
            report = evaluate(_to_gate_resource(res), action, now=now_utc)

            if report.should_act:
                summary[action] += 1
                command = {
                    "action": action,
                    "resourceId": res.id,
                    "resourceType": res.type,
                    "resourceName": res.name,
                    "resourceGroup": res.resource_group,
                    "schedule": schedule_tag,
                    "decidedAt": now_utc.isoformat(),
                    "reason": f"schedule '{schedule_tag}' says {'running' if should_run else 'outside window'}",
                    "capacity": res.capacity,
                    "estimatedHourlyInr": res.estimated_hourly_inr,
                }
                if DRY_RUN:
                    log.info("DRY RUN: would %s %s", action, res.name)
                elif action == "stop":
                    stop_commands.append(json.dumps(command))
                else:
                    start_commands.append(json.dumps(command))
                await store.record_decision(res, action, "queued", command["reason"])
            else:
                summary["skip" if report.decision.value == "skip" else "abort"] += 1
                await store.record_decision(res, action, report.decision.value,
                                            f"{report.blocked_by.value}: {report.reason}")
                log.debug("%s -> %s (%s)", res.name, report.decision.value, report.reason)

        # Two queues, not one: a stop command that fails to parse can never be
        # accidentally executed as a start, and the two paths have different
        # retry semantics.
        if stop_commands:
            stopQueue.set(json.dumps(stop_commands))
        if start_commands:
            startQueue.set(json.dumps(start_commands))

        log.info("scheduler summary: %s (dry_run=%s)", summary, DRY_RUN)
    finally:
        await store.close()
        await credential.close()


def _tag(tags: dict[str, str], key: str) -> str:
    for k, v in (tags or {}).items():
        if k.lower() == key.lower():
            return (v or "").strip()
    return ""


def _to_gate_resource(res) -> Resource:
    return Resource(
        id=res.id,
        name=res.name,
        type=res.type,
        resource_group=res.resource_group,
        tags=res.tags or {},
        current_state=res.state,
        current_capacity=res.capacity,
    )
