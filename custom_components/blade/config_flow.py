"""Config flow for Blade: API key, one subentry per feature, action options."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any, override

import probatio
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryData,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import (
    CONF_API_KEY,
    CONF_LLM_HASS_API,
    CONF_MODEL,
    CONF_NAME,
    CONF_PROMPT,
)
from homeassistant.core import callback
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TemplateSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import BladeAuthenticationError, BladeClient, BladeError, BladeModel
from .const import (
    CONF_CODE_INTERPRETER,
    CONF_CREATE_PIPELINE,
    CONF_FORECAST_ENABLED,
    CONF_FORECAST_MODEL,
    CONF_FUNCTIONS,
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
    DEFAULT_STT_PROMPT,
    DOMAIN,
    EXTRA_STT_MODELS,
    IMAGE_SIZES,
    KIND_CHAT,
    KIND_DECISION,
    KIND_FORECAST,
    KIND_IMAGE,
    KIND_SPEECH,
    KIND_TRANSCRIPTION,
    LOGGER,
    REASONING_EFFORTS,
    RECOMMENDED_AI_TASK_OPTIONS,
    RECOMMENDED_CONVERSATION_OPTIONS,
    RECOMMENDED_EVALUATE_OPTIONS,
    RECOMMENDED_MAX_TOKENS,
    RECOMMENDED_MODELS,
    RECOMMENDED_REASONING_EFFORT,
    RECOMMENDED_STT_OPTIONS,
    RECOMMENDED_TEMPERATURE,
    RECOMMENDED_TTS_OPTIONS,
    SPEECH_VOICES,
    STREAMING_SPEECH_MODELS,
    SUBENTRY_AI_TASK,
    SUBENTRY_CONVERSATION,
    SUBENTRY_EVALUATE,
    SUBENTRY_STT,
    SUBENTRY_TTS,
    default_name,
    default_prompt,
)
from .functions import FunctionsError, parse_functions

if TYPE_CHECKING:
    from . import BladeConfigEntry

CONSOLE_URL = "https://console.blade.sh"
API_KEY_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    }
)
STEP_USER_SCHEMA = API_KEY_SCHEMA.extend(
    {probatio.Required(CONF_CREATE_PIPELINE, default=True): BooleanSelector()}
)

type ModelFilter = Callable[[BladeModel], bool]


def is_chat(model: BladeModel) -> bool:
    """Chat models of any modality."""
    return model.kind == KIND_CHAT


def is_tool_chat(model: BladeModel) -> bool:
    """Chat models able to call Home Assistant tools."""
    return model.kind == KIND_CHAT and model.supports_tools


def is_vision_chat(model: BladeModel) -> bool:
    """Chat models that read images."""
    return model.kind == KIND_CHAT and model.supports_vision


def is_kind(kind: str) -> ModelFilter:
    """Match one catalog kind."""
    return lambda model: model.kind == kind


def is_stt(model: BladeModel) -> bool:
    """Transcription models, plus audio chat models served on that route."""
    return model.kind == KIND_TRANSCRIPTION or any(
        fnmatch(model.id, pattern) for pattern in EXTRA_STT_MODELS
    )


def _rank(model_id: str, recommended: tuple[str, ...]) -> int:
    """Position of a model in a preference list, len(list) when absent."""
    return next(
        (i for i, pattern in enumerate(recommended) if fnmatch(model_id, pattern)),
        len(recommended),
    )


def model_selector(
    models: Mapping[str, BladeModel],
    keep: ModelFilter,
    recommended: tuple[str, ...] = (),
) -> SelectSelector:
    """List matching models, recommended ones first; any id can be typed in."""
    matching = sorted(
        (m for m in models.values() if keep(m)),
        key=lambda m: (_rank(m.id, recommended), m.id),
    )
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(
                    value=model.id,
                    label=(
                        "★ " if _rank(model.id, recommended) < len(recommended) else ""
                    )
                    + (
                        f"{model.id} ({model.publisher})"
                        if model.publisher
                        else model.id
                    ),
                )
                for model in matching
            ],
            mode=SelectSelectorMode.DROPDOWN,
            custom_value=True,
        )
    )


type Choice = tuple[ModelFilter, tuple[str, ...]]

# Default services: for each model option, its filter and preference list.
DEFAULT_SUBENTRIES: list[tuple[str, Mapping[str, Any], dict[str, Choice]]] = [
    (
        SUBENTRY_CONVERSATION,
        RECOMMENDED_CONVERSATION_OPTIONS,
        {CONF_MODEL: (is_tool_chat, RECOMMENDED_MODELS["conversation"])},
    ),
    (
        SUBENTRY_AI_TASK,
        RECOMMENDED_AI_TASK_OPTIONS,
        {
            CONF_MODEL: (is_vision_chat, RECOMMENDED_MODELS["ai_task"]),
            CONF_IMAGE_MODEL: (is_kind(KIND_IMAGE), RECOMMENDED_MODELS["image"]),
        },
    ),
    (
        SUBENTRY_EVALUATE,
        RECOMMENDED_EVALUATE_OPTIONS,
        {CONF_MODEL: (is_kind(KIND_DECISION), RECOMMENDED_MODELS["evaluate"])},
    ),
    (
        SUBENTRY_STT,
        RECOMMENDED_STT_OPTIONS,
        {CONF_MODEL: (is_stt, RECOMMENDED_MODELS["stt"])},
    ),
    (
        SUBENTRY_TTS,
        RECOMMENDED_TTS_OPTIONS,
        {CONF_MODEL: (is_kind(KIND_SPEECH), RECOMMENDED_MODELS["tts"])},
    ),
]
DEFAULT_ACTIONS: dict[str, tuple[str, Choice]] = {
    CONF_OCR_MODEL: (CONF_OCR_ENABLED, (is_vision_chat, RECOMMENDED_MODELS["ocr"])),
    CONF_TRANSLATE_MODEL: (
        CONF_TRANSLATE_ENABLED,
        (is_chat, RECOMMENDED_MODELS["translate"]),
    ),
    CONF_FORECAST_MODEL: (
        CONF_FORECAST_ENABLED,
        (is_kind(KIND_FORECAST), RECOMMENDED_MODELS["forecast"]),
    ),
}


def _pick(models: Mapping[str, BladeModel], choice: Choice) -> str | None:
    """Best available model: preference list first, then any match."""
    keep, recommended = choice
    matching = [m.id for m in models.values() if keep(m)]
    return min(matching, key=lambda m: (_rank(m, recommended), m), default=None)


def default_subentries(
    models: Mapping[str, BladeModel], language: str = "en"
) -> list[ConfigSubentryData]:
    """Fit the recommended services to the account's catalog.

    A service whose main model has no match on the account is left out
    rather than created broken.
    """
    subentries: list[ConfigSubentryData] = []
    for subentry_type, recommended, filters in DEFAULT_SUBENTRIES:
        data = dict(recommended)
        if CONF_PROMPT in data:
            data[CONF_PROMPT] = default_prompt(language)
        for key, choice in filters.items():
            picked = _pick(models, choice)
            if picked is None and key == CONF_MODEL:
                break
            if picked is None:
                data[CONF_IMAGES_ENABLED] = False
            else:
                data[key] = picked
        else:
            if subentry_type == SUBENTRY_TTS:
                voices = SPEECH_VOICES.get(data[CONF_MODEL])
                if voices and data[CONF_VOICE] not in voices:
                    data[CONF_VOICE] = next(iter(voices))
                data[CONF_STREAMING] = data[CONF_MODEL] in STREAMING_SPEECH_MODELS
            subentries.append(
                ConfigSubentryData(
                    subentry_type=subentry_type,
                    data=data,
                    title=default_name(subentry_type),
                    unique_id=None,
                )
            )
    return subentries


def default_options(models: Mapping[str, BladeModel]) -> dict[str, Any]:
    """Fit the action models to the catalog; switch off what cannot run."""
    options = dict(DEFAULT_ENTRY_OPTIONS)
    for model_key, (enabled_key, choice) in DEFAULT_ACTIONS.items():
        picked = _pick(models, choice)
        if picked is None:
            options[enabled_key] = False
        else:
            options[model_key] = picked
    return options


async def validate_api_key(hass: Any, api_key: str) -> dict[str, BladeModel]:
    """Return the catalog if the key works."""
    client = BladeClient(async_get_clientsession(hass), api_key)
    return await client.list_models()


class BladeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the API key and create the default feature subentries."""

    VERSION = 1
    MINOR_VERSION = 1

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Each Blade feature is a subentry the user adds or removes."""
        return {
            SUBENTRY_CONVERSATION: ConversationFlowHandler,
            SUBENTRY_AI_TASK: AITaskFlowHandler,
            SUBENTRY_EVALUATE: EvaluateFlowHandler,
            SUBENTRY_STT: STTFlowHandler,
            SUBENTRY_TTS: TTSFlowHandler,
        }

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Toggle the Blade actions."""
        return BladeOptionsFlow()

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the API key form."""
        return await self._async_step_api_key(user_input, "user")

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after a rejected key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new key."""
        return await self._async_step_api_key(user_input, "reauth_confirm")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the key."""
        return await self._async_step_api_key(user_input, "reconfigure")

    async def _async_step_api_key(
        self, user_input: dict[str, Any] | None, step_id: str
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if self.source == SOURCE_USER:
                self._async_abort_entries_match(
                    {CONF_API_KEY: user_input[CONF_API_KEY]}
                )
            try:
                models = await validate_api_key(self.hass, user_input[CONF_API_KEY])
            except BladeAuthenticationError:
                errors["base"] = "invalid_auth"
            except BladeError:
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
                    entry = (
                        self._get_reauth_entry()
                        if self.source == SOURCE_REAUTH
                        else self._get_reconfigure_entry()
                    )
                    return self.async_update_reload_and_abort(
                        entry, data_updates=user_input
                    )
                return self.async_create_entry(
                    title="Blade",
                    data=user_input,
                    options=default_options(models),
                    subentries=default_subentries(models, self.hass.config.language),
                )
        return self.async_show_form(
            step_id=step_id,
            data_schema=STEP_USER_SCHEMA if step_id == "user" else API_KEY_SCHEMA,
            errors=errors,
            description_placeholders={"console_url": CONSOLE_URL},
        )


class BladeOptionsFlow(OptionsFlow):
    """Switch each Blade action on or off and pick its model."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the action toggles."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        entry: BladeConfigEntry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        models = entry.runtime_data.models
        schema = probatio.Schema(
            {
                probatio.Required(CONF_OCR_ENABLED): BooleanSelector(),
                probatio.Required(CONF_OCR_MODEL): model_selector(
                    models, is_vision_chat, RECOMMENDED_MODELS["ocr"]
                ),
                probatio.Required(CONF_TRANSLATE_ENABLED): BooleanSelector(),
                probatio.Required(CONF_TRANSLATE_MODEL): model_selector(
                    models, is_chat, RECOMMENDED_MODELS["translate"]
                ),
                probatio.Required(CONF_FORECAST_ENABLED): BooleanSelector(),
                probatio.Required(CONF_FORECAST_MODEL): model_selector(
                    models, is_kind(KIND_FORECAST), RECOMMENDED_MODELS["forecast"]
                ),
                probatio.Required(CONF_MONTHLY_BUDGET): probatio.All(
                    NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            step=0.5,
                            unit_of_measurement="€",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    probatio.Coerce(float),
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, {**DEFAULT_ENTRY_OPTIONS, **entry.options}
            ),
        )


class BladeSubentryFlow(ConfigSubentryFlow):
    """Common add/reconfigure logic for every feature subentry."""

    defaults: Mapping[str, Any] = {}
    options: dict[str, Any]

    @property
    def _is_new(self) -> bool:
        return self.source == SOURCE_USER

    @property
    def _models(self) -> dict[str, BladeModel]:
        entry: BladeConfigEntry = self._get_entry()
        return entry.runtime_data.models

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a subentry with recommended settings."""
        self.options = dict(self.defaults)
        if CONF_PROMPT in self.options:
            self.options[CONF_PROMPT] = default_prompt(self.hass.config.language)
        return await self.async_step_init()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit an existing subentry."""
        self.options = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_init()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show the feature form."""
        if self._get_entry().state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            self.options.update(user_input)
            errors, placeholders = self._validate()
            if not errors:
                if (next_step := self._next_step()) is not None:
                    return await next_step()
                return self._save()
        schema: dict[Any, Any] = {}
        if self._is_new:
            schema[probatio.Required(CONF_NAME, default=self._default_name)] = str
        schema.update(self._schema())
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema(schema), self.options
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    def _validate(self) -> tuple[dict[str, str], dict[str, str]]:
        """Return form errors and their placeholders; none by default."""
        return {}, {}

    @property
    def _default_name(self) -> str:
        return default_name(self._subentry_type)

    def _schema(self) -> dict[Any, Any]:
        raise NotImplementedError

    def _next_step(self) -> Callable[[], Awaitable[SubentryFlowResult]] | None:
        return None

    def _save(self) -> SubentryFlowResult:
        if self._is_new:
            title = self.options.pop(CONF_NAME)
            return self.async_create_entry(title=title, data=self.options)
        return self.async_update_and_abort(
            self._get_entry(), self._get_reconfigure_subentry(), data=self.options
        )


class ConversationFlowHandler(BladeSubentryFlow):
    """Configure a conversation agent."""

    defaults = RECOMMENDED_CONVERSATION_OPTIONS

    @override
    def _schema(self) -> dict[Any, Any]:
        hass_apis = [
            SelectOptionDict(label=api.name, value=api.id)
            for api in llm.async_get_apis(self.hass)
        ]
        if suggested := self.options.get(CONF_LLM_HASS_API):
            valid = {api["value"] for api in hass_apis}
            self.options[CONF_LLM_HASS_API] = [a for a in suggested if a in valid]
        return {
            probatio.Required(CONF_MODEL): model_selector(
                self._models, is_tool_chat, RECOMMENDED_MODELS["conversation"]
            ),
            probatio.Optional(CONF_PROMPT): TemplateSelector(),
            probatio.Optional(CONF_LLM_HASS_API): SelectSelector(
                SelectSelectorConfig(
                    options=hass_apis, multiple=True, mode=SelectSelectorMode.LIST
                )
            ),
            probatio.Optional(CONF_FUNCTIONS): TextSelector(
                TextSelectorConfig(multiline=True)
            ),
            probatio.Required(CONF_WEB_SEARCH): BooleanSelector(),
            probatio.Required(CONF_WEB_FETCH): BooleanSelector(),
            probatio.Required(CONF_CODE_INTERPRETER): BooleanSelector(),
            probatio.Required(CONF_RECOMMENDED): BooleanSelector(),
        }

    @override
    def _validate(self) -> tuple[dict[str, str], dict[str, str]]:
        try:
            parse_functions(self.options.get(CONF_FUNCTIONS))
        except FunctionsError as err:
            return {CONF_FUNCTIONS: "invalid_functions"}, {"error": str(err)}
        return {}, {}

    @override
    def _next_step(self) -> Callable[[], Awaitable[SubentryFlowResult]] | None:
        if not self.options.get(CONF_FUNCTIONS):
            self.options.pop(CONF_FUNCTIONS, None)
        if not self.options.get(CONF_LLM_HASS_API):
            self.options.pop(CONF_LLM_HASS_API, None)
        if self.options.get(CONF_RECOMMENDED):
            for key in (CONF_TEMPERATURE, CONF_MAX_TOKENS, CONF_REASONING_EFFORT):
                self.options.pop(key, None)
            return None
        return self.async_step_advanced

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Tune sampling and reasoning."""
        if user_input is not None:
            self.options.update(user_input)
            return self._save()
        schema = probatio.Schema(
            {
                probatio.Required(
                    CONF_TEMPERATURE, default=RECOMMENDED_TEMPERATURE
                ): NumberSelector(NumberSelectorConfig(min=0, max=2, step=0.05)),
                probatio.Required(
                    CONF_MAX_TOKENS, default=RECOMMENDED_MAX_TOKENS
                ): probatio.All(
                    NumberSelector(NumberSelectorConfig(min=64, max=65536, step=1)),
                    # The selector returns a float; the API wants an integer.
                    probatio.Coerce(int),
                ),
                probatio.Required(
                    CONF_REASONING_EFFORT, default=RECOMMENDED_REASONING_EFFORT
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=REASONING_EFFORTS,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_REASONING_EFFORT,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="advanced",
            data_schema=self.add_suggested_values_to_schema(schema, self.options),
        )


class AITaskFlowHandler(BladeSubentryFlow):
    """Configure an AI task entity."""

    defaults = RECOMMENDED_AI_TASK_OPTIONS

    @override
    def _schema(self) -> dict[Any, Any]:
        return {
            probatio.Required(CONF_MODEL): model_selector(
                self._models, is_chat, RECOMMENDED_MODELS["ai_task"]
            ),
            probatio.Required(CONF_IMAGES_ENABLED): BooleanSelector(),
            probatio.Required(CONF_IMAGE_MODEL): model_selector(
                self._models, is_kind(KIND_IMAGE), RECOMMENDED_MODELS["image"]
            ),
            probatio.Required(CONF_IMAGE_SIZE): SelectSelector(
                SelectSelectorConfig(
                    options=IMAGE_SIZES,
                    mode=SelectSelectorMode.DROPDOWN,
                    custom_value=True,
                )
            ),
        }


class EvaluateFlowHandler(BladeSubentryFlow):
    """Configure a calibrated evaluation entity."""

    defaults = RECOMMENDED_EVALUATE_OPTIONS

    @override
    def _schema(self) -> dict[Any, Any]:
        return {
            probatio.Required(CONF_MODEL): model_selector(
                self._models, is_kind(KIND_DECISION), RECOMMENDED_MODELS["evaluate"]
            )
        }


class STTFlowHandler(BladeSubentryFlow):
    """Configure a speech-to-text entity."""

    defaults = RECOMMENDED_STT_OPTIONS

    @override
    def _schema(self) -> dict[Any, Any]:
        return {
            probatio.Required(CONF_MODEL): model_selector(
                self._models, is_stt, RECOMMENDED_MODELS["stt"]
            ),
            probatio.Optional(
                CONF_PROMPT, description={"suggested_value": DEFAULT_STT_PROMPT}
            ): TextSelector(TextSelectorConfig(multiline=True)),
        }


class TTSFlowHandler(BladeSubentryFlow):
    """Configure a text-to-speech entity."""

    defaults = RECOMMENDED_TTS_OPTIONS

    @override
    def _schema(self) -> dict[Any, Any]:
        voices = [
            SelectOptionDict(value=voice_id, label=f"{label} · {model}")
            for model, model_voices in SPEECH_VOICES.items()
            for voice_id, label in model_voices.items()
        ]
        return {
            probatio.Required(CONF_MODEL): model_selector(
                self._models, is_kind(KIND_SPEECH), RECOMMENDED_MODELS["tts"]
            ),
            probatio.Required(CONF_VOICE): SelectSelector(
                SelectSelectorConfig(
                    options=voices, mode=SelectSelectorMode.DROPDOWN, custom_value=True
                )
            ),
            probatio.Required(CONF_STREAMING): BooleanSelector(),
        }

    @override
    def _next_step(self) -> Callable[[], Awaitable[SubentryFlowResult]] | None:
        model = self.options[CONF_MODEL]
        known = SPEECH_VOICES.get(model, {})
        voice = self.options.get(CONF_VOICE)
        other_models = {
            other_voice
            for other, voices in SPEECH_VOICES.items()
            if other != model
            for other_voice in voices
        }
        if known and (not voice or (voice in other_models and voice not in known)):
            # A documented voice of another model would be rejected by this one;
            # a voice typed in by hand is kept as is.
            self.options[CONF_VOICE] = next(iter(known))
        if model not in STREAMING_SPEECH_MODELS:
            self.options[CONF_STREAMING] = False
        return None
