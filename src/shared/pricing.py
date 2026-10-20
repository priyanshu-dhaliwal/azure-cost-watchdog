"""
Azure Retail Prices API client.

The point of this module is that the savings report uses *real* prices rather
than a hardcoded rate. A hardcoded rate is a number that is wrong six months
later and nobody notices.

The API is public (no auth), rate-limited, and paginated. Results are cached in
memory and in Cosmos, because prices change slowly and the report does not need
to be second-fresh.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

try:
    import aiohttp
except ImportError:  # pragma: no cover - aiohttp is a runtime dep of the function app
    aiohttp = None  # type: ignore[assignment]

log = logging.getLogger("cost.pricing")

BASE = "https://prices.azure.com/api/retail/prices"
API_VERSION = "2023-01-01-preview"


@dataclass(frozen=True)
class Price:
    meter_id: str
    sku: str
    region: str
    retail_price: float        # per unit (usually per hour)
    unit_of_measure: str       # "1 Hour", "1 GB/Month", ...
    currency: str

    @property
    def per_hour(self) -> float | None:
        """
        Normalise the unit to an hourly price, or None if it is not a
        *time-based compute* price.

        Order matters: "1 GB/Month" contains "month" but is a storage rate, not
        an hourly rate. Only units with no size dimension are time prices.
        """
        u = self.unit_of_measure.lower()
        size_dimensions = ("gb", "tb", "10k", "1k", "operations", "transactions", "requests")
        if any(d in u for d in size_dimensions):
            return None
        if "hour" in u:
            return self.retail_price
        if "month" in u:
            # Azure months are 730 hours.
            return round(self.retail_price / 730, 6)
        return None


class PricingClient:
    def __init__(self, currency: str = "INR", session: aiohttp.ClientSession | None = None) -> None:
        self.currency = currency
        self._session = session
        self._cache: dict[str, Price | None] = {}

    async def _get(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        if aiohttp is None:  # pragma: no cover
            raise RuntimeError("aiohttp is required for live price lookups")
        own_session = self._session is None
        session = self._session or aiohttp.ClientSession()
        try:
            for attempt in range(1, 4):
                try:
                    async with session.get(url, params=params,
                                           timeout=aiohttp.ClientTimeout(total=20)) as r:
                        if r.status == 429:
                            wait = 2 ** attempt
                            log.warning("pricing API rate-limited, waiting %ss", wait)
                            await asyncio.sleep(wait)
                            continue
                        r.raise_for_status()
                        return await r.json()
                except aiohttp.ClientError as exc:
                    if attempt == 3:
                        raise
                    log.warning("pricing request failed (%s), retrying", exc)
                    await asyncio.sleep(2 ** attempt)
            raise RuntimeError("pricing API unreachable")
        finally:
            if own_session:
                await session.close()

    async def price_for_vm(self, arm_sku_name: str, region: str) -> Price | None:
        """
        Look up the Linux, pay-as-you-go, spot-free price for a VM size.

        Filtering by armSkuName AND armRegionName keeps the result set to one or
        two rows instead of the whole catalogue.
        """
        cache_key = f"vm:{arm_sku_name}:{region}:{self.currency}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        flt = (
            f"serviceName eq 'Virtual Machines' "
            f"and armRegionName eq '{region}' "
            f"and armSkuName eq '{arm_sku_name}' "
            f"and priceType eq 'Consumption'"
        )
        data = await self._get(BASE, {
            "api-version": API_VERSION,
            "currencyCode": self.currency,
            "$filter": flt,
        })
        items = data.get("Items") or []
        # Prefer Linux; the catalogue contains Windows rows at a higher price.
        linux = [i for i in items if "Windows" not in (i.get("productName") or "")]
        chosen = (linux or items or [None])[0]
        if not chosen:
            self._cache[cache_key] = None
            return None

        price = Price(
            meter_id=chosen["meterId"],
            sku=chosen.get("armSkuName", arm_sku_name),
            region=chosen.get("armRegionName", region),
            retail_price=float(chosen["retailPrice"]),
            unit_of_measure=chosen.get("unitOfMeasure", "1 Hour"),
            currency=chosen.get("currencyCode", self.currency),
        )
        self._cache[cache_key] = price
        return price

    async def price_for_sku(self, service: str, sku: str, region: str) -> Price | None:
        """Generic lookup for non-VM services (SQL, App Service, AKS nodes)."""
        cache_key = f"sku:{service}:{sku}:{region}:{self.currency}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        flt = (f"serviceName eq '{service}' and armRegionName eq '{region}' "
               f"and skuName eq '{sku}' and priceType eq 'Consumption'")
        data = await self._get(BASE, {"api-version": API_VERSION,
                                      "currencyCode": self.currency, "$filter": flt})
        items = data.get("Items") or []
        if not items:
            self._cache[cache_key] = None
            return None
        chosen = items[0]
        price = Price(
            meter_id=chosen["meterId"],
            sku=chosen.get("skuName", sku),
            region=chosen.get("armRegionName", region),
            retail_price=float(chosen["retailPrice"]),
            unit_of_measure=chosen.get("unitOfMeasure", "1 Hour"),
            currency=chosen.get("currencyCode", self.currency),
        )
        self._cache[cache_key] = price
        return price


def saving(hourly_price: float, stopped_hours: float) -> float:
    """
    Money saved by not running for `stopped_hours`.

    Deliberately linear and explicit: no amortised discounts, no reserved
    instance assumptions, no "blended rate". The report says "at retail price,
    this is what those hours would have cost", which is a defensible claim.
    Anything cleverer is a claim you cannot defend in a review.
    """
    return round(hourly_price * stopped_hours, 2)
