"""Tests for the Blade conversation agent and the shared chat loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import probatio
import pytest
from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import DATA_CHAT_LOGS
from homeassistant.const import CONF_LLM_HASS_API, CONF_MODEL, CONF_PROMPT
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import chat_session, intent, llm
from pytest_homeassistant.common import MockConfigEntry

from custom_components.blade.api import BladeApiError, StreamDelta
from custom_components.blade.const import (
    CONF_CODE_INTERPRETER,
    CONF_MAX_TOKENS,
    CONF_REASONING_EFFORT,
    CONF_RECOMMENDED,
    CONF_TEMPERATURE,
    CONF_WEB_FETCH,
    CONF_WEB_SEARCH,
    DOMAIN,
)
from custom_components.blade.entity import (
    BladeEntity,
    content_to_message,
    format_structure,
    format_tool,
    transform_stream,
)

from .conftest import TEST_API, EchoTool, script_chat, text_round, tool_call

ENTITY_ID = "conversation.blade_conversation"
NO_API = {CONF_MODEL: "gemma-4-26b-a4b", CONF_RECOMMENDED: True}


async def ask(
    hass: HomeAssistant, text: str = "hello", conversation_id: str | None = None
) -> conversation.ConversationResult:
    """Send one utterance to the Blade agent."""
    return await conversation.async_converse(
        hass, text, conversation_id, Context(), agent_id=ENTITY_ID
    )


def speech(result: conversation.ConversationResult) -> str:
    """Return the spoken answer."""
    return result.response.speech["plain"]["speech"]


@pytest.mark.parametrize("conversation_data", [NO_API])
async def test_entity_without_control(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Without an LLM API the agent cannot control devices."""
    state = hass.states.get(ENTITY_ID)
    assert state
    assert state.attributes["supported_features"] == 0
    entity = hass.data["entity_components"]["conversation"].get_entity(ENTITY_ID)
    assert entity.supported_languages == "*"


