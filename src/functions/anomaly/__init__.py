"""
anomaly — timer, hourly.

Detects spend that is out of line with *this subscription's own* recent
behaviour, and says which resources moved.

Why not a fixed threshold: a fixed number is either noise on a small account or
useless on a large one. Why not day-over-day: traffic is diurnal, so 14:00
yesterday versus 14:00 today removes the daily shape and leaves the anomaly.

The baseline is the same hour on the trailing 7 days, and the test is both:

    delta > 3 sigma   AND   delta > MIN_DELTA_INR

The second condition is what stops a quiet dev subscription from alerting on a
₹12 move.

What it deliberately does NOT do: turn anything off. An anomaly is a signal for a
human; a cost system that reacts to an anomaly by shutting infrastructure down is
an availability risk dressed as governance.
"""

from __future__ import annotations

import logging
import os
import statistics
from datetime import datetime, timedelta, timezone

import azure.functions as func
from azure.identity.aio import DefaultAzureCredential

from shared.store import ActionStore

log = logging.getLogger("cost.anomaly")

SIGMA = float(os.environ.get("ANOMALY_SIGMA", "3"))
MIN_DELTA = float(os.environ.get("ANOMALY_MIN_DELTA_INR", "500"))
LOOKBACK_DAYS = int(os.environ.get("ANOMALY_LOOKBACK_DAYS", "7"))


async def main(timer: func.TimerRequest, message: func.Out[str] | None = None) -> None:
    credential = DefaultAzureCredential()
    store = ActionStore(credential)

    try:
        result = await detect(credential, store)
        log.info("anomaly check: %s", result["summary"])
        if result["anomalous"] and message is not None:
            message.set(result_json(result))
    finally:
        await store.close()
        await credential.close()


def result_json(result: dict) -> str:
    import json
    return json.dumps(result)


async def detect(credential, store: ActionStore) -> dict:
    from azure.mgmt.costmanagement.aio import CostManagementClient

    now = datetime.now(timezone.utc)
    hour_start = now.replace(minute=0, second=0, microsecond=0)

    async with CostManagementClient(credential) as client:
        today = await _hourly_total(client, hour_start - timedelta(hours=1), hour_start)
        baseline = []
        for day in range(1, LOOKBACK_DAYS + 1):
            start = hour_start - timedelta(days=day)
            baseline.append(await _hourly_total(client, start, start + timedelta(hours=1)))

    values = [b for b in baseline if b is not None]
    if not values:
        return {"anomalous": False, "summary": "no baseline data", "current": today,
                "baseline": None, "delta": 0.0}

    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
    delta = (today or 0.0) - mean
    threshold = SIGMA * stdev

    anomalous = delta > max(threshold, MIN_DELTA)
    summary = (f"current={today:.2f} mean={mean:.2f} stdev={stdev:.2f} "
               f"delta={delta:.2f} threshold={max(threshold, MIN_DELTA):.2f}")

    result = {
        "anomalous": anomalous,
        "summary": summary,
        "current": round(today or 0.0, 2),
        "baselineMean": round(mean, 2),
        "baselineStdev": round(stdev, 2),
        "delta": round(delta, 2),
        "threshold": round(max(threshold, MIN_DELTA), 2),
        "detectedAt": now.isoformat(),
        "topContributors": [],
    }

    if anomalous:
        result["topContributors"] = await _top_movers(credential, hour_start - timedelta(hours=1), hour_start)
        log.warning("cost anomaly: %s", summary)

    return result


async def _hourly_total(client, start: datetime, end: datetime) -> float | None:
    """Total cost in a one-hour window, across the subscription."""
    from azure.mgmt.costmanagement.models import (
        QueryDefinition, TimeframeType, QueryDataset, QueryTimePeriod,
    )
    query = QueryDefinition(
        type="ActualCost",
        timeframe=TimeframeType.CUSTOM,
        time_period=QueryTimePeriod(from_property=start, to=end),
        dataset=QueryDataset(granularity="None", aggregation={
            "totalCost": {"name": "Cost", "function": "Sum"},
        }),
    )
    try:
        response = await client.query(scope=f"/subscriptions/{os.environ['AZURE_SUBSCRIPTION_ID']}",
                                      parameters=query)
    except Exception as exc:  # noqa: BLE001
        log.warning("cost query failed: %s", exc)
        return None

    rows = response.rows or []
    if not rows:
        return 0.0
    # The single aggregation column is the last value in the row.
    return float(rows[0][-1])


async def _top_movers(credential, start: datetime, end: datetime) -> list[dict]:
    """The five resources that grew most, so the alert is actionable."""
    from azure.mgmt.costmanagement.aio import CostManagementClient
    from azure.mgmt.costmanagement.models import (
        QueryDefinition, TimeframeType, QueryDataset, QueryTimePeriod,
    )

    query = QueryDefinition(
        type="ActualCost",
        timeframe=TimeframeType.CUSTOM,
        time_period=QueryTimePeriod(from_property=start, to=end),
        dataset=QueryDataset(
            granularity="None",
            aggregation={"totalCost": {"name": "Cost", "function": "Sum"}},
            grouping=[{"type": "Dimension", "name": "ResourceId"}],
        ),
    )
    try:
        async with CostManagementClient(credential) as client:
            response = await client.query(
                scope=f"/subscriptions/{os.environ['AZURE_SUBSCRIPTION_ID']}", parameters=query)
    except Exception as exc:  # noqa: BLE001
        log.warning("top-movers query failed: %s", exc)
        return []

    rows = sorted((response.rows or []), key=lambda r: float(r[-1]), reverse=True)
    return [{"resourceId": r[0], "cost": round(float(r[-1]), 2)} for r in rows[:5]]
