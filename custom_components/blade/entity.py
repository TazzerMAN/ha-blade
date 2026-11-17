"""Shared base entity and chat loop for Blade."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from mimetypes import guess_file_type
from pathlib import Path
from typing import TYPE_CHECKING, Any

import probatio
from homeassistant.components import conversation
from homeassistant.const import CONF_MODEL
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import llm
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.json import json_dumps
from homeassistant.util import slugify

from .api import BladeApiError, BladeError, BladeModel, StreamDelta, current_service
from .const import (
    CONF_CODE_INTERPRETER,
    CONF_MAX_TOKENS,
    CONF_REASONING_EFFORT,
    CONF_RECOMMENDED,
    CONF_TEMPERATURE,
    CONF_WEB_FETCH,
    CONF_WEB_SEARCH,
    DOMAIN,
    MAX_TOOL_ITERATIONS,
    RECOMMENDED_MAX_TOKENS,
    RECOMMENDED_TEMPERATURE,
)
from .functions import FunctionTool

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant

    from . import BladeConfigEntry

# Keywords some serving stacks reject inside function parameter schemas.
_UNSUPPORTED_TOOL_KEYS = frozenset({"oneOf", "anyOf", "allOf"})
_IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")


def blade_error(err: BladeError, model: str) -> HomeAssistantError:
    """Build the Home Assistant error shown for a failed Blade call."""
    if isinstance(err, BladeApiError) and err.cold_start:
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="model_starting",
            translation_placeholders={"model": model},
        )
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="api_error",
        translation_placeholders={"error": str(err)},
    )


def format_tool(
    tool: llm.Tool, custom_serializer: Callable[[Any], Any] | None
) -> dict[str, Any]:
    """Convert a Home Assistant tool to an OpenAI function tool."""
    inner = tool
    while isinstance(inner, llm.NamespacedTool):
        inner = inner.tool
    if isinstance(inner, FunctionTool):
        schema = inner.json_schema
    else:
        schema = probatio.to_openapi(
            tool.parameters,
            custom_serializer=custom_serializer,
            openapi_version="3.1.0",
        )
    function: dict[str, Any] = {
        "name": tool.name,
        "parameters": {
            key: value
            for key, value in schema.items()
            if key not in _UNSUPPORTED_TOOL_KEYS
        },
    }
    if tool.description:
        function["description"] = tool.description
    return {"type": "function", "function": function}


def format_structure(
    name: str, schema: probatio.Schema, llm_api: llm.APIInstance | None
) -> dict[str, Any]:
    """Build a json_schema response_format from an AI task structure."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": slugify(name)[:64],
            "schema": probatio.to_openapi(
                schema,
                custom_serializer=(
                    llm_api.custom_serializer if llm_api else llm.selector_serializer
                ),
                openapi_version="3.1.0",
            ),
        },
    }


def content_to_message(content: conversation.Content) -> dict[str, Any] | None:
    """Convert chat log content to a chat completions message."""
    if isinstance(content, conversation.ToolResultContent):
        return {
            "role": "tool",
            "tool_call_id": content.tool_call_id,
            "content": json_dumps(
                {"data": content.result.data, "error": content.result.error}
            ),
        }
    if content.role in ("system", "user"):
        return {"role": content.role, "content": content.content or ""}
    if isinstance(content, conversation.AssistantContent):
        message: dict[str, Any] = {"role": "assistant", "content": content.content}
        if content.tool_calls:
            message["tool_calls"] = [
                {
                    "type": "function",
                    "id": call.id,
                    "function": {
                        "name": call.tool_name,
                        "arguments": json_dumps(call.tool_args),
                    },
                }
                for call in content.tool_calls
            ]
        return message
    return None


def _decode_arguments(arguments: str) -> Any:
    if not arguments:
        return {}
    try:
        return json.loads(arguments)
    except json.JSONDecodeError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="tool_args_parse_error",
            translation_placeholders={"error": str(err)},
        ) from err


async def transform_stream(
    deltas: AsyncIterator[StreamDelta],
) -> AsyncGenerator[conversation.AssistantContentDeltaDict]:
    """Turn Blade stream deltas into chat log deltas."""
    role_sent = False
    received = False
    calls: dict[int, dict[str, str]] = {}
    async for delta in deltas:
        received = True
        data: conversation.AssistantContentDeltaDict = {}
        if not role_sent:
            role_sent = True
            data["role"] = "assistant"
        if delta.content:
            data["content"] = delta.content
        if delta.reasoning:
            data["thinking_content"] = delta.reasoning
        for call in delta.tool_calls:
            current = calls.setdefault(
                call.get("index", 0), {"id": "", "name": "", "arguments": ""}
            )
            current["id"] = call.get("id") or current["id"]
            function = call.get("function") or {}
            current["name"] = function.get("name") or current["name"]
            current["arguments"] += function.get("arguments") or ""
        if data:
            yield data
    if calls:
        yield {
            "tool_calls": [
                llm.ToolInput(
                    id=call["id"],
                    tool_name=call["name"],
                    tool_args=_decode_arguments(call["arguments"]),
                )
                for _, call in sorted(calls.items())
            ]
        }
    if not received:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="empty_response"
        )


