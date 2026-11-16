"""Conversation agent backed by a Blade chat model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, override

from homeassistant.components import conversation
from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT, MATCH_ALL
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import llm
from homeassistant.helpers.template import Template

from .const import CONF_FUNCTIONS, DOMAIN, SUBENTRY_CONVERSATION
from .entity import BladeEntity
from .functions import FunctionsAPI, exposed_entities, parse_functions

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import BladeConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: BladeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one agent per conversation subentry."""
    for subentry_id, subentry in config_entry.subentries.items():
        if subentry.subentry_type == SUBENTRY_CONVERSATION:
            async_add_entities(
                [BladeConversationEntity(config_entry, subentry)],
                config_subentry_id=subentry_id,
            )


class BladeConversationEntity(BladeEntity, conversation.ConversationEntity):
    """Blade conversation agent."""

    _attr_supports_streaming = True

    def __init__(self, entry: BladeConfigEntry, subentry: ConfigSubentry) -> None:
        """Enable device control when tools are available."""
        super().__init__(entry, subentry)
        self._functions = parse_functions(subentry.data.get(CONF_FUNCTIONS))
        if subentry.data.get(CONF_LLM_HASS_API) or self._functions:
            self._attr_supported_features = (
                conversation.ConversationEntityFeature.CONTROL
            )

    @property
    @override
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Blade chat models are multilingual."""
        return MATCH_ALL

    def _llm_api(self) -> str | list[str] | llm.API | None:
        """Return the selected Home Assistant APIs, plus the user functions."""
        selected: list[str] = self.subentry.data.get(CONF_LLM_HASS_API) or []
        if not self._functions:
            return selected or None
        functions = FunctionsAPI(
            self.hass, f"{DOMAIN}_{self.subentry.subentry_id}", self._functions
        )
        apis = [api for api in llm.async_get_apis(self.hass) if api.id in selected]
        return llm.MergedAPI([*apis, functions]) if apis else functions

    def _prompt(self, user_input: conversation.ConversationInput) -> str | None:
        """Render Extended OpenAI prompt variables Home Assistant does not know."""
        prompt: str | None = self.subentry.data.get(CONF_PROMPT)
        if not prompt or "exposed_entities" not in prompt:
            return prompt
        try:
            rendered = Template(prompt, self.hass).async_render(
                {
                    "exposed_entities": exposed_entities(self.hass),
                    "current_device_id": user_input.device_id,
                    "ha_name": self.hass.config.location_name,
                },
                parse_result=False,
            )
        except TemplateError:
            # Home Assistant renders it again and answers with its usual error.
            return prompt
        # Already rendered: Home Assistant's own pass must leave it as is.
        return "{% raw %}" + str(rendered) + "{% endraw %}"

    @override
    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Answer one user turn."""
        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                self._llm_api(),
                self._prompt(user_input),
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        await self._async_handle_chat_log(chat_log)
        return conversation.async_get_result_from_chat_log(user_input, chat_log)
