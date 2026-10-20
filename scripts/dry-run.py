#!/usr/bin/env python3
"""
dry-run — show exactly what the watchdog would do, right now.

    ./scripts/dry-run.py --env dev
    ./scripts/dry-run.py --env dev --resource /subscriptions/.../vm/x

Read-only. This is the script you run before trusting the system, and the one you
attach to a PR that changes a gate.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from azure.identity.aio import DefaultAzureCredential  # noqa: E402

from shared.gates import Resource, evaluate  # noqa: E402
from shared.resourcegraph import list_candidates  # noqa: E402
from shared.schedule import ScheduleError, local_now, parse  # noqa: E402

COLUMNS = ("RESOURCE", "TYPE", "NOW", "ACTION", "DECISION", "REASON")


def _tag(tags: dict[str, str], key: str) -> str:
    for k, v in (tags or {}).items():
        if k.lower() == key.lower():
            return (v or "").strip()
    return ""


async def run(env_filter: str | None, resource_filter: str | None) -> int:
    tz = os.environ.get("POLICY_TIMEZONE", "Asia/Kolkata")
    now_utc = datetime.now(timezone.utc)
    local = local_now(tz, now_utc)

    credential = DefaultAzureCredential()
    try:
        candidates = await list_candidates(credential)
    finally:
        await credential.close()

    rows = []
    for res in candidates:
        if env_filter and _tag(res.tags, "environment").lower() != env_filter.lower():
            continue
        if resource_filter and resource_filter not in res.id:
            continue

        schedule_tag = _tag(res.tags, "autoShutdownSchedule") or "mon-fri 09:00-19:00"
        try:
            schedule = parse(schedule_tag)
        except ScheduleError as exc:
            rows.append((res.name, res.type.split("/")[-1], res.state, "-", "ERROR",
                         f"bad schedule: {exc}"))
            continue

        should_run = schedule.is_running_at(local)
        action = "start" if should_run else "stop"
        report = evaluate(Resource(res.id, res.name, res.type, res.resource_group,
                                   res.tags, res.state, res.capacity), action, now=now_utc)

        decision = "PROCEED" if report.should_act else report.decision.value.upper()
        reason = "all gates passed" if report.should_act else f"{report.blocked_by.value}: {report.reason}"
        rows.append((res.name, res.type.split("/")[-1], res.state, action.upper(), decision, reason))

    widths = [max(len(str(r[i])) for r in rows + [COLUMNS]) for i in range(len(COLUMNS))]
    line = "  ".join(h.ljust(w) for h, w in zip(COLUMNS, widths))
    print(f"\nLocal time: {local.isoformat()} ({tz})\n")
    print(line)
    print("-" * len(line))
    for row in sorted(rows, key=lambda r: (r[4] != "PROCEED", r[0])):
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)))

    acted = sum(1 for r in rows if r[4] == "PROCEED")
    print(f"\n{len(rows)} candidate(s), {acted} would be acted on.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", help="filter by environment tag (dev/staging/...)")
    ap.add_argument("--resource", help="substring of a resource id")
    args = ap.parse_args()
    return asyncio.run(run(args.env, args.resource))


if __name__ == "__main__":
    sys.exit(main())
