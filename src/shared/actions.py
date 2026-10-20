"""
Per-resource-type stop/start implementations.

Each handler is a small, explicit mapping from resource type to the ARM operation
that stops and starts it, plus the state values that mean "running" and
"stopped". No reflection, no generic "invoke whatever" — a cost tool should be
boring and obvious about what it does to your infrastructure.

Adding a new resource type is one entry in HANDLERS and one test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger("cost.actions")


class ArmClient(Protocol):
    """The slice of the ARM SDK this module needs. Injected, so tests are trivial."""

    async def get(self, resource_id: str) -> dict[str, Any]: ...
    async def post(self, resource_id: str, action: str, body: dict | None = None) -> dict[str, Any]: ...
    async def patch(self, resource_id: str, body: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Handler:
    """How to stop and start one resource type."""

    running_states: frozenset[str]
    stopped_states: frozenset[str]
    stop_action: str | None          # ARM POST action, e.g. "deallocate"
    start_action: str | None
    # For resources where "stopped" means scaled to zero (AKS, app service plans)
    scale_property: str | None = None
    scale_api_version: str = "2023-12-01"

    def needs_scale(self) -> bool:
        return self.scale_property is not None


HANDLERS: dict[str, Handler] = {
    # VMs: deallocate releases the compute reservation — this is the one that
    # actually saves money. "stop" without deallocate does not.
    "microsoft.compute/virtualmachines": Handler(
        running_states=frozenset({"running"}),
        stopped_states=frozenset({"deallocated", "stopped", "deallocating", "stopping"}),
        stop_action="deallocate",
        start_action="start",
    ),
    # AKS: scale the default node pool to 0. AKS itself keeps a small control
    # plane charge; the nodes are the cost.
    "microsoft.containerservice/managedclusters": Handler(
        running_states=frozenset({"running", "succeeded"}),
        stopped_states=frozenset({"stopped"}),
        stop_action=None,
        start_action=None,
        scale_property="nodePoolCount",
    ),
    # App Service: an app can be stopped, but the PLAN is what you pay for.
    # Scaling the plan to 0 is only valid for the Isolated tier, so we stop the
    # apps instead and record it as a partial saving.
    "microsoft.web/sites": Handler(
        running_states=frozenset({"running"}),
        stopped_states=frozenset({"stopped"}),
        stop_action="stop",
        start_action="start",
    ),
    # SQL serverless databases: pause via the databases API.
    "microsoft.sql/servers/databases": Handler(
        running_states=frozenset({"online", "resuming"}),
        stopped_states=frozenset({"paused", "pausing", "offline"}),
        stop_action="pause",
        start_action="resume",
    ),
    # Databricks clusters: state is in properties, handled by the generic path.
    "microsoft.databricks/workspaces": Handler(
        running_states=frozenset({"running"}),
        stopped_states=frozenset({"stopped"}),
        stop_action=None,
        start_action=None,
    ),
}

# Types the gates protect but the handler map also needs to know about, so the
# scheduler can report "skipped: no handler" rather than silently ignoring them.
UNSUPPORTED_BUT_KNOWN = frozenset({
    "microsoft.network/azurefirewalls",
    "microsoft.keyvault/vaults",
    "microsoft.operationalinsights/workspaces",
})


class UnsupportedResource(Exception):
    """No safe stop/start implementation exists for this type."""


def handler_for(resource_type: str) -> Handler:
    key = resource_type.lower()
    if key not in HANDLERS:
        raise UnsupportedResource(f"no handler for {resource_type}")
    return HANDLERS[key]


def is_running(resource_type: str, properties: dict[str, Any]) -> bool:
    """Interpret the resource's state from its ARM properties."""
    h = handler_for(resource_type)
    state = str(properties.get("provisioningState") or properties.get("state") or "").lower()

    # Some resources report state in nested fields; check the common ones.
    for key in ("powerState", "status", "runningState"):
        value = properties.get(key)
        if isinstance(value, str) and value.lower() in h.running_states | h.stopped_states:
            state = value.lower()
            break

    if state in h.running_states:
        return True
    if state in h.stopped_states:
        return False
    # Unknown state: treat as running so the tool does not silently skip work it
    # should be doing, and log it loudly.
    log.warning("unknown state '%s' for %s — assuming running", state, resource_type)
    return True


async def stop(client: ArmClient, resource_id: str, resource_type: str) -> dict[str, Any]:
    """
    Stop (or pause) a resource. Returns an action record.

    Note on VMs: `deallocate` is used rather than `stop` because a stopped VM
    still bills for the compute reservation. This distinction is the whole
    saving.
    """
    h = handler_for(resource_type)
    if h.needs_scale():
        body = {"properties": {"agentPoolProfiles": [
            {"name": "default", "count": 0}
        ]}}
        await client.patch(resource_id, body)
        return {"operation": "scale-to-zero", "resourceId": resource_id}

    if not h.stop_action:
        raise UnsupportedResource(f"{resource_type} has no stop operation")

    await client.post(resource_id, h.stop_action)
    return {"operation": h.stop_action, "resourceId": resource_id}


async def start(client: ArmClient, resource_id: str, resource_type: str,
                previous_capacity: int | None = None) -> dict[str, Any]:
    """Reverse of `stop`, restoring the recorded capacity where relevant."""
    h = handler_for(resource_type)
    if h.needs_scale():
        body = {"properties": {"agentPoolProfiles": [
            {"name": "default", "count": previous_capacity or 1}
        ]}}
        await client.patch(resource_id, body)
        return {"operation": "scale-up", "capacity": previous_capacity or 1,
                "resourceId": resource_id}

    if not h.start_action:
        raise UnsupportedResource(f"{resource_type} has no start operation")

    await client.post(resource_id, h.start_action)
    return {"operation": h.start_action, "resourceId": resource_id}


def recorded_capacity(resource_type: str, properties: dict[str, Any]) -> int | None:
    """
    Capture the capacity BEFORE stopping, so startup restores exactly what was
    there. Restoring "1" when the team had 5 is a silent regression.
    """
    h = handler_for(resource_type)
    if not h.needs_scale():
        return None
    pools = properties.get("agentPoolProfiles") or []
    for pool in pools:
        if pool.get("name") == "default":
            return int(pool.get("count") or 1)
    return None
