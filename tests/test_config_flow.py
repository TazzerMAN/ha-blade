"""Tests for the config flow, the options flow and every subentry flow."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntryState,
)
from homeassistant.const import CONF_API_KEY, CONF_LLM_HASS_API, CONF_MODEL, CONF_PROMPT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import llm
from pytest_homeassistant.common import MockConfigEntry

from custom_components.blade.api import (
    BladeApiError,
    BladeAuthenticationError,
    BladeConnectionError,
)
from custom_components.blade.config_flow import (
    BladeConfigFlow,
    BladeSubentryFlow,
    ConversationFlowHandler,
    TTSFlowHandler,
    model_selector,
)
from custom_components.blade.const import (
    CONF_CODE_INTERPRETER,
    CONF_CREATE_PIPELINE,
    CONF_FORECAST_ENABLED,
    CONF_FORECAST_MODEL,
    CONF_IMAGE_MODEL,
    CONF_IMAGE_SIZE,
    CONF_IMAGES_ENABLED,
    CONF_MAX_TOKENS,
    CONF_MONTHLY_BUDGET,
    CONF_OCR_ENABLED,
    CONF_OCR_MODEL,
    CONF_REASONING_EFFORT,
    CONF_RECOMMENDED,
    CONF_STREAMING,
    CONF_TEMPERATURE,
    CONF_TRANSLATE_ENABLED,
    CONF_TRANSLATE_MODEL,
    CONF_VOICE,
    CONF_WEB_FETCH,
    CONF_WEB_SEARCH,
    DEFAULT_ENTRY_OPTIONS,
    DOMAIN,
    SUBENTRY_AI_TASK,
    SUBENTRY_CONVERSATION,
    SUBENTRY_EVALUATE,
    SUBENTRY_STT,
    SUBENTRY_TTS,
    default_name,
)

from .conftest import CATALOG, subentry_id

NEW_KEY = "sk-new"


async def start_user_flow(hass: HomeAssistant) -> dict[str, Any]:
    """Open the user step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


BRAND_TITLES = [
    "Blade Conversation",
    "Blade AI Task",
    "Blade Evaluation",
    "Blade STT",
    "Blade TTS",
]


# Titles stay brand-first in every language, like the other voice providers.
@pytest.mark.parametrize("language", ["en", "fr", "fr-CA", "de"])
async def test_user_flow(
    hass: HomeAssistant, mock_client: MagicMock, language: str
) -> None:
    """The key creates the entry with five default subentries named by language."""
    hass.config.language = language
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: NEW_KEY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Blade"
    assert result["data"] == {CONF_API_KEY: NEW_KEY, CONF_CREATE_PIPELINE: True}
    assert result["options"] == DEFAULT_ENTRY_OPTIONS
    await hass.async_block_till_done()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.state is ConfigEntryState.LOADED
    # The pipeline flag is one-shot: setup consumes it.
    assert entry.data == {CONF_API_KEY: NEW_KEY}
    assert sorted(s.subentry_type for s in entry.subentries.values()) == sorted(
        [
            SUBENTRY_CONVERSATION,
            SUBENTRY_AI_TASK,
            SUBENTRY_EVALUATE,
            SUBENTRY_STT,
            SUBENTRY_TTS,
        ]
    )
    assert [s.title for s in entry.subentries.values()] == BRAND_TITLES
    assert mock_client.list_models.await_count >= 1


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (BladeAuthenticationError("no"), "invalid_auth"),
        (BladeConnectionError("down"), "cannot_connect"),
        (BladeApiError(500, None, "x"), "cannot_connect"),
        (RuntimeError("surprise"), "unknown"),
    ],
)
async def test_user_flow_errors_then_recover(
    hass: HomeAssistant, mock_client: MagicMock, error: Exception, key: str
) -> None:
    """Each failure shows its error and the form can be resubmitted."""
    mock_client.list_models.side_effect = error
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: NEW_KEY}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}

    mock_client.list_models.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: NEW_KEY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_already_configured(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The same key cannot be added twice."""
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: mock_config_entry.data[CONF_API_KEY]}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Reauth validates the new key, stores it and reloads."""
    result = await init_integration.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    mock_client.list_models.side_effect = BladeAuthenticationError("no")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "still-bad"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    mock_client.list_models.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: NEW_KEY}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert init_integration.data[CONF_API_KEY] == NEW_KEY
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.LOADED


