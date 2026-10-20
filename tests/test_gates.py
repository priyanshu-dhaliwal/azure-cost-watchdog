"""Tests for the five safety gates.

The gates decide whether production infrastructure is stopped. Every branch is
tested here, including the combinations that are supposed to be impossible —
because the expensive failure is the one where a tag is right but the
combination is wrong.
"""

from __future__ import annotations

import sys
import pathlib
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from shared.gates import (  # noqa: E402
    Decision, Gate, Resource, evaluate,
    gate_cooldown, gate_environment, gate_idempotency, gate_opt_in, gate_protected,
)


def vm(tags: dict[str, str] | None = None, state: str = "running",
       rg: str = "rg-dev-app", rtype: str = "Microsoft.Compute/virtualMachines") -> Resource:
    base = {"autoShutdown": "true", "environment": "dev", "owner": "team-a"}
    base.update(tags or {})
    return Resource(
        id=f"/subscriptions/s/resourceGroups/{rg}/providers/{rtype}/res-1",
        name="res-1",
        type=rtype,
        resource_group=rg,
        tags=base,
        current_state=state,
    )


# --------------------------------------------------------------------------
# Gate 1 — opt-in
# --------------------------------------------------------------------------

def test_untagged_resource_is_aborted_not_skipped():
    """Absence of a tag is not consent. This is the most important assertion in
    the repository: it is the difference between a cost tool and an outage."""
    r = vm()
    r = Resource(r.id, r.name, r.type, r.resource_group, {"environment": "dev"}, "running")
    result = gate_opt_in(r)
    assert result.decision is Decision.ABORT
    assert "never act on untagged" in result.reason


def test_auto_shutdown_false_is_skipped():
    result = gate_opt_in(vm({"autoShutdown": "false"}))
    assert result.decision is Decision.SKIP


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "yes", "1"])
def test_opt_in_accepts_common_truthy_spellings(value):
    assert gate_opt_in(vm({"autoShutdown": value})).decision is Decision.PROCEED


def test_tag_keys_are_case_insensitive():
    r = vm()
    r = Resource(r.id, r.name, r.type, r.resource_group,
                 {"AUTOSHUTDOWN": "true", "Environment": "dev"}, "running")
    assert gate_opt_in(r).decision is Decision.PROCEED
    assert gate_environment(r).decision is Decision.PROCEED


# --------------------------------------------------------------------------
# Gate 2 — environment
# --------------------------------------------------------------------------

@pytest.mark.parametrize("env", ["prod", "PROD", "production", "prd", "live"])
def test_production_is_never_stopped_even_when_opted_in(env):
    """A prod resource with autoShutdown=true must still be refused. The cost of
    being wrong is an outage; the cost of being conservative is a few rupees."""
    report = evaluate(vm({"environment": env}), "stop")
    assert report.decision is Decision.ABORT
    assert report.blocked_by is Gate.ENVIRONMENT


def test_missing_environment_tag_aborts():
    r = vm()
    r = Resource(r.id, r.name, r.type, r.resource_group, {"autoShutdown": "true"}, "running")
    report = evaluate(r, "stop")
    assert report.decision is Decision.ABORT
    assert report.blocked_by is Gate.ENVIRONMENT


# --------------------------------------------------------------------------
# Gate 3 — protected resources
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rtype", [
    "Microsoft.Network/azureFirewalls",
    "Microsoft.KeyVault/vaults",
    "Microsoft.OperationalInsights/workspaces",
    "Microsoft.Network/virtualNetworkGateways",
])
def test_protected_types_are_never_stopped(rtype):
    report = evaluate(vm(rtype=rtype), "stop")
    assert report.decision is Decision.ABORT
    assert report.blocked_by is Gate.PROTECTED


@pytest.mark.parametrize("rg", ["rg-platform-prod", "rg-hub-network", "rg-tfstate-platform"])
def test_platform_resource_groups_are_never_stopped(rg):
    report = evaluate(vm(rg=rg), "stop")
    assert report.decision is Decision.ABORT
    assert report.blocked_by is Gate.PROTECTED


def test_workload_rg_is_allowed():
    assert gate_protected(vm(rg="rg-dev-app")).decision is Decision.PROCEED


# --------------------------------------------------------------------------
# Gate 4 — keep-alive cooldown
# --------------------------------------------------------------------------

def test_keepalive_suppresses_shutdown():
    until = (datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat()
    result = gate_cooldown(vm({"keepAliveUntil": until}))
    assert result.decision is Decision.SKIP
    assert "keep-alive" in result.reason


def test_expired_keepalive_allows_shutdown():
    until = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert gate_cooldown(vm({"keepAliveUntil": until})).decision is Decision.PROCEED


def test_malformed_keepalive_does_not_block_forever():
    """A garbage timestamp must not grant an infinite grace period — otherwise a
    typo disables the whole cost system for one resource, silently."""
    result = gate_cooldown(vm({"keepAliveUntil": "tomorrow-ish"}))
    assert result.decision is Decision.PROCEED
    assert "unparseable" in result.reason


def test_keepalive_with_z_suffix_parses():
    until = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert gate_cooldown(vm({"keepAliveUntil": until})).decision is Decision.SKIP


# --------------------------------------------------------------------------
# Gate 5 — idempotency
# --------------------------------------------------------------------------

def test_already_stopped_is_a_noop():
    report = evaluate(vm(state="deallocated"), "stop")
    assert report.decision is Decision.SKIP
    assert report.blocked_by is Gate.IDEMPOTENCY


def test_already_running_is_a_noop_for_start():
    report = evaluate(vm(state="running"), "start")
    assert report.decision is Decision.SKIP


def test_running_resource_proceeds_through_all_gates():
    report = evaluate(vm(), "stop")
    assert report.decision is Decision.PROCEED
    assert [r.gate for r in report.results] == [
        Gate.OPT_IN, Gate.ENVIRONMENT, Gate.PROTECTED, Gate.COOLDOWN, Gate.IDEMPOTENCY,
    ]
    assert report.should_act


# --------------------------------------------------------------------------
# Ordering: the first failing gate wins, and gates do not run after it
# --------------------------------------------------------------------------

def test_first_failing_gate_short_circuits():
    """An untagged prod resource must report the opt-in gate, not the
    environment gate — because the fix is different (tag it vs do not tag it)."""
    r = vm({"environment": "prod"})
    r = Resource(r.id, r.name, r.type, r.resource_group, {"environment": "prod"}, "running")
    report = evaluate(r, "stop")
    assert report.blocked_by is Gate.OPT_IN
    assert len(report.results) == 1


def test_prod_with_keepalive_still_aborts_on_environment():
    """A keep-alive must never be able to authorise stopping production."""
    until = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    report = evaluate(vm({"environment": "prod", "keepAliveUntil": until}), "stop")
    assert report.decision is Decision.ABORT
    assert report.blocked_by is Gate.ENVIRONMENT


def test_start_path_skips_harm_prevention_gates():
    """Starting a resource back up cannot cause harm, so the gates that exist to
    prevent harm do not run — otherwise a prod resource stopped out-of-band
    could never be restarted by this tool."""
    report = evaluate(vm({"environment": "prod"}, state="deallocated"), "start")
    assert report.decision is Decision.PROCEED
    assert [r.gate for r in report.results] == [Gate.IDEMPOTENCY]
