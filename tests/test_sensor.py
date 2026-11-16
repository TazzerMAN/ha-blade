"""Tests for the account sensors."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.blade.api import BladeModel
from custom_components.blade.const import DOMAIN

COST_TODAY = "sensor.blade_estimated_cost_today"
COST_MONTH = "sensor.blade_estimated_cost_this_month"
TOKENS = "sensor.blade_tokens_this_month"
STATUS = "sensor.blade_account_status"

PRICED = BladeModel(
    "priced", "chat", batch={"multiplier": 1, "price_in": 2, "price_out": 4}
)


def state(hass: HomeAssistant, entity_id: str) -> State:
    """Return an existing state."""
    found = hass.states.get(entity_id)
    assert found is not None
    return found


async def test_initial_values(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Fresh sensors read zero and the account is fine."""
    assert state(hass, COST_TODAY).state == "0.0"
    assert state(hass, COST_MONTH).state == "0.0"
    assert state(hass, TOKENS).state == "0"
    assert state(hass, STATUS).state == "ok"
    assert state(hass, COST_TODAY).attributes["unit_of_measurement"] == "EUR"
    assert state(hass, COST_TODAY).attributes["by_service"] == {}
    # No cap: neither budget nor remaining are exposed.
    attributes = state(hass, COST_MONTH).attributes
    assert "budget" not in attributes
    assert "remaining" not in attributes
    assert state(hass, TOKENS).attributes["audio_seconds"] == 0.0
    assert state(hass, TOKENS).attributes["images"] == 0


async def test_device(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """The four sensors hang from one Blade account device."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    entries = [
        entity_registry.async_get(entity_id)
        for entity_id in (COST_TODAY, COST_MONTH, TOKENS, STATUS)
    ]
    assert {entry.device_id for entry in entries if entry} != {None}
    assert len({entry.device_id for entry in entries if entry}) == 1
    assert entries[0] is not None
    assert entries[0].unique_id == f"{init_integration.entry_id}_cost_today"
    device = device_registry.async_get(entries[0].device_id or "")
    assert device is not None
    assert device.name == "Blade"
    assert device.identifiers == {(DOMAIN, init_integration.entry_id)}
    assert device.entry_type is dr.DeviceEntryType.SERVICE


async def test_updates_after_a_call(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A usage report refreshes the figures and the per-service breakdown."""
    init_integration.runtime_data.models["priced"] = PRICED
    mock_client.hooks.usage("priced", tokens_in=500_000.0, tokens_out=250_000.0)
    await hass.async_block_till_done()
    assert float(state(hass, COST_TODAY).state) == pytest.approx(2.0)
    assert float(state(hass, COST_MONTH).state) == pytest.approx(2.0)
    assert state(hass, TOKENS).state == "750000"
    assert state(hass, COST_TODAY).attributes["by_service"] == {"actions": 2.0}
    assert state(hass, COST_MONTH).attributes["by_service"] == {"actions": 2.0}


async def test_budget_attributes(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """With a cap the month sensor shows the budget and what is left."""
    usage = init_integration.runtime_data.usage
    usage.budget = 5.0
    init_integration.runtime_data.models["priced"] = PRICED
    mock_client.hooks.usage("priced", tokens_in=1_000_000.0)
    await hass.async_block_till_done()
    attributes = state(hass, COST_MONTH).attributes
    assert attributes["budget"] == 5.0
    assert attributes["remaining"] == 3.0
    mock_client.hooks.usage("priced", tokens_in=2_000_000.0)
    await hass.async_block_till_done()
    assert state(hass, COST_MONTH).attributes["remaining"] == 0


async def test_status_enum(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The status follows the account errors and the cap, and recovers."""
    mock_client.hooks.account_error("invoice_unpaid")
    await hass.async_block_till_done()
    assert state(hass, STATUS).state == "invoice_unpaid"
    mock_client.hooks.account_error("weird")
    await hass.async_block_till_done()
    assert state(hass, STATUS).state == "payment_error"
    mock_client.hooks.usage("whatever", tokens_in=1.0)
    await hass.async_block_till_done()
    assert state(hass, STATUS).state == "ok"
    assert "local_budget_reached" in state(hass, STATUS).attributes["options"]


async def test_unload_removes_listeners(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Unloading the entry detaches the sensors from the tracker."""
    usage = init_integration.runtime_data.usage
    assert usage._listeners
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert not usage._listeners
