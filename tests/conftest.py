"""Shared fixtures: a mocked Blade client and a loaded config entry."""

from __future__ import annotations

import copy
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import probatio
import pytest
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.blade.api import BladeModel, StreamDelta
from custom_components.blade.const import (
    DEFAULT_ENTRY_OPTIONS,
    DOMAIN,
    RECOMMENDED_AI_TASK_OPTIONS,
    RECOMMENDED_CONVERSATION_OPTIONS,
    RECOMMENDED_EVALUATE_OPTIONS,
    RECOMMENDED_STT_OPTIONS,
    RECOMMENDED_TTS_OPTIONS,
    SUBENTRY_AI_TASK,
    SUBENTRY_CONVERSATION,
    SUBENTRY_EVALUATE,
    SUBENTRY_STT,
    SUBENTRY_TTS,
)

API_KEY = "sk-blade-secret"
TEST_API = "blade_test_api"

CATALOG: dict[str, BladeModel] = {
    model.id: model
    for model in (
        BladeModel(
            "gemma-4-26b-a4b",
            "chat",
            input_modalities=("text", "image"),
            supported_parameters=frozenset({"tools", "reasoning_effort"}),
            publisher="google",
            reasoning=True,
        ),
        BladeModel(
            "gemma-4-31b",
            "chat",
            input_modalities=("text", "image"),
            supported_parameters=frozenset({"tools"}),
            publisher="google",
        ),
        BladeModel("text-only", "chat", supported_parameters=frozenset({"tools"})),
        BladeModel("deepseek-ocr-2", "chat", input_modalities=("text", "image")),
        BladeModel("hy-mt2-7b", "chat", publisher="tencent"),
        BladeModel("voxtral-mini-3b", "chat", input_modalities=("text", "audio")),
        BladeModel("flux-2-klein", "image"),
        BladeModel("clef-flash", "decision"),
        BladeModel("whisper-large", "transcription"),
        BladeModel("kyutai-tts-1.6b-en-fr", "speech"),
        BladeModel("kokoro-82m", "speech"),
        BladeModel("timestral-28m", "forecast"),
    )
}


def make_subentries(
    conversation: dict[str, Any] | None = None,
) -> list[ConfigSubentryData]:
    """Build the five default subentries with readable titles."""
    return [
        ConfigSubentryData(
            data=conversation
            if conversation is not None
            else dict(RECOMMENDED_CONVERSATION_OPTIONS),
            subentry_type=SUBENTRY_CONVERSATION,
            subentry_id=f"{SUBENTRY_CONVERSATION}_id",
            title="Blade Conversation",
            unique_id=None,
        ),
        ConfigSubentryData(
            data=dict(RECOMMENDED_AI_TASK_OPTIONS),
            subentry_type=SUBENTRY_AI_TASK,
            subentry_id=f"{SUBENTRY_AI_TASK}_id",
            title="Blade AI Task",
            unique_id=None,
        ),
        ConfigSubentryData(
            data=dict(RECOMMENDED_EVALUATE_OPTIONS),
            subentry_type=SUBENTRY_EVALUATE,
            subentry_id=f"{SUBENTRY_EVALUATE}_id",
            title="Blade Evaluation",
            unique_id=None,
        ),
        ConfigSubentryData(
            data=dict(RECOMMENDED_STT_OPTIONS),
            subentry_type=SUBENTRY_STT,
            subentry_id=f"{SUBENTRY_STT}_id",
            title="Blade STT",
            unique_id=None,
        ),
        ConfigSubentryData(
            data=dict(RECOMMENDED_TTS_OPTIONS),
            subentry_type=SUBENTRY_TTS,
            subentry_id=f"{SUBENTRY_TTS}_id",
            title="Blade TTS",
            unique_id=None,
        ),
    ]


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    """Use a writable config directory (the TTS cache lives in it)."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return str(config_dir)


@pytest.fixture(autouse=True)
async def setup_ha(hass: HomeAssistant, tmp_path: Path) -> None:
    """Set up the base integration the Assist API relies on."""
    # Generated images must not land in the repository.
    hass.config.media_dirs = {"local": str(tmp_path / "media")}
    assert await async_setup_component(hass, "homeassistant", {})


@pytest.fixture
def mock_client() -> Iterator[MagicMock]:
    """Replace BladeClient everywhere with a mock exposing the small catalog."""
    with (
        patch("custom_components.blade.BladeClient") as client_class,
        patch("custom_components.blade.config_flow.BladeClient", new=client_class),
    ):
        client = client_class.return_value
        client.list_models = AsyncMock(return_value=dict(CATALOG))
        client.chat = AsyncMock()
        client.chat_stream = MagicMock()
        client.transcribe = AsyncMock(return_value="turn on the light")
        client.speech = AsyncMock(return_value=b"RIFFwav")
        client.speech_stream = AsyncMock()
        client.generate_image = AsyncMock(return_value=b"\x89PNG....")
        client.decide = AsyncMock()
        client.forecast = AsyncMock()
        yield client


@pytest.fixture
def conversation_data() -> dict[str, Any]:
    """Options of the conversation subentry; override per test."""
    return dict(RECOMMENDED_CONVERSATION_OPTIONS)


@pytest.fixture
def mock_config_entry(
    hass: HomeAssistant, conversation_data: dict[str, Any]
) -> MockConfigEntry:
    """Return an unloaded entry with the five default subentries."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Blade",
        data={CONF_API_KEY: API_KEY},
        options=dict(DEFAULT_ENTRY_OPTIONS),
        version=1,
        minor_version=1,
        subentries_data=make_subentries(conversation_data),
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> MockConfigEntry:
    """Load the entry."""
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


