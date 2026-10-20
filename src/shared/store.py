"""
Cosmos-backed action store: decisions, actions, keep-alives, policies.

Two containers, one partition key (`/environment`), because every query the
report runs is scoped to an environment and a time window.

The store is deliberately append-only for decisions: "why did the watchdog not
stop this" is answered by reading the decision log, and a log you can rewrite is
not evidence.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("cost.store")


class ActionStore:
    def __init__(self, credential, endpoint: str | None = None, database: str | None = None) -> None:
        self._credential = credential
        self._endpoint = endpoint or os.environ["COSMOS_ENDPOINT"]
        self._database = database or os.environ.get("COSMOS_DATABASE", "costwatchdog")
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from azure.cosmos.aio import CosmosClient
            self._client = CosmosClient(self._endpoint, credential=self._credential)
        return self._client

    def _container(self, name: str):
        return self.client.get_database_client(self._database).get_container_client(name)

    # ------------------------------------------------------------- decisions

    async def record_decision(self, resource, action: str, decision: str, reason: str) -> None:
        """Every tick, for every candidate. This is the audit trail."""
        doc = {
            "id": str(uuid.uuid4()),
            "environment": resource.tags.get("environment", "unknown").lower(),
            "docType": "decision",
            "resourceId": resource.id,
            "resourceName": resource.name,
            "resourceType": resource.type,
            "action": action,
            "decision": decision,
            "reason": reason,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await self._container("decisions").upsert_item(doc)
        except Exception as exc:  # noqa: BLE001
            # A logging failure must never stop a shutdown. Loud, not fatal.
            log.error("could not record decision for %s: %s", resource.name, exc)

    # --------------------------------------------------------------- actions

    async def record_action(self, command: dict, result: dict) -> None:
        doc = {
            "id": str(uuid.uuid4()),
            "environment": (command.get("tags") or {}).get("environment", "unknown").lower(),
            "docType": "action",
            "resourceId": command["resourceId"],
            "resourceType": command["resourceType"],
            "resourceName": command["resourceName"],
            "action": command["action"],
            "operation": result.get("operation"),
            "capacity": command.get("capacity"),
            "hourlyInr": command.get("estimatedHourlyInr"),
            "at": datetime.now(timezone.utc).isoformat(),
            "succeeded": result.get("succeeded", True),
            "detail": result.get("detail", ""),
        }
        await self._container("actions").upsert_item(doc)

    async def actions_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        query = ("SELECT * FROM c WHERE c.docType = 'action' "
                 "AND c.at >= @start AND c.at < @end")
        params = [{"name": "@start", "value": start.isoformat()},
                  {"name": "@end", "value": end.isoformat()}]
        return [d async for d in self._container("actions").query_items(query, parameters=params)]

    # ------------------------------------------------------------ keep-alive

    async def set_keepalive(self, resource_id: str, environment: str, until: datetime,
                            requested_by: str) -> None:
        doc = {
            "id": f"keepalive-{resource_id.split('/')[-1]}",
            "environment": environment.lower(),
            "docType": "keepAlive",
            "resourceId": resource_id,
            "until": until.isoformat(),
            "requestedBy": requested_by,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        await self._container("policies").upsert_item(doc)

    async def get_keepalive(self, resource_id: str, environment: str) -> dict | None:
        try:
            return await self._container("policies").read_item(
                item=f"keepalive-{resource_id.split('/')[-1]}", partition_key=environment.lower())
        except Exception:
            return None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()


def period_to_window(period: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Parse '7d' / '30d' / '24h' into a [start, end) window."""
    now = now or datetime.now(timezone.utc)
    period = period.strip().lower()
    try:
        n = int(period[:-1])
    except (ValueError, IndexError) as exc:
        raise ValueError(f"bad period '{period}', expected e.g. 7d or 24h") from exc
    unit = period[-1]
    if unit == "d":
        return now - timedelta(days=n), now
    if unit == "h":
        return now - timedelta(hours=n), now
    raise ValueError(f"bad period unit '{unit}', expected d or h")
