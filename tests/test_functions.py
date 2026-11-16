"""Tests for Extended OpenAI compatible custom functions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm
from pytest_homeassistant.common import MockConfigEntry, async_mock_service

from custom_components.blade.api import StreamDelta
from custom_components.blade.const import (
    CONF_FUNCTIONS,
    RECOMMENDED_CONVERSATION_OPTIONS,
    SUBENTRY_CONVERSATION,
)
from custom_components.blade.functions import (
    FunctionsError,
    FunctionTool,
    exposed_entities,
    parse_functions,
)

from .conftest import TEST_API, script_chat, subentry_id, text_round, tool_call

ENTITY_ID = "conversation.blade_conversation"
GLADOS_YAML = (
    Path(__file__).parent / "fixtures" / "extended_openai_functions.yaml"
).read_text()

TEMPLATE_FN = """
- spec:
    name: shout
    description: Upper-case a word
    parameters:
      type: object
      properties:
        word: {type: string}
  function:
    type: template
    value_template: "{{ word | upper }}"
"""
SCRIPT_FN = """
- spec:
    name: double
    parameters:
      type: object
      properties:
        n: {type: integer}
  function:
    type: script
    sequence:
      - variables:
          _function_result: "{{ n * 2 }}"
- spec:
    name: noop
  function:
    type: script
    sequence:
      - variables:
          other: 1
"""
NATIVE_FN = """
- spec:
    name: execute_services
    parameters: {type: object}
  function:
    type: native
    name: execute_service
- spec:
    name: execute_one
    parameters: {type: object}
  function:
    type: native
    name: execute_service_single
"""


async def ask(
    hass: HomeAssistant, text: str = "hello"
) -> conversation.ConversationResult:
    """Send one utterance to the Blade agent."""
    return await conversation.async_converse(
        hass, text, None, Context(), agent_id=ENTITY_ID
    )


def call_round(name: str, args: dict[str, Any]) -> list[StreamDelta]:
    """Return a stream that calls one tool."""
    return [
        StreamDelta(
            role="assistant",
            tool_calls=[tool_call(0, "call_1", name, json.dumps(args))],
        ),
        StreamDelta(finish_reason="tool_calls"),
    ]


def tool_result(body: dict[str, Any]) -> dict[str, Any]:
    """Return the decoded content of the last tool message sent to the model."""
    message = next(m for m in reversed(body["messages"]) if m["role"] == "tool")
    return json.loads(message["content"])


# ----------------------------------------------------------------- Parsing


def test_parse_real_extended_openai_config() -> None:
    """The YAML of an existing Extended OpenAI agent parses as is."""
    specs = parse_functions(GLADOS_YAML)
    assert [s.name for s in specs] == [
        "execute_services",
        "add_items_to_list",
        "get_list_items",
        "search_entities",
        "get_entity_details",
        "get_weather_forecast",
    ]
    assert {s.kind for s in specs} == {"native", "script", "template"}


@pytest.mark.parametrize("text", [None, "", "   \n"])
def test_parse_empty(text: str | None) -> None:
    """No text means no functions."""
    assert parse_functions(text) == []


def test_parse_default_parameters() -> None:
    """A function without parameters gets an empty object schema."""
    spec = parse_functions(SCRIPT_FN)[1]
    assert spec.parameters == {"type": "object", "properties": {}}
    assert spec.description == ""


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("a: [", "YAML"),
        ("spec: {}", "list of functions"),
        ("- 3", "spec and function"),
        ("- {spec: {name: a}}", "spec and function"),
        ("- {spec: {}, function: {type: template}}", "spec.name"),
        ("- {spec: {name: a}, function: {type: rest}}", "unsupported type"),
        (
            "- {spec: {name: a}, function: {type: native, name: get_energy}}",
            "unsupported native",
        ),
        ("- {spec: {name: a}, function: {type: script}}", "sequence"),
        ("- {spec: {name: a}, function: {type: template}}", "value_template"),
        (
            (
                "- {spec: {name: a, parameters: 3}, "
                "function: {type: template, value_template: x}}"
            ),
            "parameters",
        ),
        (
            (
                "- {spec: {name: a}, function: {type: template, value_template: x}}\n"
                "- {spec: {name: a}, function: {type: template, value_template: y}}"
            ),
            "unique",
        ),
    ],
)
def test_parse_errors(text: str, message: str) -> None:
    """Each malformed configuration explains itself."""
    with pytest.raises(FunctionsError, match=message):
        parse_functions(text)


# ------------------------------------------------------------ Conversation


@pytest.fixture
def conversation_data() -> dict[str, Any]:
    """Agent with only the custom functions, like Extended OpenAI."""
    data = dict(RECOMMENDED_CONVERSATION_OPTIONS)
    data.pop(CONF_LLM_HASS_API)
    data[CONF_FUNCTIONS] = TEMPLATE_FN + SCRIPT_FN + NATIVE_FN
    return data


async def test_functions_sent_with_raw_schema(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The user's JSON schema reaches the model untouched."""
    bodies = script_chat(mock_client, text_round("ok"))
    await ask(hass)
    tools = {t["function"]["name"]: t["function"] for t in bodies[0]["tools"]}
    assert set(tools) == {"shout", "double", "noop", "execute_services", "execute_one"}
    assert tools["shout"]["parameters"]["properties"] == {"word": {"type": "string"}}
    assert tools["shout"]["description"] == "Upper-case a word"
    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.attributes["supported_features"] == (
        conversation.ConversationEntityFeature.CONTROL
    )