def subentry_id(entry: MockConfigEntry, subentry_type: str) -> str:
    """Return the id of the first subentry of a type."""
    return next(
        sub_id
        for sub_id, sub in entry.subentries.items()
        if sub.subentry_type == subentry_type
    )


def script_chat(
    client: MagicMock, *rounds: list[StreamDelta] | Exception
) -> list[dict[str, Any]]:
    """Make chat_stream answer each call with the next scripted round.

    Returns the list that receives a deep copy of every request body.
    """
    bodies: list[dict[str, Any]] = []
    pending = list(rounds)

    def factory(body: dict[str, Any]) -> AsyncIterator[StreamDelta]:
        bodies.append(copy.deepcopy(body))
        round_ = pending.pop(0)

        async def generate() -> AsyncGenerator[StreamDelta]:
            if isinstance(round_, Exception):
                raise round_
            for delta in round_:
                yield delta

        return generate()

    client.chat_stream.side_effect = factory
    return bodies


def text_round(*chunks: str) -> list[StreamDelta]:
    """Return a stream answering with plain text chunks."""
    return [
        StreamDelta(role="assistant" if i == 0 else None, content=chunk)
        for i, chunk in enumerate(chunks)
    ] + [StreamDelta(finish_reason="stop")]


def tool_call(
    index: int, call_id: str | None, name: str | None, arguments: str
) -> dict[str, Any]:
    """Return one tool call fragment as the API streams it."""
    fragment: dict[str, Any] = {"index": index, "function": {"arguments": arguments}}
    if call_id:
        fragment["id"] = call_id
    if name:
        fragment["function"]["name"] = name
    return fragment


type Factory = Callable[..., Any]


class EchoTool(llm.Tool):
    """A real LLM tool that records its calls."""

    name = "echo"
    description = "Echo a text"
    integration = DOMAIN
    parameters = probatio.Schema(
        {probatio.Required("text"): str, probatio.Optional("times"): int}
    )

    def __init__(self) -> None:
        """Keep the calls."""
        self.calls: list[dict[str, Any]] = []

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> llm.ToolResult:
        """Return what we were given."""
        self.calls.append(tool_input.tool_args)
        return llm.ToolResult(data={"echoed": tool_input.tool_args["text"]})


class PlainTool(llm.Tool):
    """A tool with neither description nor parameters."""

    name = "plain"
    integration = DOMAIN

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> llm.ToolResult:
        """Do nothing."""
        return llm.ToolResult(data={})


class TestAPI(llm.API):
    """An LLM API serving the two tools."""

    __test__ = False

    def __init__(self, hass: HomeAssistant, tools: list[llm.Tool]) -> None:
        """Bind the API to its tools."""
        self.hass = hass
        self.id = TEST_API
        self.name = "Test API"
        self.requires_admin = False
        self.tools = tools

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Return an instance exposing the tools."""
        return llm.APIInstance(self, "Use the tools.", llm_context, self.tools)


@pytest.fixture
def echo_tool(hass: HomeAssistant) -> EchoTool:
    """Register the test API and return its echo tool."""
    tool = EchoTool()
    llm.async_register_api(hass, TestAPI(hass, [tool, PlainTool()]))
    return tool


async def call(
    hass: HomeAssistant, service: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Call a Blade action and return its response."""
    result = await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )
    assert result is not None
    return result
