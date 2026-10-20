"""Tests for the per-resource-type handlers and the savings arithmetic."""

from __future__ import annotations

import asyncio
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from shared.actions import (  # noqa: E402
    UnsupportedResource, handler_for, is_running, recorded_capacity, start, stop,
)
from shared.pricing import Price, saving  # noqa: E402


class FakeArm:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []

    async def get(self, resource_id):
        return {}

    async def post(self, resource_id, action, body=None):
        self.calls.append(("post", action, body))
        return {}

    async def patch(self, resource_id, body):
        self.calls.append(("patch", resource_id, body))
        return {}


def test_vm_uses_deallocate_not_stop():
    """A stopped VM still bills the compute reservation. `deallocate` is the
    operation that actually saves money — this test exists so nobody 'simplifies'
    it to stop later."""
    client = FakeArm()
    asyncio.run(stop(client, "/vm/1", "Microsoft.Compute/virtualMachines"))
    assert client.calls == [("post", "deallocate", None)]


def test_app_service_stops_the_app():
    client = FakeArm()
    asyncio.run(stop(client, "/app/1", "Microsoft.Web/sites"))
    assert client.calls[0][1] == "stop"


def test_aks_scales_to_zero_and_records_capacity():
    props = {"agentPoolProfiles": [{"name": "default", "count": 4}]}
    assert recorded_capacity("Microsoft.ContainerService/managedClusters", props) == 4

    client = FakeArm()
    asyncio.run(stop(client, "/aks/1", "Microsoft.ContainerService/managedClusters"))
    kind, _, body = client.calls[0]
    assert kind == "patch"
    assert body["properties"]["agentPoolProfiles"][0]["count"] == 0


def test_aks_start_restores_the_recorded_capacity():
    """Restoring 1 when the team had 4 is a silent regression that nobody notices
    until the next load test."""
    client = FakeArm()
    asyncio.run(start(client, "/aks/1", "Microsoft.ContainerService/managedClusters",
                      previous_capacity=4))
    _, _, body = client.calls[0]
    assert body["properties"]["agentPoolProfiles"][0]["count"] == 4


def test_unknown_type_raises_rather_than_guessing():
    with pytest.raises(UnsupportedResource):
        handler_for("Microsoft.Fabric/capacities")


def test_unknown_state_is_treated_as_running():
    """Failing safe here means doing the work, not skipping it. A resource we
    cannot read the state of might well be running and costing money."""
    assert is_running("Microsoft.Compute/virtualMachines", {"provisioningState": "weird"}) is True


def test_deallocated_is_not_running():
    assert is_running("Microsoft.Compute/virtualMachines",
                      {"provisioningState": "deallocated"}) is False


# --------------------------------------------------------------------------
# Savings arithmetic
# --------------------------------------------------------------------------

def test_saving_is_linear_and_rounded():
    assert saving(10.0, 8.0) == 80.0
    assert saving(4.123456, 100) == 412.35


def test_hourly_normalisation_from_monthly_unit():
    p = Price("m", "sku", "centralindia", 7300.0, "1/Month", "INR")
    assert p.per_hour == 10.0


def test_hourly_normalisation_from_hourly_unit():
    p = Price("m", "sku", "centralindia", 12.5, "1 Hour", "INR")
    assert p.per_hour == 12.5


def test_non_time_unit_has_no_hourly_price():
    p = Price("m", "sku", "centralindia", 1.5, "1 GB/Month", "INR")
    assert p.per_hour is None


def test_weekly_saving_for_a_standard_dev_schedule():
    """
    600 running minutes/day × 5 days = 3000 of 10080 minutes/week.
    7080 stopped minutes = 118 stopped hours/week.
    At ₹12/hour that is ₹1416/week, or ₹6,144/month — a number the report claims.
    """
    stopped_hours = 7080 / 60
    assert saving(12.0, stopped_hours * 4) == pytest.approx(5664.0)
