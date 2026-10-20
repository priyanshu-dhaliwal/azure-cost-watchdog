"""
The five safety gates.

This module is pure: no Azure, no I/O, no clock. Everything it needs is passed
in. That is deliberate — the logic that decides whether to stop a production
resource is the most consequential code in this repository, so it must be
testable without mocks and reviewable in one screen.

The gates are evaluated IN ORDER. The first failing gate aborts and its reason
is returned, because "why did this not stop" is the question asked most often.

    1. opt-in      — autoShutdown=true must be present
    2. environment — prod/production always aborts
    3. protected   — resource types and resource groups that must never stop
    4. cooldown    — an owner's keep-alive request suppresses the action
    5. idempotency — already in the desired state → no-op
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable


class Gate(str, Enum):
    OPT_IN = "1:opt-in"
    ENVIRONMENT = "2:environment"
    PROTECTED = "3:protected"
    COOLDOWN = "4:cooldown"
    IDEMPOTENCY = "5:idempotency"


class Decision(str, Enum):
    PROCEED = "proceed"
    SKIP = "skip"
    ABORT = "abort"


# Environments that can never be stopped, regardless of tags.
DENY_ENVIRONMENTS = {"prod", "production", "prd", "live"}

# Resource types that must never be stopped by a cost tool.
#   - firewalls/routers: stopping them is a network outage
#   - key vaults / DNS: platform dependencies of everything else
#   - log analytics: stopping ingestion blinds every alert
PROTECTED_TYPES = {
    "microsoft.network/azurefirewalls",
    "microsoft.network/virtualnetworkgateways",
    "microsoft.network/dnszones",
    "microsoft.keyvault/vaults",
    "microsoft.operationalinsights/workspaces",
    "microsoft.insights/components",
    "microsoft.managedidentity/userassignedidentities",
}

# Resource groups that are platform infrastructure, not workload.
PROTECTED_RG_PREFIXES = ("rg-platform", "rg-hub", "rg-tfstate", "rg-dns")

COOLDOWN_MINUTES = 10


@dataclass(frozen=True)
class Resource:
    """The minimum a gate needs to know about a resource."""

    id: str
    name: str
    type: str
    resource_group: str
    tags: dict[str, str]
    current_state: str            # "running" | "stopped" | "deallocated" | "online" | ...
    current_capacity: int | None = None


@dataclass(frozen=True)
class GateResult:
    gate: Gate
    decision: Decision
    reason: str

    @property
    def blocks(self) -> bool:
        return self.decision is not Decision.PROCEED


def _tag(tags: dict[str, str], key: str) -> str:
    # Tag keys are case-insensitive in Azure; values are not normalised for us.
    for k, v in tags.items():
        if k.lower() == key.lower():
            return (v or "").strip()
    return ""


def gate_opt_in(resource: Resource) -> GateResult:
    """
    Gate 1. Absence of the tag is NOT consent.

    This is the single most important rule: a resource that was never tagged is
    a resource nobody thought about, and stopping it is an unforced error.
    """
    value = _tag(resource.tags, "autoShutdown").lower()
    if value in ("true", "yes", "1"):
        return GateResult(Gate.OPT_IN, Decision.PROCEED, "autoShutdown=true")
    if value in ("false", "no", "0"):
        return GateResult(Gate.OPT_IN, Decision.SKIP, "autoShutdown explicitly false")
    return GateResult(Gate.OPT_IN, Decision.ABORT, "no autoShutdown tag — never act on untagged resources")


def gate_environment(resource: Resource) -> GateResult:
    """
    Gate 2. A production tag always wins.

    Even if someone sets autoShutdown=true on a prod resource, this gate aborts.
    The cost of being wrong here is an outage; the cost of being conservative is
    a few hundred rupees.
    """
    env = _tag(resource.tags, "environment").lower()
    if env in DENY_ENVIRONMENTS:
        return GateResult(Gate.ENVIRONMENT, Decision.ABORT, f"environment={env} is protected")
    if not env:
        return GateResult(Gate.ENVIRONMENT, Decision.ABORT, "no environment tag — cannot prove this is non-prod")
    return GateResult(Gate.ENVIRONMENT, Decision.PROCEED, f"environment={env}")


def gate_protected(resource: Resource) -> GateResult:
    """Gate 3. Some things are never optional, whatever their tags say."""
    rtype = resource.type.lower()
    if rtype in PROTECTED_TYPES:
        return GateResult(Gate.PROTECTED, Decision.ABORT, f"{resource.type} is a protected type")
    rg = resource.resource_group.lower()
    for prefix in PROTECTED_RG_PREFIXES:
        if rg.startswith(prefix):
            return GateResult(Gate.PROTECTED, Decision.ABORT, f"resource group '{resource.resource_group}' is platform infrastructure")
    return GateResult(Gate.PROTECTED, Decision.PROCEED, "not a protected type or group")


def gate_cooldown(resource: Resource, now: datetime | None = None) -> GateResult:
    """
    Gate 4. Honour a keep-alive request.

    `keepAliveUntil` is an ISO-8601 UTC timestamp written by the keep-alive
    endpoint. A developer mid-debug gets a grace period instead of a stopped VM.
    """
    now = now or datetime.now(timezone.utc)
    raw = _tag(resource.tags, "keepAliveUntil")
    if not raw:
        return GateResult(Gate.COOLDOWN, Decision.PROCEED, "no keep-alive")

    try:
        until = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        # A malformed keep-alive must not grant an indefinite grace period, and
        # must not silently block shutdown forever either. Treat it as expired
        # but surface it in the reason.
        return GateResult(Gate.COOLDOWN, Decision.PROCEED, "keep-alive unparseable — ignored")

    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)

    if until > now:
        remaining = int((until - now).total_seconds() // 60)
        return GateResult(Gate.COOLDOWN, Decision.SKIP, f"keep-alive for another {remaining} min")
    return GateResult(Gate.COOLDOWN, Decision.PROCEED, "keep-alive expired")


def gate_idempotency(resource: Resource, action: str) -> GateResult:
    """
    Gate 5. Already done? Then do nothing.

    The scheduler runs every 15 minutes; without this, a stopped VM would be
    "stopped" 96 times a day and the action log would be useless.
    """
    state = resource.current_state.lower()
    if action == "stop" and state in ("stopped", "deallocated", "deallocating", "stopping"):
        return GateResult(Gate.IDEMPOTENCY, Decision.SKIP, f"already {state}")
    if action == "start" and state in ("running", "starting"):
        return GateResult(Gate.IDEMPOTENCY, Decision.SKIP, f"already {state}")
    return GateResult(Gate.IDEMPOTENCY, Decision.PROCEED, f"state={state}")


@dataclass
class GateReport:
    resource_id: str
    action: str
    decision: Decision
    blocked_by: Gate | None
    reason: str
    results: list[GateResult]

    @property
    def should_act(self) -> bool:
        return self.decision is Decision.PROCEED


def evaluate(resource: Resource, action: str, now: datetime | None = None) -> GateReport:
    """
    Run all gates in order. The first blocking gate wins.

    `action` is "stop" or "start". Startup deliberately runs a *reduced* gate
    set: gates 1, 2 and 4 exist to prevent harm from stopping things, and none
    of them apply to starting a resource back up. Idempotency and protection
    still apply.
    """
    results: list[GateResult] = []

    if action == "stop":
        checks: Iterable[GateResult] = (
            gate_opt_in(resource),
            gate_environment(resource),
            gate_protected(resource),
            gate_cooldown(resource, now),
            gate_idempotency(resource, action),
        )
    else:
        checks = (
            gate_idempotency(resource, action),
        )

    for result in checks:
        results.append(result)
        if result.decision is Decision.ABORT:
            return GateReport(resource.id, action, Decision.ABORT, result.gate, result.reason, results)
        if result.decision is Decision.SKIP:
            return GateReport(resource.id, action, Decision.SKIP, result.gate, result.reason, results)

    return GateReport(resource.id, action, Decision.PROCEED, None, "all gates passed", results)
