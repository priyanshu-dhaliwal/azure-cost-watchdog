"""
Resource discovery via Azure Resource Graph.

Resource Graph is used rather than per-resource-type SDK calls because it answers
"give me everything tagged autoShutdown=true across all subscriptions I can see"
in one query. The alternative is N API calls per type per subscription, which
takes minutes and gets throttled.

The query is deliberately narrow: only resources that opted in are returned, so
the scheduler never even sees production infrastructure.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("cost.resourcegraph")

# Types we know how to stop/start. Anything else is returned so the scheduler can
# log "skipped: no handler" rather than silently ignoring it.
CANDIDATE_TYPES = (
    "microsoft.compute/virtualmachines",
    "microsoft.containerservice/managedclusters",
    "microsoft.web/sites",
    "microsoft.sql/servers/databases",
    "microsoft.databricks/workspaces",
)

QUERY = """
Resources
| where tags['autoShutdown'] =~ 'true'
| where type in~ ({types})
| project id, name, type, resourceGroup, subscriptionId, location, tags,
          properties
| order by name asc
""".format(types=", ".join(f"'{t}'" for t in CANDIDATE_TYPES))


@dataclass
class Candidate:
    id: str
    name: str
    type: str
    resource_group: str
    subscription_id: str
    location: str
    tags: dict[str, str] = field(default_factory=dict)
    state: str = "unknown"
    capacity: int | None = None
    sku: str | None = None
    estimated_hourly_inr: float | None = None


async def list_candidates(credential, subscriptions: list[str] | None = None) -> list[Candidate]:
    """Query Resource Graph for opt-in resources and normalise the results."""
    from azure.mgmt.resourcegraph.aio import ResourceGraphClient
    from azure.mgmt.resourcegraph.models import QueryRequest

    subs = subscriptions or [s for s in os.environ.get("WATCHDOG_SUBSCRIPTIONS", "").split(",") if s]
    if not subs:
        # Fall back to every subscription the identity can read.
        from azure.mgmt.subscription.aio import SubscriptionClient
        async with SubscriptionClient(credential) as sc:
            subs = [s.subscription_id async for s in sc.subscriptions.list()]

    async with ResourceGraphClient(credential) as client:
        request = QueryRequest(subscriptions=subs, query=QUERY, options={"resultFormat": "objectArray"})
        response = await client.resources(request)

    out: list[Candidate] = []
    for row in response.data or []:
        props = row.get("properties") or {}
        state = _extract_state(props)
        out.append(Candidate(
            id=row["id"],
            name=row["name"],
            type=row["type"],
            resource_group=row["resourceGroup"],
            subscription_id=row["subscriptionId"],
            location=row.get("location", ""),
            tags=row.get("tags") or {},
            state=state,
            capacity=_extract_capacity(props),
            sku=_extract_sku(props),
        ))
    return out


def _extract_state(props: dict) -> str:
    # VM state lives in instanceView at runtime, but Resource Graph exposes the
    # last known provisioningState; the shutdown worker re-reads the live state
    # before acting, so this is only a hint.
    for key in ("provisioningState", "state", "powerState"):
        value = props.get(key)
        if isinstance(value, str) and value:
            return value.lower()
    return "unknown"


def _extract_capacity(props: dict) -> int | None:
    pools = props.get("agentPoolProfiles") or []
    for pool in pools:
        if pool.get("name") == "default":
            return int(pool.get("count") or 1)
    return props.get("maximumNumberOfWorkers")


def _extract_sku(props: dict) -> str | None:
    sku = props.get("hardwareProfile") or {}
    return sku.get("vmSize") or (props.get("sku") or {}).get("name")
