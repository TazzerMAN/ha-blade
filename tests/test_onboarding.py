"""Tests for the first-run defaults: models per account, and the Blade pipeline."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from homeassistant.components.assist_pipeline import async_get_pipelines
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_MODEL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.blade.api import BladeModel
from custom_components.blade.config_flow import (
    default_options,
    default_subentries,
    is_stt,
    model_selector,
)
from custom_components.blade.const import (
    CONF_CREATE_PIPELINE,
    CONF_FORECAST_ENABLED,
    CONF_IMAGES_ENABLED,
    CONF_OCR_ENABLED,
    CONF_STREAMING,
    CONF_VOICE,
    DOMAIN,
    PIPELINE_NAME,
    RECOMMENDED_MODELS,
    SUBENTRY_CONVERSATION,
    SUBENTRY_EVALUATE,
    default_prompt,
)

from .conftest import CATALOG


def _by_type(subentries: list[Any]) -> dict[str, dict[str, Any]]:
    return {s["subentry_type"]: dict(s["data"]) for s in subentries}


def test_recommended_models_kept_when_available() -> None:
    """The first preference present on the account wins."""
    catalog = {
        **CATALOG,
        "qwen3.6-35b-a3b": BladeModel(
            "qwen3.6-35b-a3b", "chat", supported_parameters=frozenset({"tools"})
        ),
    }
    data = _by_type(default_subentries(catalog))
    assert data["conversation"][CONF_MODEL] == "qwen3.6-35b-a3b"
    assert data["ai_task_data"][CONF_MODEL] == "gemma-4-31b"
    assert data["stt"][CONF_MODEL] == "voxtral-mini-3b"


def test_fallback_to_next_preference() -> None:
    """Without the first choice, the next preference is taken."""
    data = _by_type(default_subentries(CATALOG))
    assert data["conversation"][CONF_MODEL] == "gemma-4-26b-a4b"


def test_glob_preference_wins_for_future_models() -> None:
    """A Gladia model, once on the account, becomes the default transcriber."""
    gladia = BladeModel("gladia-solaria-1", "transcription")
    catalog = {**CATALOG, gladia.id: gladia}
    assert is_stt(gladia)
    data = _by_type(default_subentries(catalog))
    assert data["stt"][CONF_MODEL] == "gladia-solaria-1"


def test_missing_kinds_are_skipped_or_switched_off() -> None:
    """No decision model: no evaluation; no image model: images off."""
    catalog = {k: v for k, v in CATALOG.items() if v.kind not in ("decision", "image")}
    data = _by_type(default_subentries(catalog))
    assert SUBENTRY_EVALUATE not in data
    assert data["ai_task_data"][CONF_IMAGES_ENABLED] is False


def test_tts_voice_follows_fallback_model() -> None:
    """With Kokoro only, its first voice is used and streaming is off."""
    catalog = {k: v for k, v in CATALOG.items() if k != "kyutai-tts-1.6b-en-fr"}
    tts = _by_type(default_subentries(catalog))["tts"]
    assert tts[CONF_MODEL] == "kokoro-82m"
    assert tts[CONF_VOICE] == "af_heart"
    assert tts[CONF_STREAMING] is False


def test_unknown_tts_model_keeps_voice() -> None:
    """A speech model without documented voices keeps the recommended voice id."""
    catalog = {k: v for k, v in CATALOG.items() if v.kind != "speech"} | {
        "new-tts": BladeModel("new-tts", "speech")
    }
    tts = _by_type(default_subentries(catalog))["tts"]
    assert tts[CONF_MODEL] == "new-tts"
    assert tts[CONF_VOICE] == "unmute-prod-website/developpeuse-3.wav"


def test_default_options_switch_off_actions_without_model() -> None:
    """An action whose kind is missing from the catalog starts disabled."""
    catalog = {k: v for k, v in CATALOG.items() if v.kind != "forecast"}
    options = default_options(catalog)
    assert options[CONF_FORECAST_ENABLED] is False
    assert options[CONF_OCR_ENABLED] is True


def test_selector_lists_recommended_first_with_a_star() -> None:
    """Recommended models come first and are marked."""
    selector = model_selector(
        CATALOG, lambda m: m.kind == "speech", RECOMMENDED_MODELS["tts"]
    )
    options = selector.config["options"]
    assert [o["value"] for o in options] == ["kyutai-tts-1.6b-en-fr", "kokoro-82m"]
    assert all(o["label"].startswith("★ ") for o in options)
    plain = model_selector(CATALOG, lambda m: m.kind == "forecast")
    assert plain.config["options"][0]["label"] == "timestral-28m"


async def _create(hass: HomeAssistant, *, create_pipeline: bool) -> MockConfigEntry:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_API_KEY: "sk-new", CONF_CREATE_PIPELINE: create_pipeline},
    )
    await hass.async_block_till_done()
    return result["result"]


async def test_pipeline_created_once(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """The Blade pipeline wires the entry's own STT, agent and TTS."""
    hass.config.language = "en"
    assert await async_setup_component(hass, "assist_pipeline", {})
    entry = await _create(hass, create_pipeline=True)
    assert entry.state is ConfigEntryState.LOADED
    pipeline = next(p for p in async_get_pipelines(hass) if p.name == PIPELINE_NAME)
    assert pipeline.conversation_engine == "conversation.blade_conversation"
    assert pipeline.stt_engine == "stt.blade_stt"
    assert pipeline.tts_engine == "tts.blade_tts"
    assert CONF_CREATE_PIPELINE not in entry.data

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    names = [p.name for p in async_get_pipelines(hass)]
    assert names.count(PIPELINE_NAME) == 1