async def test_reconfigure(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Reconfigure replaces the key even when it equals another entry's."""
    result = await init_integration.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    mock_client.list_models.side_effect = BladeConnectionError("down")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: NEW_KEY}
    )
    assert result["errors"] == {"base": "cannot_connect"}

    mock_client.list_models.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: NEW_KEY}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert init_integration.data[CONF_API_KEY] == NEW_KEY


def test_flow_declares_supported_subentries(mock_config_entry: MockConfigEntry) -> None:
    """Every feature is an addable subentry type."""
    assert set(
        BladeConfigFlow.async_get_supported_subentry_types(mock_config_entry)
    ) == {
        SUBENTRY_CONVERSATION,
        SUBENTRY_AI_TASK,
        SUBENTRY_EVALUATE,
        SUBENTRY_STT,
        SUBENTRY_TTS,
    }


def test_default_name() -> None:
    """Default titles are short and brand first."""
    assert default_name(SUBENTRY_STT) == "Blade STT"
    assert default_name(SUBENTRY_TTS) == "Blade TTS"


def test_model_selector_labels() -> None:
    """The publisher is appended to the label when known."""
    config = model_selector(
        CATALOG, lambda m: m.id in {"hy-mt2-7b", "text-only"}
    ).config
    assert {o["value"]: o["label"] for o in config["options"]} == {
        "hy-mt2-7b": "hy-mt2-7b (tencent)",
        "text-only": "text-only",
    }


# ------------------------------------------------------------------ Options


async def test_options_flow(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The options form lists the matching models and stores the answers."""
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    schema = {str(key): key for key in result["data_schema"].schema}
    assert schema[CONF_OCR_MODEL].description["suggested_value"] == "ovisocr3-0.8b"

    options = {
        CONF_OCR_ENABLED: False,
        CONF_OCR_MODEL: "gemma-4-31b",
        CONF_TRANSLATE_ENABLED: True,
        CONF_TRANSLATE_MODEL: "hy-mt2-7b",
        CONF_FORECAST_ENABLED: True,
        CONF_FORECAST_MODEL: "timestral-28m",
        CONF_MONTHLY_BUDGET: 5,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], options
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.options == {**options, CONF_MONTHLY_BUDGET: 5.0}
    assert isinstance(init_integration.options[CONF_MONTHLY_BUDGET], float)
    await hass.async_block_till_done()
    assert init_integration.state is ConfigEntryState.LOADED


async def test_options_flow_entry_not_loaded(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The options need the catalog, hence a loaded entry."""
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"


# ---------------------------------------------------------------- Subentries


async def add_subentry(
    hass: HomeAssistant, entry: MockConfigEntry, subentry_type: str
) -> dict[str, Any]:
    """Start the add flow of a subentry type."""
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, subentry_type), context={"source": SOURCE_USER}
    )


async def reconfigure_subentry(
    hass: HomeAssistant, entry: MockConfigEntry, subentry_type: str
) -> dict[str, Any]:
    """Start the reconfigure flow of the first subentry of a type."""
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, subentry_type),
        context={
            "source": SOURCE_RECONFIGURE,
            "subentry_id": subentry_id(entry, subentry_type),
        },
    )


def newest(entry: MockConfigEntry, subentry_type: str) -> Any:
    """Return the last subentry of a type."""
    return [s for s in entry.subentries.values() if s.subentry_type == subentry_type][
        -1
    ]


