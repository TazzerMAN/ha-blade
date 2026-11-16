"""User-defined tools in the Extended OpenAI Conversation format.

Lets users paste the ``functions`` YAML they already wrote for that
integration: ``native`` (execute_service), ``script`` and ``template``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, override

import probatio
import yaml  # type: ignore[import-untyped]
from homeassistant.components.homeassistant.exposed_entities import (
    async_should_expose,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import llm
from homeassistant.helpers.script import Script
from homeassistant.helpers.template import Template

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.util.json import JsonObjectType

CONVERSATION_ASSISTANT: Final = "conversation"
NATIVE_FUNCTIONS: Final = frozenset({"execute_service", "execute_service_single"})
FUNCTION_TYPES: Final = frozenset({"native", "script", "template"})
# Script variable an Extended OpenAI script sets to return a value.
RESULT_VARIABLE: Final = "_function_result"


class FunctionsError(ValueError):
    """The functions YAML cannot be used."""


@dataclass(slots=True, frozen=True)
class FunctionSpec:
    """One parsed function: what the model sees and how it runs."""

    name: str
    description: str
    parameters: dict[str, Any]
    kind: str
    config: dict[str, Any]


def parse_functions(text: str | None) -> list[FunctionSpec]:
    """Parse and check the YAML list; raise FunctionsError on any problem."""
    if not text or not text.strip():
        return []
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise FunctionsError(f"YAML: {err}") from err
    if not isinstance(raw, list):
        raise FunctionsError("a list of functions is expected")
    specs = [_parse_one(index, item) for index, item in enumerate(raw)]
    if len({spec.name for spec in specs}) != len(specs):
        raise FunctionsError("function names must be unique")
    return specs


def _parse_one(index: int, item: Any) -> FunctionSpec:
    """Check one ``{spec, function}`` item."""
    where = f"function {index + 1}"
    spec = item.get("spec") if isinstance(item, dict) else None
    function = item.get("function") if isinstance(item, dict) else None
    if not isinstance(spec, dict) or not isinstance(function, dict):
        raise FunctionsError(f"{where}: spec and function are expected")
    name = spec.get("name")
    if not isinstance(name, str) or not name:
        raise FunctionsError(f"{where}: spec.name is missing")
    _check_function(name, function)
    parameters = spec.get("parameters") or {"type": "object", "properties": {}}
    if not isinstance(parameters, dict):
        raise FunctionsError(f"{name}: parameters must be an object")
    return FunctionSpec(
        name=name,
        description=str(spec.get("description") or ""),
        parameters=parameters,
        kind=function["type"],
        config=function,
    )


def _check_function(name: str, function: dict[str, Any]) -> None:
    """Check the ``function`` part against the supported types."""
    kind = function.get("type")
    if kind not in FUNCTION_TYPES:
        raise FunctionsError(f"{name}: unsupported type {kind!r}")
    if kind == "native" and function.get("name") not in NATIVE_FUNCTIONS:
        raise FunctionsError(
            f"{name}: unsupported native function {function.get('name')!r}"
        )
    if kind == "script" and not isinstance(function.get("sequence"), list):
        raise FunctionsError(f"{name}: script needs a sequence")
    if kind == "template" and not isinstance(function.get("value_template"), str):
        raise FunctionsError(f"{name}: template needs a value_template")


class FunctionTool(llm.Tool):
    """A user function exposed to the model as a tool."""

    integration = DOMAIN
    # Arguments follow the user's JSON schema, checked by the model's decoder.
    parameters = probatio.Schema({}, extra=probatio.ALLOW_EXTRA)

    def __init__(self, spec: FunctionSpec) -> None:
        """Keep the spec; the raw JSON schema is sent as is."""
        self.spec = spec
        self.name = spec.name
        self.description = spec.description
        self.json_schema = spec.parameters

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> llm.ToolResult:
        """Run the function and wrap what it returns."""
        args = tool_input.tool_args
        try:
            match self.spec.kind:
                case "native":
                    data = await _execute_services(hass, self.spec, args, llm_context)
                case "script":
                    data = await _run_script(hass, self.spec, args, llm_context)
                case _:
                    data = Template(
                        self.spec.config["value_template"], hass
                    ).async_render(args, parse_result=False)
        except (HomeAssistantError, ValueError, TypeError) as err:
            return llm.ToolResult(data={"error": str(err)}, error=True)
        return llm.ToolResult(data={"result": data})


async def _execute_services(
    hass: HomeAssistant,
    spec: FunctionSpec,
    args: dict[str, Any],
    llm_context: llm.LLMContext,
) -> str:
    """Call services on exposed entities only, like Extended OpenAI."""
    calls = args.get("list") if spec.config["name"] == "execute_service" else [args]
    if not isinstance(calls, list) or not calls:
        raise ValueError("no service to call")
    for call in calls:
        domain, service = call.get("domain"), call.get("service")
        data = dict(call.get("service_data") or {})
        if not isinstance(domain, str) or not isinstance(service, str):
            raise TypeError("domain and service are required")
        entity_ids = cv.ensure_list(data.get("entity_id"))
        if not entity_ids:
            raise ValueError("service_data.entity_id is required")
        for entity_id in entity_ids:
            if not async_should_expose(hass, CONVERSATION_ASSISTANT, entity_id):
                raise ValueError(f"{entity_id} is not exposed to Assist")
        try:
            await hass.services.async_call(
                domain, service, data, blocking=True, context=llm_context.context
            )
        except ServiceNotFound as err:
            raise ValueError(f"service {domain}.{service} does not exist") from err
    return "Success"


async def _run_script(
    hass: HomeAssistant,
    spec: FunctionSpec,
    args: dict[str, Any],
    llm_context: llm.LLMContext,
) -> Any:
    """Run the sequence; its _function_result variable is the answer."""
    script = Script(hass, cv.SCRIPT_SCHEMA(spec.config["sequence"]), spec.name, DOMAIN)
    result = await script.async_run(run_variables=args, context=llm_context.context)
    if result is None:
        return "Success"
    return result.variables.get(RESULT_VARIABLE, "Success")


class FunctionsAPI(llm.API):
    """The user functions of one conversation agent, as an LLM API."""

    def __init__(
        self, hass: HomeAssistant, api_id: str, specs: list[FunctionSpec]
    ) -> None:
        """Wrap the parsed functions."""
        super().__init__(hass=hass, id=api_id, name="Blade functions")
        self.specs = specs

    @override
    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Expose one tool per function."""
        return llm.APIInstance(
            api=self,
            api_prompt="",
            llm_context=llm_context,
            tools=[FunctionTool(spec) for spec in self.specs],
        )


def exposed_entities(hass: HomeAssistant) -> list[JsonObjectType]:
    """Entities exposed to Assist, as Extended OpenAI prompts expect them."""
    registry = er.async_get(hass)
    entities: list[JsonObjectType] = []
    for state in hass.states.async_all():
        if not async_should_expose(hass, CONVERSATION_ASSISTANT, state.entity_id):
            continue
        entry = registry.async_get(state.entity_id)
        entities.append(
            {
                "entity_id": state.entity_id,
                "name": state.name,
                "state": state.state,
                # A non-string alias stands for the computed entity name.
                "aliases": [a for a in entry.aliases if isinstance(a, str)]
                if entry
                else [],
            }
        )
    return entities
