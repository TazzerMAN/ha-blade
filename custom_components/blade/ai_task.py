"""AI tasks: data, camera images, image generation and calibrated evaluation."""

from __future__ import annotations

import re
from json import JSONDecodeError
from typing import TYPE_CHECKING, Any, override

from homeassistant.components import ai_task, conversation
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.json import json_loads

from .api import BladeError, current_service
from .const import (
    CONF_IMAGE_MODEL,
    CONF_IMAGE_SIZE,
    CONF_IMAGES_ENABLED,
    DOMAIN,
    RECOMMENDED_IMAGE_MODEL,
    RECOMMENDED_IMAGE_SIZE,
    SUBENTRY_AI_TASK,
    SUBENTRY_EVALUATE,
)
from .entity import BladeEntity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import BladeConfigEntry

PARALLEL_UPDATES = 0
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: BladeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one entity per AI task or evaluation subentry."""
    for subentry_id, subentry in config_entry.subentries.items():
        entity: ai_task.AITaskEntity
        if subentry.subentry_type == SUBENTRY_AI_TASK:
            entity = BladeAITaskEntity(config_entry, subentry)
        elif subentry.subentry_type == SUBENTRY_EVALUATE:
            entity = BladeEvaluationEntity(config_entry, subentry)
        else:
            continue
        async_add_entities([entity], config_subentry_id=subentry_id)


class BladeAITaskEntity(ai_task.AITaskEntity, BladeEntity):
    """Generate data and images with Blade models."""

    def __init__(self, entry: BladeConfigEntry, subentry: ConfigSubentry) -> None:
        """Advertise only what the configured models can do."""
        super().__init__(entry, subentry)
        features = ai_task.AITaskEntityFeature.GENERATE_DATA
        if (model := self.model) is None or model.supports_vision:
            features |= ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
        if subentry.data.get(CONF_IMAGES_ENABLED):
            features |= ai_task.AITaskEntityFeature.GENERATE_IMAGE
        self._attr_supported_features = features

    @override
    async def _async_generate_data(
        self, task: ai_task.GenDataTask, chat_log: conversation.ChatLog
    ) -> ai_task.GenDataTaskResult:
        """Answer the task, as text or as JSON matching its structure."""
        await self._async_handle_chat_log(chat_log, task.name, task.structure)
        last = chat_log.content[-1]
        if not isinstance(last, conversation.AssistantContent):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="response_not_found"
            )
        text = last.content or ""
        if not task.structure:
            return ai_task.GenDataTaskResult(
                conversation_id=chat_log.conversation_id, data=text
            )
        try:
            data = json_loads(_strip_fences(text))
        except JSONDecodeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="json_parse_error"
            ) from err
        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id, data=data
        )

    @override
    async def _async_generate_image(
        self, task: ai_task.GenImageTask, chat_log: conversation.ChatLog
    ) -> ai_task.GenImageTaskResult:
        """Draw the image described by the task instructions."""
        options = self.subentry.data
        model = options.get(CONF_IMAGE_MODEL, RECOMMENDED_IMAGE_MODEL)
        size = options.get(CONF_IMAGE_SIZE, RECOMMENDED_IMAGE_SIZE)
        current_service.set(self.subentry.title)
        try:
            image = await self.entry.runtime_data.client.generate_image(
                task.instructions, model, size
            )
        except BladeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="api_error",
                translation_placeholders={"error": str(err)},
            ) from err
        width, _, height = size.partition("x")
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=self.entity_id, content="")
        )
        return ai_task.GenImageTaskResult(
            image_data=image,
            conversation_id=chat_log.conversation_id,
            mime_type=_sniff_image_mime(image),
            width=int(width),
            height=int(height),
            model=model,
        )


class BladeEvaluationEntity(ai_task.AITaskEntity, BladeEntity):
    """Calibrated yes/no, choice and score answers from a decision model."""

    _attr_supported_features = ai_task.AITaskEntityFeature.EVALUATE

    @override
    async def _async_evaluate(
        self, task: ai_task.EvaluationTask
    ) -> ai_task.EvaluationTaskResult:
        """Ask every question in one decision call."""
        if task.attachments:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="evaluate_text_only"
            )
        questions: dict[str, dict[str, Any]] = {
            question_id: dict(question)
            for question_id, question in task.questions.items()
        }
        current_service.set(self.subentry.title)
        try:
            answers = await self.entry.runtime_data.client.decide(
                self.model_id, task.state if task.state is not None else "", questions
            )
        except BladeError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="api_error",
                translation_placeholders={"error": str(err)},
            ) from err
        return ai_task.EvaluationTaskResult(
            answers={
                question_id: _convert_answer(question_id, answers.get(question_id))
                for question_id in task.questions
            }
        )


def _convert_answer(question_id: str, answer: Any) -> ai_task.EvaluationAnswer:
    """Map a Blade decision answer to its Home Assistant type."""
    try:
        match answer["type"]:
            case "noul":
                return ai_task.NoulAnswer(noul=float(answer["noul"]))
            case "choice":
                return ai_task.ChoiceAnswer(
                    choice=str(answer["choice"]),
                    probabilities={
                        str(key): float(value)
                        for key, value in answer["probabilities"].items()
                    },
                )
            case "score":
                levels = answer["probabilities"]
                return ai_task.ScoreAnswer(
                    score=float(answer["score"]),
                    probabilities=[float(levels[str(i)]) for i in range(len(levels))],
                )
    except (KeyError, TypeError, ValueError) as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="invalid_answer",
            translation_placeholders={"question": question_id},
        ) from err
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="invalid_answer",
        translation_placeholders={"question": question_id},
    )


def _strip_fences(text: str) -> str:
    """Remove a Markdown code fence some models wrap JSON in."""
    if match := _FENCE.fullmatch(text.strip()):
        return match.group(1).strip()
    return text.strip()


def _sniff_image_mime(data: bytes) -> str:
    """Guess the image type from its magic bytes."""
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"
