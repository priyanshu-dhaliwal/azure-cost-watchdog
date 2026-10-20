"""
report — timer, daily 07:00 IST.

Computes what the watchdog actually saved, using real retail prices, and emails
it. The method is stated in the output because a savings number without a method
is a guess with a currency symbol.

Method (also in docs/savings.md):

    saving(resource) = retail_hourly_price(resource) × stopped_hours(resource)

where `stopped_hours` is measured from the action log (the gap between a
`stop` action and the following `start`), not from the schedule. The schedule
says what *should* have happened; the log says what did.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import azure.functions as func
from azure.identity.aio import DefaultAzureCredential

from shared.pricing import PricingClient, saving
from shared.store import ActionStore, period_to_window

log = logging.getLogger("cost.report")

CURRENCY = os.environ.get("PRICE_CURRENCY", "INR")
PERIOD = os.environ.get("REPORT_PERIOD", "7d")


async def main(timer: func.TimerRequest, message: func.Out[str] | None = None) -> None:
    start, end = period_to_window(PERIOD)
    credential = DefaultAzureCredential()
    store = ActionStore(credential)
    pricing = PricingClient(currency=CURRENCY)

    try:
        actions = await store.actions_between(start, end)
        report = await build_report(actions, pricing, start, end)

        log.info("savings report: %s", json.dumps(report["totals"]))
        if message is not None:
            message.set(json.dumps(report))
    finally:
        await store.close()
        await credential.close()


async def build_report(actions: list[dict], pricing: PricingClient,
                       start: datetime, end: datetime) -> dict:
    """
    Join actions into stop/start pairs per resource and price the stopped hours.
    """
    by_resource: dict[str, list[dict]] = defaultdict(list)
    for action in actions:
        by_resource[action["resourceId"]].append(action)

    per_resource: list[dict] = []
    totals = {"stoppedHours": 0.0, "savedInr": 0.0, "resources": 0,
              "shutdowns": 0, "startups": 0, "unpricedResources": 0}

    for resource_id, events in by_resource.items():
        events.sort(key=lambda e: e["at"])
        stopped_hours = 0.0
        open_stop: datetime | None = None
        shutdowns = startups = 0

        for event in events:
            at = _parse(event["at"])
            if event["action"] == "stop":
                shutdowns += 1
                open_stop = at
            elif event["action"] == "start":
                startups += 1
                if open_stop is not None:
                    stopped_hours += (at - open_stop).total_seconds() / 3600
                    open_stop = None

        # Still stopped at the end of the window: count up to `end`.
        if open_stop is not None:
            stopped_hours += (end - open_stop).total_seconds() / 3600

        hourly = events[-1].get("hourlyInr")
        saved = None
        if hourly:
            saved = saving(float(hourly), stopped_hours)
        else:
            totals["unpricedResources"] += 1

        per_resource.append({
            "resourceId": resource_id,
            "resourceName": events[-1].get("resourceName"),
            "resourceType": events[-1].get("resourceType"),
            "stoppedHours": round(stopped_hours, 2),
            "hourlyInr": hourly,
            "savedInr": saved,
            "shutdowns": shutdowns,
            "startups": startups,
        })
        totals["stoppedHours"] += stopped_hours
        totals["shutdowns"] += shutdowns
        totals["startups"] += startups
        if saved:
            totals["savedInr"] += saved

    totals["resources"] = len(per_resource)
    totals["savedInr"] = round(totals["savedInr"], 2)
    totals["stoppedHours"] = round(totals["stoppedHours"], 2)

    per_resource.sort(key=lambda r: r["savedInr"] or 0, reverse=True)

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "currency": pricing.currency,
        "method": "retail hourly price x measured stopped hours (from the action log)",
        "totals": totals,
        "resources": per_resource,
        "top": per_resource[:10],
    }


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def render_text(report: dict) -> str:
    """Plain-text summary for email. Numbers first, method last."""
    t = report["totals"]
    lines = [
        f"Cost watchdog — {report['period']['start'][:10]} to {report['period']['end'][:10]}",
        "",
        f"  Saved (retail):     {report['currency']} {t['savedInr']:,.2f}",
        f"  Stopped hours:      {t['stoppedHours']:,.1f}",
        f"  Resources affected: {t['resources']}",
        f"  Shutdowns/startups: {t['shutdowns']} / {t['startups']}",
    ]
    if t["unpricedResources"]:
        lines.append(f"  Unpriced (no SKU):  {t['unpricedResources']}")
    if report["top"]:
        lines += ["", "  Top savers:"]
        for r in report["top"]:
            saved = f"{r['savedInr']:,.2f}" if r["savedInr"] is not None else "n/a"
            lines.append(f"    {r['resourceName']:<32} {r['stoppedHours']:>7.1f}h  {saved:>12}")
    lines += ["", f"  Method: {report['method']}"]
    return "\n".join(lines)