async def test_template_function(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A template function renders with the call arguments."""
    bodies = script_chat(
        mock_client, call_round("shout", {"word": "glados"}), text_round("done")
    )
    await ask(hass)
    assert tool_result(bodies[1]) == {"data": {"result": "GLADOS"}, "error": False}


@pytest.mark.parametrize(
    ("name", "args", "expected"), [("double", {"n": 21}, 42), ("noop", {}, "Success")]
)
async def test_script_function(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    name: str,
    args: dict[str, Any],
    expected: object,
) -> None:
    """A script returns its _function_result, or Success."""
    bodies = script_chat(mock_client, call_round(name, args), text_round("done"))
    await ask(hass)
    assert tool_result(bodies[1])["data"] == {"result": expected}


async def test_script_without_result(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A stopped script run (None result) still reports success."""
    tool = FunctionTool(parse_functions(SCRIPT_FN)[0])
    context = llm.LLMContext(
        platform="blade",
        context=Context(),
        language="en",
        assistant=None,
        device_id=None,
    )
    with pytest.MonkeyPatch.context() as patch:

        async def none_run(*_: Any, **__: Any) -> None:
            return None

        patch.setattr("custom_components.blade.functions.Script.async_run", none_run)
        result = await tool.async_call(
            hass, llm.ToolInput(tool_name="double", tool_args={"n": 1}), context
        )
    assert result.data == {"result": "Success"}


async def test_native_execute_services(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Services run on exposed entities, several in one call."""
    calls: list[ServiceCall] = async_mock_service(hass, "light", "turn_on")
    hass.states.async_set("light.salon", "off")
    async_expose_entity(hass, "conversation", "light.salon", should_expose=True)
    bodies = script_chat(
        mock_client,
        call_round(
            "execute_services",
            {
                "list": [
                    {
                        "domain": "light",
                        "service": "turn_on",
                        "service_data": {"entity_id": "light.salon"},
                    }
                ]
            },
        ),
        call_round(
            "execute_one",
            {
                "domain": "light",
                "service": "turn_on",
                "service_data": {"entity_id": ["light.salon"]},
            },
        ),
        text_round("done"),
    )
    await ask(hass)
    assert len(calls) == 2
    assert tool_result(bodies[1])["data"] == {"result": "Success"}
    assert tool_result(bodies[2])["data"] == {"result": "Success"}


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"list": []}, "no service"),
        ({"list": [{"service": "turn_on"}]}, "domain and service"),
        (
            {"list": [{"domain": "light", "service": "turn_on", "service_data": {}}]},
            "entity_id is required",
        ),
        (
            {
                "list": [
                    {
                        "domain": "lock",
                        "service": "unlock",
                        "service_data": {"entity_id": "lock.door"},
                    }
                ]
            },
            "not exposed",
        ),
        (
            {
                "list": [
                    {
                        "domain": "light",
                        "service": "explode",
                        "service_data": {"entity_id": "light.salon"},
                    }
                ]
            },
            "does not exist",
        ),
    ],
)
async def test_native_refusals(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    args: dict[str, Any],
    message: str,
) -> None:
    """Unexposed entities, missing fields and unknown services are refused."""
    hass.states.async_set("light.salon", "off")
    hass.states.async_set("lock.door", "locked")
    async_expose_entity(hass, "conversation", "light.salon", should_expose=True)
    bodies = script_chat(
        mock_client, call_round("execute_services", args), text_round("no")
    )
    await ask(hass)
    result = tool_result(bodies[1])
    assert result["error"] is True
    assert message in result["data"]["error"]


async def test_prompt_exposed_entities(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Extended OpenAI prompt variables render, and HA leaves the result alone."""
    registry = er.async_get(hass)
    salon = registry.async_get_or_create("light", "test", "salon")
    registry.async_update_entity(salon.entity_id, aliases=["lampe"])
    hass.states.async_set(salon.entity_id, "on")
    hass.states.async_set("light.hidden", "on")
    async_expose_entity(hass, "conversation", salon.entity_id, should_expose=True)
    async_expose_entity(hass, "conversation", "light.hidden", should_expose=False)
    subentry = mock_config_entry.subentries[
        subentry_id(mock_config_entry, SUBENTRY_CONVERSATION)
    ]
    hass.config_entries.async_update_subentry(
        mock_config_entry,
        subentry,
        data={
            **subentry.data,
            CONF_PROMPT: "{% for e in exposed_entities %}{{ e.entity_id }}="
            "{{ e.aliases | join('/') }};{% endfor %}{{ '{{literal}}' }}",
        },
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    bodies = script_chat(mock_client, text_round("ok"))
    await ask(hass)
    system = bodies[0]["messages"][0]["content"]
    assert f"{salon.entity_id}=lampe;{{{{literal}}}}" in system
    assert "light.hidden" not in system


async def test_prompt_error_answers_politely(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A broken Extended OpenAI prompt gives Home Assistant's error, no crash."""
    subentry = mock_config_entry.subentries[
        subentry_id(mock_config_entry, SUBENTRY_CONVERSATION)
    ]
    hass.config_entries.async_update_subentry(
        mock_config_entry,
        subentry,
        data={
            **subentry.data,
            CONF_PROMPT: "{{ exposed_entities | length }} {{ as_local(none) }}",
        },
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    result = await ask(hass)
    assert result.response.error_code is not None
    mock_client.chat_stream.assert_not_called()


async def test_exposed_entities_without_registry_entry(hass: HomeAssistant) -> None:
    """A state with no registry entry has no aliases."""
    hass.states.async_set("sensor.loose", "1")
    async_expose_entity(hass, "conversation", "sensor.loose", should_expose=True)
    assert exposed_entities(hass) == [
        {"entity_id": "sensor.loose", "name": "loose", "state": "1", "aliases": []}
    ]


async def test_functions_merged_with_assist(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    echo_tool: Any,
) -> None:
    """Custom functions add to the selected Home Assistant API."""
    subentry = mock_config_entry.subentries[
        subentry_id(mock_config_entry, SUBENTRY_CONVERSATION)
    ]
    hass.config_entries.async_update_subentry(
        mock_config_entry,
        subentry,
        data={**subentry.data, CONF_LLM_HASS_API: [TEST_API]},
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    bodies = script_chat(mock_client, text_round("ok"))
    await ask(hass)
    names = {t["function"]["name"] for t in bodies[0]["tools"]}
    assert {"echo", "shout", "execute_services"} <= names


ECHO_FN = """
- spec:
    name: echo
    parameters: {type: object, properties: {text: {type: string}}}
  function:
    type: template
    value_template: "{{ text }}"
"""


async def test_function_named_like_an_assist_tool(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    echo_tool: Any,
) -> None:
    """A clash is namespaced, and the user schema survives the wrapping."""
    subentry = mock_config_entry.subentries[
        subentry_id(mock_config_entry, SUBENTRY_CONVERSATION)
    ]
    hass.config_entries.async_update_subentry(
        mock_config_entry,
        subentry,
        data={**subentry.data, CONF_LLM_HASS_API: [TEST_API], CONF_FUNCTIONS: ECHO_FN},
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    bodies = script_chat(mock_client, text_round("ok"))
    await ask(hass)
    tools = {t["function"]["name"]: t["function"] for t in bodies[0]["tools"]}
    custom = next(
        f for n, f in tools.items() if n.endswith("__echo") and "test-api" not in n
    )
    assert custom["parameters"] == {
        "type": "object",
        "properties": {"text": {"type": "string"}},
    }


# ----------------------------------------------------------------- Config flow


async def test_flow_rejects_bad_functions_then_accepts(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Invalid YAML is reported on the field; a fix saves it."""
    entry = init_integration
    sub_id = subentry_id(entry, SUBENTRY_CONVERSATION)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_CONVERSATION),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": sub_id},
    )
    data = {k: v for k, v in entry.subentries[sub_id].data.items() if k != "model"}
    data["model"] = "gemma-4-26b-a4b"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**data, CONF_FUNCTIONS: "- 3"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_FUNCTIONS: "invalid_functions"}
    assert "spec and function" in result["description_placeholders"]["error"]

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**data, CONF_FUNCTIONS: GLADOS_YAML}
    )
    assert result["type"] is FlowResultType.ABORT
    assert entry.subentries[sub_id].data[CONF_FUNCTIONS] == GLADOS_YAML


async def test_flow_drops_empty_functions(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Clearing the field removes the key."""
    entry = init_integration
    sub_id = subentry_id(entry, SUBENTRY_CONVERSATION)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_CONVERSATION),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": sub_id},
    )
    data = dict(entry.subentries[sub_id].data)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**data, CONF_FUNCTIONS: ""}
    )
    assert result["type"] is FlowResultType.ABORT
    assert CONF_FUNCTIONS not in entry.subentries[sub_id].data