async def async_image_parts(
    hass: HomeAssistant, files: list[tuple[Path, str | None]]
) -> list[dict[str, Any]]:
    """Encode image attachments as image_url content parts."""

    def encode() -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        for path, mime_type in files:
            if not path.exists():
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="file_not_found",
                    translation_placeholders={"file_path": str(path)},
                )
            mime = (mime_type or guess_file_type(path)[0] or "").split(";")[0]
            mime = "image/jpeg" if mime == "image/jpg" else mime
            if mime not in _IMAGE_MIME_TYPES:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="unsupported_file_type",
                    translation_placeholders={"file_path": str(path)},
                )
            encoded = base64.b64encode(path.read_bytes()).decode()
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                }
            )
        return parts

    return await hass.async_add_executor_job(encode)


class BladeEntity(Entity):
    """Base entity bound to one Blade subentry."""

    _attr_has_entity_name = True
    _attr_name: str | None = None

    def __init__(self, entry: BladeConfigEntry, subentry: ConfigSubentry) -> None:
        """Link the entity to its subentry and service device."""
        self.entry = entry
        self.subentry = subentry
        self.model_id: str = subentry.data[CONF_MODEL]
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="Blade",
            model=self.model_id,
            entry_type=dr.DeviceEntryType.SERVICE,
            configuration_url="https://console.blade.sh",
        )

    @property
    def model(self) -> BladeModel | None:
        """Return the catalog entry of the configured model, if listed."""
        return self.entry.runtime_data.models.get(self.model_id)

    def _model_args(self, chat_log: conversation.ChatLog) -> dict[str, Any]:
        """Build the request body shared by every chat round."""
        options = self.subentry.data
        args: dict[str, Any] = {
            "model": self.model_id,
            "user": chat_log.conversation_id,
            # Entries saved before the int coercion may hold a float.
            "max_tokens": int(options.get(CONF_MAX_TOKENS, RECOMMENDED_MAX_TOKENS)),
        }
        if not options.get(CONF_RECOMMENDED, True):
            args["temperature"] = options.get(CONF_TEMPERATURE, RECOMMENDED_TEMPERATURE)
            model = self.model
            effort = options.get(CONF_REASONING_EFFORT)
            if effort and model and "reasoning_effort" in model.supported_parameters:
                args["reasoning_effort"] = effort

        tools: list[dict[str, Any]] = []
        if chat_log.llm_api:
            tools.extend(
                format_tool(tool, chat_log.llm_api.custom_serializer)
                for tool in chat_log.llm_api.tools
            )
        # Server-side tools run and are billed by Blade; each is opt-in.
        tools.extend(
            {"type": tool_type}
            for option, tool_type in (
                (CONF_WEB_SEARCH, "web_search"),
                (CONF_WEB_FETCH, "web_fetch"),
                (CONF_CODE_INTERPRETER, "code_interpreter"),
            )
            if options.get(option)
        )
        if tools:
            args["tools"] = tools
        return args

    async def _async_handle_chat_log(
        self,
        chat_log: conversation.ChatLog,
        structure_name: str | None = None,
        structure: probatio.Schema | None = None,
    ) -> None:
        """Run the model over the chat log until no tool call is left."""
        args = self._model_args(chat_log)
        messages = [
            message
            for content in chat_log.content
            if (message := content_to_message(content)) is not None
        ]

        last = chat_log.content[-1]
        if last.role == "user" and last.attachments:
            model = self.model
            if model is not None and not model.supports_vision:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="model_without_vision",
                    translation_placeholders={"model": self.model_id},
                )
            parts = await async_image_parts(
                self.hass, [(a.path, a.mime_type) for a in last.attachments]
            )
            messages[-1]["content"] = [
                {"type": "text", "text": messages[-1]["content"]},
                *parts,
            ]

        if structure is not None and structure_name is not None:
            args["response_format"] = format_structure(
                structure_name, structure, chat_log.llm_api
            )

        client = self.entry.runtime_data.client
        current_service.set(self.subentry.title)
        for _ in range(MAX_TOOL_ITERATIONS):
            try:
                messages.extend(
                    [
                        message
                        async for content in chat_log.async_add_delta_content_stream(
                            self.entity_id,
                            transform_stream(
                                client.chat_stream({**args, "messages": messages})
                            ),
                        )
                        if (message := content_to_message(content)) is not None
                    ]
                )
            except BladeError as err:
                raise blade_error(err, self.model_id) from err
            if not chat_log.unresponded_tool_results:
                break