async def test_entity_with_control(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Selecting an LLM API enables the control feature."""
    state = hass.states.get(ENTITY_ID)
    assert (
        state.attributes["supported_features"]
        == conversation.ConversationEntityFeature.CONTROL
    )


@pytest.mark.parametrize("conversation_data", [{**NO_API, CONF_PROMPT: "Be brief."}])
async def test_simple_answer(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A streamed text answer is returned and the request body is complete."""
    bodies = script_chat(mock_client, text_round("Hello ", "there!"))
    result = await ask(hass, "hi", "conv-1")
    assert result.response.response_type is intent.IntentResponseType.ACTION_DONE
    assert speech(result) == "Hello there!"
    assert result.conversation_id == "conv-1"

    (body,) = bodies
    assert body["model"] == "gemma-4-26b-a4b"
    assert body["user"] == "conv-1"
    assert body["max_tokens"] == 3000
    assert "stream_options" not in body
    assert "tools" not in body
    assert "temperature" not in body
    assert body["messages"][0]["role"] == "system"
    assert "Be brief." in body["messages"][0]["content"]
    assert body["messages"][1] == {"role": "user", "content": "hi"}


@pytest.mark.parametrize(
    ("model", "options", "expected"),
    [
        (
            "gemma-4-26b-a4b",
            {CONF_TEMPERATURE: 0.2, CONF_MAX_TOKENS: 123, CONF_REASONING_EFFORT: "low"},
            {"temperature": 0.2, "max_tokens": 123, "reasoning_effort": "low"},
        ),
        # The model has no reasoning_effort parameter, so none is sent.
        (
            "gemma-4-31b",
            {CONF_TEMPERATURE: 0.9, CONF_REASONING_EFFORT: "high"},
            {"temperature": 0.9, "max_tokens": 3000},
        ),
        # A model missing from the catalog still works, without reasoning.
        (
            "not-in-catalog",
            {CONF_REASONING_EFFORT: "high"},
            {"temperature": 0.7, "max_tokens": 3000},
        ),
        # No effort chosen.
        ("gemma-4-26b-a4b", {}, {"temperature": 0.7, "max_tokens": 3000}),
        # An entry saved with a float length still sends an integer.
        ("gemma-4-26b-a4b", {CONF_MAX_TOKENS: 400.0}, {"max_tokens": 400}),
    ],
)
async def test_advanced_options(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    model: str,
    options: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    """Sampling options are sent unless the recommended settings are on."""
    sub = next(iter(mock_config_entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        mock_config_entry,
        sub,
        data={CONF_MODEL: model, CONF_RECOMMENDED: False, **options},
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    bodies = script_chat(mock_client, text_round("ok"))
    await ask(hass)
    body = bodies[0]
    for key, value in expected.items():
        assert body[key] == value
        assert type(body[key]) is type(value)
    assert ("reasoning_effort" in body) is ("reasoning_effort" in expected)


@pytest.mark.parametrize(
    ("flags", "types"),
    [
        ({}, []),
        ({CONF_WEB_SEARCH: True}, ["web_search"]),
        ({CONF_WEB_FETCH: True}, ["web_fetch"]),
        ({CONF_CODE_INTERPRETER: True}, ["code_interpreter"]),
        (
            {CONF_WEB_SEARCH: True, CONF_WEB_FETCH: True, CONF_CODE_INTERPRETER: True},
            ["web_search", "web_fetch", "code_interpreter"],
        ),
        ({CONF_WEB_SEARCH: False, CONF_WEB_FETCH: False}, []),
    ],
)
async def test_server_tools_are_opt_in(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    flags: dict[str, bool],
    types: list[str],
) -> None:
    """Server-side tools are only declared when ticked; nothing is ever called."""
    sub = next(iter(mock_config_entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        mock_config_entry, sub, data={**NO_API, **flags}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    bodies = script_chat(mock_client, text_round("ok"))
    await ask(hass)
    declared = [tool["type"] for tool in bodies[0].get("tools", [])]
    assert declared == types
    mock_client.chat.assert_not_called()


async def test_tool_loop(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    echo_tool: EchoTool,
) -> None:
    """Tool calls streamed in pieces run, feed the next round, then answer."""
    sub = next(iter(mock_config_entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        mock_config_entry,
        sub,
        data={**NO_API, CONF_LLM_HASS_API: [TEST_API], CONF_WEB_SEARCH: True},
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    first = [
        StreamDelta(role="assistant", content="Let me check."),
        StreamDelta(tool_calls=[tool_call(1, "call_b", "plain", "")]),
        StreamDelta(tool_calls=[tool_call(0, "call_a", "echo", '{"text": ')]),
        StreamDelta(tool_calls=[tool_call(0, None, None, '"hi"}')]),
        StreamDelta(finish_reason="tool_calls"),
    ]
    bodies = script_chat(mock_client, first, text_round("Done: hi"))
    result = await ask(hass, "echo hi", "conv-tools")
    assert speech(result) == "Done: hi"
    assert echo_tool.calls == [{"text": "hi"}]

    assert len(bodies) == 2
    tools = bodies[0]["tools"]
    assert tools[0] == {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Echo a text",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "text": {"type": "string"},
                    "times": {"type": "integer"},
                },
                "required": ["text"],
            },
        },
    }
    assert tools[1]["function"]["name"] == "plain"
    assert "description" not in tools[1]["function"]
    assert tools[2] == {"type": "web_search"}

    second = bodies[1]["messages"]
    assistant = second[2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "Let me check."
    assert [c["id"] for c in assistant["tool_calls"]] == ["call_a", "call_b"]
    assert assistant["tool_calls"][0]["function"] == {
        "name": "echo",
        "arguments": '{"text":"hi"}',
    }
    assert assistant["tool_calls"][1]["function"]["arguments"] == "{}"
    assert [m["role"] for m in second[3:]] == ["tool", "tool"]
    assert second[3]["tool_call_id"] == "call_a"
    assert '"echoed":"hi"' in second[3]["content"]

    # The history, tool calls included, is replayed on the next turn.
    bodies2 = script_chat(mock_client, text_round("again"))
    await ask(hass, "once more", "conv-tools")
    roles = [m["role"] for m in bodies2[0]["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "tool", "assistant", "user"]


async def test_thinking_content(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Reasoning deltas end up as thinking content in the chat log."""
    script_chat(
        mock_client,
        [
            StreamDelta(role="assistant", reasoning="Hmm. "),
            StreamDelta(reasoning="Right."),
            StreamDelta(content="42"),
        ],
    )
    await ask(hass, "meaning?", "conv-think")
    chat_log = hass.data[DATA_CHAT_LOGS]["conv-think"]
    assistant = chat_log.content[-1]
    assert isinstance(assistant, conversation.AssistantContent)
    assert assistant.thinking_content == "Hmm. Right."
    assert assistant.content == "42"


@pytest.mark.parametrize("conversation_data", [NO_API])
@pytest.mark.parametrize(
    "failure",
    [BladeApiError(500, "boom", "kaput"), BladeApiError(429, "rate", "slow")],
)
async def test_api_error(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    failure: Exception,
) -> None:
    """An API failure becomes an error answer, not an exception."""
    script_chat(mock_client, failure)
    result = await ask(hass)
    assert result.response.response_type is intent.IntentResponseType.ERROR
    assert "kaput" in speech(result) or "slow" in speech(result)


async def test_empty_stream(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A stream without any delta is reported as an empty response."""
    script_chat(mock_client, [])
    result = await ask(hass)
    assert result.response.response_type is intent.IntentResponseType.ERROR


async def test_bad_tool_arguments(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    echo_tool: EchoTool,
) -> None:
    """Unparseable tool arguments are an error, not a crash."""
    sub = next(iter(mock_config_entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        mock_config_entry, sub, data={**NO_API, CONF_LLM_HASS_API: [TEST_API]}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    script_chat(
        mock_client,
        [StreamDelta(tool_calls=[tool_call(0, "c1", "echo", "{not json")])],
    )
    result = await ask(hass)
    assert result.response.response_type is intent.IntentResponseType.ERROR
    assert echo_tool.calls == []


async def test_missing_llm_api(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """An LLM API that vanished makes the agent answer with the converse error."""
    sub = next(iter(mock_config_entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        mock_config_entry, sub, data={**NO_API, CONF_LLM_HASS_API: ["gone"]}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    result = await ask(hass)
    assert result.response.response_type is intent.IntentResponseType.ERROR
    mock_client.chat_stream.assert_not_called()


# --------------------------------------------------------------- Attachments


def entity_of(hass: HomeAssistant, entity_id: str) -> BladeEntity:
    """Return a live Blade entity object."""
    domain = entity_id.split(".", maxsplit=1)[0]
    return hass.data["entity_components"][domain].get_entity(entity_id)


async def run_with_attachment(
    hass: HomeAssistant,
    entity_id: str,
    attachment: conversation.Attachment,
) -> None:
    """Run the shared chat loop with one user attachment."""
    entity = entity_of(hass, entity_id)
    with (
        chat_session.async_get_chat_session(hass, "attach", context=None) as session,
        conversation.async_get_chat_log(hass, session) as chat_log,
    ):
        chat_log.async_add_user_content(
            conversation.UserContent("what is this?", attachments=[attachment])
        )
        await entity._async_handle_chat_log(chat_log)


@pytest.fixture
def image_file(tmp_path: Path) -> Path:
    """Write a tiny image file."""
    path = tmp_path / "photo.png"
    path.write_bytes(b"\x89PNGdata")
    return path


@pytest.mark.parametrize(
    ("mime_type", "expected"),
    [
        ("image/png", "image/png"),
        (None, "image/png"),
        ("image/png; charset=binary", "image/png"),
    ],
)
async def test_image_attachment(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    image_file: Path,
    mime_type: str | None,
    expected: str,
) -> None:
    """Images are sent as base64 data URLs next to the text."""
    bodies = script_chat(mock_client, text_round("a cat"))
    await run_with_attachment(
        hass,
        ENTITY_ID,
        conversation.Attachment("media-source://x", mime_type, image_file),
    )
    content = bodies[0]["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith(f"data:{expected};base64,")


async def test_jpg_alias(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    tmp_path: Path,
) -> None:
    """image/jpg is normalised to image/jpeg."""
    path = tmp_path / "p.jpg"
    path.write_bytes(b"jpeg")
    bodies = script_chat(mock_client, text_round("ok"))
    await run_with_attachment(
        hass, ENTITY_ID, conversation.Attachment("m", "image/jpg", path)
    )
    url = bodies[0]["messages"][-1]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")


async def test_attachment_unknown_model_is_attempted(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    image_file: Path,
) -> None:
    """A model missing from the catalog is not blocked from reading images."""
    sub = next(iter(mock_config_entry.subentries.values()))
    hass.config_entries.async_update_subentry(
        mock_config_entry, sub, data={CONF_MODEL: "not-in-catalog"}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    bodies = script_chat(mock_client, text_round("ok"))
    await run_with_attachment(
        hass, ENTITY_ID, conversation.Attachment("m", "image/png", image_file)
    )
    assert isinstance(bodies[0]["messages"][-1]["content"], list)


@pytest.mark.parametrize("conversation_data", [{CONF_MODEL: "text-only"}])
async def test_attachment_without_vision(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    image_file: Path,
) -> None:
    """A model that cannot see refuses the image before any request."""
    with pytest.raises(HomeAssistantError, match="text-only"):
        await run_with_attachment(
            hass, ENTITY_ID, conversation.Attachment("m", "image/png", image_file)
        )
    mock_client.chat_stream.assert_not_called()


async def test_attachment_missing_file(
    hass: HomeAssistant, init_integration: MockConfigEntry, tmp_path: Path
) -> None:
    """A file that disappeared is reported with its path."""
    missing = tmp_path / "gone.png"
    with pytest.raises(HomeAssistantError, match=r"gone\.png"):
        await run_with_attachment(
            hass, ENTITY_ID, conversation.Attachment("m", "image/png", missing)
        )


@pytest.mark.parametrize("mime_type", ["text/plain", None])
async def test_attachment_unsupported_type(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    tmp_path: Path,
    mime_type: str | None,
) -> None:
    """Only images are accepted, whether declared or guessed."""
    path = tmp_path / "notes.txt"
    path.write_text("hi")
    with pytest.raises(HomeAssistantError, match=r"notes\.txt"):
        await run_with_attachment(
            hass, ENTITY_ID, conversation.Attachment("m", mime_type, path)
        )


# ------------------------------------------------------------ Pure helpers


def test_content_to_message_unknown_role() -> None:
    """Content that is neither chat nor tool is skipped."""
    assert content_to_message(MagicMock(role="mystery")) is None


async def test_transform_stream_defaults() -> None:
    """Missing call fields keep the previous values and default to empty."""

    async def deltas() -> Any:
        yield StreamDelta(tool_calls=[{"function": {"name": "t"}}])
        yield StreamDelta(tool_calls=[{"function": None, "id": "x"}])

    out = [d async for d in transform_stream(deltas())]
    (call,) = out[-1]["tool_calls"]
    assert (call.id, call.tool_name, call.tool_args) == ("x", "t", {})


def test_format_tool_drops_unsupported_keywords() -> None:
    """Top-level oneOf/anyOf/allOf are removed from tool parameters."""

    class Odd(llm.Tool):
        name = "odd"
        integration = DOMAIN
        parameters = probatio.Schema({probatio.Required("a"): str})

        async def async_call(self, *args: Any) -> dict[str, Any]:
            return {}

    tool = Odd()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            "custom_components.blade.entity.probatio.to_openapi",
            lambda *_, **__: {"type": "object", "anyOf": [], "oneOf": [], "allOf": []},
        )
        assert format_tool(tool, None)["function"]["parameters"] == {"type": "object"}


def test_format_structure_names() -> None:
    """The schema name is slugified and truncated."""
    schema = probatio.Schema({probatio.Required("a"): str})
    long = format_structure("A Very Long Name " * 10, schema, None)
    assert len(long["json_schema"]["name"]) == 64
    assert format_structure("!!!", schema, None)["json_schema"]["name"] == "unknown"
