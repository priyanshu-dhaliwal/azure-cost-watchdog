"""
A thin ARM client.

Deliberately small: get / post / patch / tag, with the live state normalised into
one place. The rest of the codebase should never need to know whether a VM's
power state lives in `properties.instanceView.statuses` or somewhere else.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

log = logging.getLogger("cost.arm")

ARM = "https://management.azure.com"
API_VERSIONS = {
    "microsoft.compute/virtualmachines": "2024-03-01",
    "microsoft.containerservice/managedclusters": "2024-02-01",
    "microsoft.web/sites": "2023-12-01",
    "microsoft.sql/servers/databases": "2023-08-01-preview",
    "microsoft.databricks/workspaces": "2024-05-01",
}
DEFAULT_API = "2021-04-01"


class ArmClient:
    def __init__(self, credential, session: aiohttp.ClientSession | None = None) -> None:
        self._credential = credential
        self._session = session
        self._token: str | None = None

    async def _auth_headers(self) -> dict[str, str]:
        if self._token is None:
            token = await self._credential.get_token("https://management.azure.com/.default")
            self._token = token.token
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def _version(self, resource_type: str) -> str:
        return API_VERSIONS.get(resource_type.lower(), DEFAULT_API)

    async def _request(self, method: str, resource_id: str, resource_type: str,
                       action: str | None = None, body: dict | None = None) -> dict[str, Any]:
        url = f"{ARM}{resource_id}"
        if action:
            url += f"/{action}"
        url += f"?api-version={self._version(resource_type)}"

        own = self._session is None
        session = self._session or aiohttp.ClientSession()
        try:
            async with session.request(method, url, headers=await self._auth_headers(),
                                       json=body, timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status >= 400:
                    text = await r.text()
                    raise RuntimeError(f"ARM {method} {resource_id} -> {r.status}: {text[:400]}")
                if r.status == 204 or not (await r.read()):
                    return {}
                try:
                    return await r.json()
                except Exception:  # noqa: BLE001 - 202 Accepted returns no body
                    return {}
        finally:
            if own:
                await session.close()

    async def get(self, resource_id: str, resource_type: str | None = None) -> dict[str, Any]:
        """Read a resource and normalise its live state."""
        rtype = resource_type or _type_from_id(resource_id)
        raw = await self._request("GET", resource_id, rtype)

        props = raw.get("properties") or {}
        state = _normalise_state(raw, props)
        return {
            "id": raw.get("id", resource_id),
            "name": raw.get("name", resource_id.split("/")[-1]),
            "type": raw.get("type", rtype),
            "resourceGroup": _rg_from_id(resource_id),
            "tags": raw.get("tags") or {},
            "state": state,
            "capacity": _capacity(props),
            "raw": raw,
        }

    async def post(self, resource_id: str, action: str,
                   resource_type: str | None = None, body: dict | None = None) -> dict[str, Any]:
        return await self._request("POST", resource_id, resource_type or _type_from_id(resource_id),
                                   action=action, body=body)

    async def patch(self, resource_id: str, body: dict[str, Any],
                    resource_type: str | None = None) -> dict[str, Any]:
        return await self._request("PATCH", resource_id, resource_type or _type_from_id(resource_id),
                                   body=body)

    async def tag(self, resource_id: str, tags: dict[str, str],
                  resource_type: str | None = None) -> dict[str, Any]:
        """
        Merge tags onto a resource.

        MERGE (not Replace) semantics matter: a PATCH that omits a tag can remove
        it on some resource types, and losing `autoShutdown` would silently opt
        the resource out of cost control.
        """
        current = await self._request("GET", resource_id, resource_type or _type_from_id(resource_id))
        merged = dict(current.get("tags") or {})
        merged.update(tags)
        return await self._request("PATCH", resource_id, resource_type or _type_from_id(resource_id),
                                   body={"tags": merged})

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


def _type_from_id(resource_id: str) -> str:
    # /subscriptions/x/resourceGroups/y/providers/NS/type1/name1[/type2/name2]
    parts = resource_id.split("/providers/")
    if len(parts) < 2:
        return ""
    provider_path = parts[-1].split("/")
    namespace = provider_path[0]
    types = [p for i, p in enumerate(provider_path[1:]) if i % 2 == 0]
    return f"{namespace}/{'/'.join(types)}"


def _rg_from_id(resource_id: str) -> str:
    parts = resource_id.split("/")
    for i, p in enumerate(parts):
        if p.lower() == "resourcegroups" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _normalise_state(raw: dict, props: dict) -> str:
    # VMs: instanceView.statuses holds PowerState/running.
    view = (props.get("instanceView") or {}).get("statuses") or []
    for status in view:
        code = (status.get("code") or "").lower()
        if code.startswith("powerstate/"):
            return code.split("/", 1)[1]
    for key in ("provisioningState", "state", "powerState"):
        value = props.get(key)
        if isinstance(value, str) and value:
            return value.lower()
    return "unknown"


def _capacity(props: dict) -> int | None:
    for pool in props.get("agentPoolProfiles") or []:
        if pool.get("name") == "default":
            return int(pool.get("count") or 1)
    return None