@pytest.mark.parametrize(
    ("subentry_type", "user_input", "expected"),
    [
        (
            SUBENTRY_AI_TASK,
            {
                "name": "Vision task",
                CONF_MODEL: "gemma-4-31b",
                CONF_IMAGES_ENABLED: False,
                CONF_IMAGE_MODEL: "flux-2-klein",
                CONF_IMAGE_SIZE: "512x512",
            },
            {
                CONF_MODEL: "gemma-4-31b",
                CONF_IMAGES_ENABLED: False,
                CONF_IMAGE_MODEL: "flux-2-klein",
                CONF_IMAGE_SIZE: "512x512",
            },
        ),
        (
            SUBENTRY_EVALUATE,
            {"name": "Judge", CONF_MODEL: "clef-flash"},
            {CONF_MODEL: "clef-flash"},
        ),
        (
            SUBENTRY_STT,
            {"name": "Ears", CONF_MODEL: "whisper-large", CONF_PROMPT: "Smart home."},
            {CONF_MODEL: "whisper-large", CONF_PROMPT: "Smart home."},
        ),
        (
            SUBENTRY_STT,
            {"name": "Ears", CONF_MODEL: "voxtral-mini-3b"},
            {CONF_MODEL: "voxtral-mini-3b"},
        ),
    ],
)
async def test_add_simple_subentries(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    subentry_type: str,
    user_input: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    """Each simple feature is added from its form with the chosen title."""
    result = await add_subentry(hass, init_integration, subentry_type)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == user_input["name"]
    assert result["data"] == expected
    await hass.async_block_till_done()


async def test_add_subentry_default_name(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The name field is pre-filled with the brand-first title."""
    hass.config.language = "fr"
    result = await add_subentry(hass, init_integration, SUBENTRY_EVALUATE)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_MODEL: "clef-flash"}
    )
    assert result["title"] == "Blade Evaluation"


async def test_subentry_entry_not_loaded(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Features cannot be configured while the catalog is unavailable."""
    result = await add_subentry(hass, mock_config_entry, SUBENTRY_STT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"
    result = await reconfigure_subentry(hass, mock_config_entry, SUBENTRY_STT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"


async def test_reconfigure_simple_subentry(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Reconfiguring shows the current values, with no name field, and updates."""
    result = await reconfigure_subentry(hass, init_integration, SUBENTRY_EVALUATE)
    assert result["type"] is FlowResultType.FORM
    assert "name" not in {str(key) for key in result["data_schema"].schema}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_MODEL: "clef-flash"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()


# -- conversation


def conversation_input(**overrides: Any) -> dict[str, Any]:
    """Return a complete conversation form."""
    data: dict[str, Any] = {
        "name": "Chat",
        CONF_MODEL: "gemma-4-26b-a4b",
        CONF_PROMPT: "Be brief.",
        CONF_LLM_HASS_API: [llm.LLM_API_ASSIST],
        CONF_WEB_SEARCH: True,
        CONF_WEB_FETCH: False,
        CONF_CODE_INTERPRETER: False,
        CONF_RECOMMENDED: True,
    }
    data.update(overrides)
    return data


def reconfigure_input(**overrides: Any) -> dict[str, Any]:
    """Return a conversation form without the name field."""
    data = conversation_input(**overrides)
    del data["name"]
    return data


ADVANCED = pytest.mark.parametrize(
    "conversation_data",
    [
        {
            CONF_MODEL: "gemma-4-26b-a4b",
            CONF_RECOMMENDED: False,
            CONF_TEMPERATURE: 1.1,
            CONF_MAX_TOKENS: 200,
            CONF_REASONING_EFFORT: "high",
            CONF_LLM_HASS_API: [llm.LLM_API_ASSIST, "ghost_api"],
        }
    ],
)


async def test_add_conversation_recommended(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """With recommended settings there is no advanced step."""
    result = await add_subentry(hass, init_integration, SUBENTRY_CONVERSATION)
    assert result["step_id"] == "init"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], conversation_input()
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_MODEL: "gemma-4-26b-a4b",
        CONF_PROMPT: "Be brief.",
        CONF_LLM_HASS_API: [llm.LLM_API_ASSIST],
        CONF_WEB_SEARCH: True,
        CONF_WEB_FETCH: False,
        CONF_CODE_INTERPRETER: False,
        CONF_RECOMMENDED: True,
    }
    await hass.async_block_till_done()


async def test_add_conversation_advanced(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Unticking recommended opens the advanced step; no LLM API drops the key."""
    result = await add_subentry(hass, init_integration, SUBENTRY_CONVERSATION)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        conversation_input(**{CONF_RECOMMENDED: False, CONF_LLM_HASS_API: []}),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "advanced"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        # The number selector sends a float, as the frontend does.
        {CONF_TEMPERATURE: 0.3, CONF_MAX_TOKENS: 512.0, CONF_REASONING_EFFORT: "low"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_LLM_HASS_API not in result["data"]
    assert result["data"][CONF_TEMPERATURE] == 0.3
    assert result["data"][CONF_MAX_TOKENS] == 512
    assert isinstance(result["data"][CONF_MAX_TOKENS], int)
    assert result["data"][CONF_REASONING_EFFORT] == "low"
    assert result["data"][CONF_RECOMMENDED] is False
    await hass.async_block_till_done()


@ADVANCED
async def test_reconfigure_conversation_back_to_recommended(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Going back to recommended settings forgets the advanced values."""
    entry = init_integration
    result = await reconfigure_subentry(hass, entry, SUBENTRY_CONVERSATION)
    assert result["step_id"] == "init"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], reconfigure_input(**{CONF_RECOMMENDED: True})
    )
    assert result["type"] is FlowResultType.ABORT
    data = entry.subentries[subentry_id(entry, SUBENTRY_CONVERSATION)].data
    assert CONF_TEMPERATURE not in data
    assert CONF_MAX_TOKENS not in data
    assert CONF_REASONING_EFFORT not in data
    await hass.async_block_till_done()


@ADVANCED
async def test_reconfigure_conversation_drops_unknown_api(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """An LLM API that no longer exists is removed from the suggestions."""
    result = await reconfigure_subentry(hass, init_integration, SUBENTRY_CONVERSATION)
    schema = {str(key): key for key in result["data_schema"].schema}
    suggested = schema[CONF_LLM_HASS_API].description["suggested_value"]
    assert suggested == [llm.LLM_API_ASSIST]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], reconfigure_input(**{CONF_RECOMMENDED: False})
    )
    assert result["step_id"] == "advanced"
    schema = {str(key): key for key in result["data_schema"].schema}
    assert schema[CONF_TEMPERATURE].description["suggested_value"] == 1.1
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_TEMPERATURE: 0.5, CONF_MAX_TOKENS: 300, CONF_REASONING_EFFORT: "none"},
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "conversation_data", [{CONF_MODEL: "gemma-4-26b-a4b", CONF_RECOMMENDED: True}]
)
async def test_reconfigure_conversation_without_llm_api(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A conversation with no LLM API shows no suggestion for it."""
    result = await reconfigure_subentry(hass, init_integration, SUBENTRY_CONVERSATION)
    schema = {str(key): key for key in result["data_schema"].schema}
    assert not schema[CONF_LLM_HASS_API].description
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], reconfigure_input(**{CONF_LLM_HASS_API: []})
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()


def test_conversation_handler_defaults() -> None:
    """The conversation handler starts from the recommended options."""
    assert ConversationFlowHandler.defaults[CONF_RECOMMENDED] is True


def test_base_subentry_flow_needs_a_schema() -> None:
    """The common flow is abstract: each feature supplies its own schema."""
    with pytest.raises(NotImplementedError):
        BladeSubentryFlow()._schema()


# -- TTS


def tts_input(**overrides: Any) -> dict[str, Any]:
    """Return a complete TTS form."""
    data: dict[str, Any] = {
        "name": "Voice",
        CONF_MODEL: "kyutai-tts-1.6b-en-fr",
        CONF_VOICE: "unmute-prod-website/developer-1.mp3",
        CONF_STREAMING: True,
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    ("overrides", "voice", "streaming"),
    [
        ({}, "unmute-prod-website/developer-1.mp3", True),
        # Another model's voice is replaced by the first voice of the new model.
        ({CONF_MODEL: "kokoro-82m"}, "af_heart", False),
        ({CONF_MODEL: "kokoro-82m", CONF_VOICE: "bf_emma"}, "bf_emma", False),
        # A voice typed in by hand is kept for a documented model too.
        ({CONF_MODEL: "kokoro-82m", CONF_VOICE: "ma_voix"}, "ma_voix", False),
        # A model with no documented voices keeps what was typed.
        ({CONF_MODEL: "text-only", CONF_VOICE: "custom"}, "custom", False),
        ({CONF_STREAMING: False}, "unmute-prod-website/developer-1.mp3", False),
    ],
)
async def test_add_tts(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    overrides: dict[str, Any],
    voice: str,
    streaming: bool,
) -> None:
    """The voice follows the model and streaming is limited to capable models."""
    result = await add_subentry(hass, init_integration, SUBENTRY_TTS)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], tts_input(**overrides)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_VOICE] == voice
    assert result["data"][CONF_STREAMING] is streaming
    await hass.async_block_till_done()


def test_tts_handler_defaults() -> None:
    """The TTS handler starts from the recommended options."""
    assert TTSFlowHandler.defaults[CONF_STREAMING] is True
    assert DEFAULT_ENTRY_OPTIONS[CONF_FORECAST_MODEL] == "timestral-28m"
