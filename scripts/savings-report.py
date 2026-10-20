#!/usr/bin/env python3
"""
savings-report — on-demand savings report from the CLI.

    ./scripts/savings-report.py --period 30d
    ./scripts/savings-report.py --period 7d --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from azure.identity.aio import DefaultAzureCredential  # noqa: E402

from functions.report import build_report, render_text  # noqa: E402
from shared.pricing import PricingClient  # noqa: E402
from shared.store import ActionStore, period_to_window  # noqa: E402


async def run(period: str, as_json: bool, currency: str) -> int:
    start, end = period_to_window(period)
    credential = DefaultAzureCredential()
    store = ActionStore(credential)
    pricing = PricingClient(currency=currency)
    try:
        actions = await store.actions_between(start, end)
        report = await build_report(actions, pricing, start, end)
    finally:
        await store.close()
        await credential.close()

    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="7d", help="e.g. 24h, 7d, 30d")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--currency", default=os.environ.get("PRICE_CURRENCY", "INR"))
    args = ap.parse_args()
    return asyncio.run(run(args.period, args.json, args.currency))


if __name__ == "__main__":
    sys.exit(main())
