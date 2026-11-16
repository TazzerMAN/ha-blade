"""Tests for setup, unload and the update listener."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from custom_components.blade import BladeRuntimeData
from custom_components.blade.api import (
    BladeApiError,
    BladeAuthenticationError,
    BladeConnectionError,
)
from custom_components.blade.const import CONF_OCR_ENABLED


async def test_setup_and_unload(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The entry loads, exposes runtime data and unloads cleanly."""
    entry = init_integration
    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, BladeRuntimeData)
    assert "gemma-4-26b-a4b" in entry.runtime_data.models
    assert hass.states.get("conversation.blade_conversation") is not None
    assert hass.states.get("ai_task.blade_ai_task") is not None
    assert hass.states.get("stt.blade_stt") is not None
    assert hass.states.get("tts.blade_tts") is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_services_registered(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The actions exist as soon as the integration is set up."""
    for service in ("ocr", "translate", "forecast"):
        assert hass.services.has_service("blade", service)


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (BladeAuthenticationError("bad"), ConfigEntryState.SETUP_ERROR),
        (BladeConnectionError("down"), ConfigEntryState.SETUP_RETRY),
        (BladeApiError(500, "boom", "oops"), ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_setup_errors(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    error: Exception,
    state: ConfigEntryState,
) -> None:
    """A rejected key asks for reauth; any other API error retries later."""
    mock_client.list_models.side_effect = error
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is state
    if state is ConfigEntryState.SETUP_ERROR:
        flows = hass.config_entries.flow.async_progress_by_handler("blade")
        assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_options_update_reloads(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Changing the options reloads the entry through the update listener."""
    before = mock_client.list_models.await_count
    hass.config_entries.async_update_entry(
        init_integration, options={CONF_OCR_ENABLED: False}
    )
    await hass.async_block_till_done()
    assert mock_client.list_models.await_count == before + 1
    assert init_integration.state is ConfigEntryState.LOADED
