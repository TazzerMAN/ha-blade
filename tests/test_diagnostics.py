"""Tests for the diagnostics."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant.typing import ClientSessionGenerator
from syrupy.assertion import SnapshotAssertion

from .conftest import API_KEY


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    snapshot: SnapshotAssertion,
) -> None:
    """The report describes the setup and never contains the API key."""
    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, init_integration
    )
    assert diagnostics == snapshot
    assert API_KEY not in str(diagnostics)
    assert diagnostics["data"]["api_key"] == "**REDACTED**"


async def test_diagnostics_unlisted_model(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
) -> None:
    """A model missing from the catalog is flagged."""
    entry = init_integration
    sub = entry.subentries["stt_id"]
    hass.config_entries.async_update_subentry(entry, sub, data={"model": "ghost"})
    await hass.async_block_till_done()
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diagnostics["subentries"]["stt_id"]["model_listed"] is False
    assert diagnostics["subentries"]["tts_id"]["model_listed"] is True