async def test_reinstall_reuses_pipeline(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """Removing and adding the integration again keeps a single Blade pipeline."""
    hass.config.language = "en"
    assert await async_setup_component(hass, "assist_pipeline", {})
    first = await _create(hass, create_pipeline=True)
    await hass.config_entries.async_remove(first.entry_id)
    await hass.async_block_till_done()
    await _create(hass, create_pipeline=True)
    blade = [p for p in async_get_pipelines(hass) if p.name == PIPELINE_NAME]
    assert len(blade) == 1
    assert blade[0].tts_engine == "tts.blade_tts"


async def test_pipeline_not_requested(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """Unticking the box leaves the pipelines alone."""
    assert await async_setup_component(hass, "assist_pipeline", {})
    await _create(hass, create_pipeline=False)
    assert PIPELINE_NAME not in [p.name for p in async_get_pipelines(hass)]


async def test_pipeline_skipped_without_assist(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """Without assist_pipeline loaded, setup still succeeds and drops the flag."""
    entry = await _create(hass, create_pipeline=True)
    assert entry.state is ConfigEntryState.LOADED
    assert CONF_CREATE_PIPELINE not in entry.data


async def test_pipeline_skipped_without_voice_entities(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """No speech model on the account: no TTS entity, so no pipeline."""
    mock_client.list_models.return_value = {
        k: v for k, v in CATALOG.items() if v.kind != "speech"
    }
    assert await async_setup_component(hass, "assist_pipeline", {})
    await _create(hass, create_pipeline=True)
    assert PIPELINE_NAME not in [p.name for p in async_get_pipelines(hass)]


async def test_pipeline_language_unsupported(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """When HA refuses the engines for the language, nothing is updated."""
    assert await async_setup_component(hass, "assist_pipeline", {})
    with (
        patch(
            "homeassistant.components.assist_pipeline.async_create_default_pipeline",
            return_value=None,
        ),
        patch(
            "homeassistant.components.assist_pipeline.async_update_pipeline"
        ) as update,
    ):
        await _create(hass, create_pipeline=True)
    update.assert_not_called()


def test_default_prompt_follows_language() -> None:
    """French users get French instructions; others keep Home Assistant's."""
    assert default_prompt("fr").startswith("Tu es un assistant vocal")
    assert default_prompt("fr-CA") == default_prompt("fr")
    assert default_prompt("de") == llm.DEFAULT_INSTRUCTIONS_PROMPT
    data = _by_type(default_subentries(CATALOG, "fr"))
    assert data["conversation"]["prompt"] == default_prompt("fr")
    assert _by_type(default_subentries(CATALOG))["conversation"]["prompt"] == (
        llm.DEFAULT_INSTRUCTIONS_PROMPT
    )


async def test_new_agent_prompt_in_french(
    hass: HomeAssistant, mock_client: MagicMock
) -> None:
    """An agent added later is also pre-filled in the user's language."""
    hass.config.language = "fr"
    entry = await _create(hass, create_pipeline=False)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_CONVERSATION), context={"source": SOURCE_USER}
    )
    prompt = next(f for f in result["data_schema"].schema if str(f) == "prompt")
    assert prompt.description["suggested_value"] == default_prompt("fr")
