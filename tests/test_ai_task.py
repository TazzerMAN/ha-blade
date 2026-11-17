"""Tests for the Blade AI task and evaluation entities."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import probatio
import pytest
from homeassistant.components import ai_task, media_source
from homeassistant.const import CONF_MODEL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant.common import MockConfigEntry

from custom_components.blade.ai_task import (
    BladeEvaluationEntity,
    _sniff_image_mime,
    _strip_fences,
)
from custom_components.blade.api import BladeApiError, StreamDelta
from custom_components.blade.const import (
    CONF_IMAGE_MODEL,
    CONF_IMAGE_SIZE,
    CONF_IMAGES_ENABLED,
)

from .conftest import (
    TEST_API,
    EchoTool,
    script_chat,
    subentry_id,
    text_round,
    tool_call,
)

TASK = "ai_task.blade_ai_task"
EVAL = "ai_task.blade_evaluation"
STRUCTURE = probatio.Schema({probatio.Required("temperature"): int})
QUESTIONS: dict[str, Any] = {
    "delivered": {"type": "noul", "instructions": "Has it arrived?"},
    "where": {
        "type": "choice",
        "instructions": "Where?",
        "criteria": {"door": "At the door", "other": "Elsewhere"},
    },
    "urgency": {
        "type": "score",
        "instructions": "How urgent?",
        "criteria": ["none", "soon", "now"],
    },
}
ANSWERS: dict[str, Any] = {
    "delivered": {"type": "noul", "noul": 0.97},
    "where": {
        "type": "choice",
        "choice": "door",
        "probabilities": {"door": 0.9, "other": 0.1},
    },
    "urgency": {
        "type": "score",
        "score": 0.15,
        "probabilities": {"2": 0.05, "0": 0.9, "1": 0.05},
    },
}


def features(hass: HomeAssistant, entity_id: str) -> ai_task.AITaskEntityFeature:
    """Return the advertised features of an entity."""
    return ai_task.AITaskEntityFeature(
        hass.states.get(entity_id).attributes["supported_features"]
    )


async def reload_with(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    subentry_type: str,
    data: dict[str, Any],
) -> None:
    """Replace the data of a subentry and set the entry up."""
    hass.config_entries.async_update_subentry(
        entry, entry.subentries[subentry_id(entry, subentry_type)], data=data
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# ------------------------------------------------------------------ Features


async def test_features_default(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A vision model with images enabled can do everything but evaluate."""
    assert features(hass, TASK) == (
        ai_task.AITaskEntityFeature.GENERATE_DATA
        | ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
        | ai_task.AITaskEntityFeature.GENERATE_IMAGE
    )
    assert features(hass, EVAL) == ai_task.AITaskEntityFeature.EVALUATE


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            {CONF_MODEL: "text-only", CONF_IMAGES_ENABLED: False},
            ai_task.AITaskEntityFeature.GENERATE_DATA,
        ),
        (
            {CONF_MODEL: "text-only", CONF_IMAGES_ENABLED: True},
            ai_task.AITaskEntityFeature.GENERATE_DATA
            | ai_task.AITaskEntityFeature.GENERATE_IMAGE,
        ),
        # A model absent from the catalog is given the benefit of the doubt.
        (
            {CONF_MODEL: "unlisted"},
            ai_task.AITaskEntityFeature.GENERATE_DATA
            | ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS,
        ),
    ],
)
async def test_features_follow_model(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    data: dict[str, Any],
    expected: ai_task.AITaskEntityFeature,
) -> None:
    """Attachments need vision; images need the toggle."""
    await reload_with(hass, mock_config_entry, "ai_task_data", data)
    assert features(hass, TASK) == expected


# ------------------------------------------------------------- Generate data


async def generate(
    hass: HomeAssistant, structure: probatio.Schema | None = None, **kwargs: Any
) -> ai_task.GenDataTaskResult:
    """Run a data task on the Blade entity."""
    return await ai_task.async_generate_data(
        hass,
        task_name="weather",
        entity_id=TASK,
        instructions="Give the weather",
        structure=structure,
        **kwargs,
    )


async def test_generate_text(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Without a structure the raw text is the data."""
    bodies = script_chat(mock_client, text_round("Sunny ", "day"))
    result = await generate(hass)
    assert result.data == "Sunny day"
    assert "response_format" not in bodies[0]
    assert bodies[0]["messages"][-1]["content"] == "Give the weather"


@pytest.mark.parametrize(
    "reply",
    [
        '{"temperature": 21}',
        '```json\n{"temperature": 21}\n```',
        '```\n{"temperature": 21}\n```  ',
        '  {"temperature": 21}  ',
    ],
)
async def test_generate_structured(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    reply: str,
) -> None:
    """The JSON answer is parsed, with or without a Markdown fence."""
    bodies = script_chat(mock_client, text_round(reply))
    result = await generate(hass, STRUCTURE)
    assert result.data == {"temperature": 21}
    response_format = bodies[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "weather"
    assert response_format["json_schema"]["schema"]["required"] == ["temperature"]


@pytest.mark.parametrize(
    "reply", ["not json", '{"temperature": ', "```json\noops\n```"]
)
async def test_generate_invalid_json(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    reply: str,
) -> None:
    """A reply that stays non-JSON after one correction raises a readable error."""
    bodies = script_chat(mock_client, text_round(reply), text_round(reply))
    with pytest.raises(HomeAssistantError) as err:
        await generate(hass, STRUCTURE)
    assert err.value.translation_key == "json_parse_error"
    assert len(bodies) == 2
    assert "not valid JSON" in bodies[1]["messages"][-1]["content"]


async def test_generate_structure_mismatch(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """JSON that keeps missing the structure names the problem."""
    reply = '{"temp": 21}'
    bodies = script_chat(mock_client, text_round(reply), text_round(reply))
    with pytest.raises(HomeAssistantError) as err:
        await generate(hass, STRUCTURE)
    assert err.value.translation_key == "structure_mismatch"
    assert "temperature" in err.value.translation_placeholders["error"]
    correction = bodies[1]["messages"][-1]
    assert correction["role"] == "user"
    assert "temperature" in correction["content"]


async def test_generate_structure_drops_extra_fields(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Fields outside the structure are dropped, without a correction."""
    bodies = script_chat(mock_client, text_round('{"temperature": 21, "unit": "C"}'))
    result = await generate(hass, STRUCTURE)
    assert result.data == {"temperature": 21}
    assert len(bodies) == 1


async def test_generate_structure_corrected(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """One correction is enough when the model fixes its answer."""
    bodies = script_chat(
        mock_client,
        text_round('{"temperature": "warm"}'),
        text_round('{"temperature": 21}'),
    )
    result = await generate(hass, STRUCTURE)
    assert result.data == {"temperature": 21}
    assert len(bodies) == 2
    assert bodies[1]["messages"][-2] == {
        "role": "assistant",
        "content": '{"temperature": "warm"}',
    }


async def test_generate_without_text(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A reply with only reasoning yields an empty string."""
    script_chat(mock_client, [StreamDelta(role="assistant", reasoning="hm")])
    assert (await generate(hass)).data == ""


async def test_generate_api_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """An API error is raised as a Home Assistant error."""
    script_chat(mock_client, BladeApiError(500, "boom", "kaput"))
    with pytest.raises(HomeAssistantError, match="kaput"):
        await generate(hass)


async def test_generate_with_llm_api_and_structure(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    echo_tool: EchoTool,
) -> None:
    """A task LLM API contributes its tools and its serializer."""
    from homeassistant.helpers import llm  # noqa: PLC0415

    api = next(a for a in llm.async_get_apis(hass) if a.id == TEST_API)
    bodies = script_chat(
        mock_client,
        [StreamDelta(tool_calls=[tool_call(0, "c1", "echo", '{"text": "x"}')])],
        text_round('{"temperature": 3}'),
    )
    result = await generate(hass, STRUCTURE, llm_api=api)
    assert result.data == {"temperature": 3}
    assert echo_tool.calls == [{"text": "x"}]
    assert bodies[0]["tools"][0]["function"]["name"] == "echo"


async def test_generate_tool_loop_without_answer(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    echo_tool: EchoTool,
) -> None:
    """A model that only ever calls tools ends on a tool result: an error."""
    from homeassistant.helpers import llm  # noqa: PLC0415

    api = next(a for a in llm.async_get_apis(hass) if a.id == TEST_API)
    rounds = [
        [StreamDelta(tool_calls=[tool_call(0, f"c{i}", "echo", '{"text": "x"}')])]
        for i in range(10)
    ]
    bodies = script_chat(mock_client, *rounds)
    with pytest.raises(HomeAssistantError) as err:
        await generate(hass, llm_api=api)
    assert err.value.translation_key == "response_not_found"
    assert len(bodies) == 10


async def test_generate_with_image_attachment(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    tmp_path: Path,
) -> None:
    """An attached image goes out as an image_url part with its MIME type."""
    image = tmp_path / "front.jpg"
    image.write_bytes(b"jpeg-bytes")
    bodies = script_chat(mock_client, text_round("A door."))
    with patch(
        "homeassistant.components.media_source.async_resolve_media",
        return_value=media_source.PlayMedia(
            url="/media/front.jpg", mime_type="image/jpeg", path=image
        ),
    ):
        result = await generate(
            hass,
            attachments=[
                {
                    "media_content_id": "media-source://media_source/local/front.jpg",
                    "media_content_type": "image/jpeg",
                }
            ],
        )
    assert result.data == "A door."
    content = bodies[0]["messages"][-1]["content"]
    assert content[0]["text"] == "Give the weather"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_attachments_refused_without_vision(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A text-only model does not advertise attachment support."""
    await reload_with(
        hass, mock_config_entry, "ai_task_data", {CONF_MODEL: "text-only"}
    )
    with pytest.raises(HomeAssistantError, match="attachments"):
        await generate(
            hass,
            attachments=[
                {"media_content_id": "media-source://media_source/local/a.png"}
            ],
        )


# ------------------------------------------------------------ Generate image


@pytest.mark.parametrize(
    ("image", "mime"),
    [
        (b"\x89PNG\r\n", "image/png"),
        (b"\xff\xd8\xff\xe0", "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", "image/png"),
        (b"???", "image/png"),
    ],
)
async def test_generate_image(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    tmp_path: Path,
    image: bytes,
    mime: str,
) -> None:
    """The image is requested with the configured model and size, then stored."""
    mock_client.generate_image.return_value = image
    result = await ai_task.async_generate_image(
        hass, task_name="cat", entity_id=TASK, instructions="A cat"
    )
    mock_client.generate_image.assert_awaited_once_with(
        "A cat", "flux-2-klein", "1024x1024"
    )
    assert result["mime_type"] == mime
    assert (result["width"], result["height"]) == (1024, 1024)
    assert result["model"] == "flux-2-klein"
    assert result["url"]


async def test_generate_image_options_and_defaults(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    tmp_path: Path,
) -> None:
    """Configured model and size win; missing ones fall back to the recommended."""
    await reload_with(
        hass,
        mock_config_entry,
        "ai_task_data",
        {
            CONF_MODEL: "gemma-4-31b",
            CONF_IMAGES_ENABLED: True,
            CONF_IMAGE_MODEL: "other-image",
            CONF_IMAGE_SIZE: "1024x768",
        },
    )
    result = await ai_task.async_generate_image(
        hass, task_name="cat", entity_id=TASK, instructions="A cat"
    )
    mock_client.generate_image.assert_awaited_once_with(
        "A cat", "other-image", "1024x768"
    )
    assert (result["width"], result["height"]) == (1024, 768)

    mock_client.generate_image.reset_mock()
    hass.config_entries.async_update_subentry(
        mock_config_entry,
        mock_config_entry.subentries[subentry_id(mock_config_entry, "ai_task_data")],
        data={CONF_MODEL: "gemma-4-31b", CONF_IMAGES_ENABLED: True},
    )
    await hass.async_block_till_done()
    await ai_task.async_generate_image(
        hass, task_name="cat", entity_id=TASK, instructions="A cat"
    )
    mock_client.generate_image.assert_awaited_once_with(
        "A cat", "flux-2-klein", "1024x1024"
    )


async def test_generate_image_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A failed generation is raised as a Home Assistant error."""
    mock_client.generate_image.side_effect = BladeApiError(500, "boom", "no gpu")
    with pytest.raises(HomeAssistantError, match="no gpu"):
        await ai_task.async_generate_image(
            hass, task_name="cat", entity_id=TASK, instructions="A cat"
        )


async def test_image_not_enabled(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Image generation is refused when the toggle is off."""
    await reload_with(
        hass,
        mock_config_entry,
        "ai_task_data",
        {CONF_MODEL: "gemma-4-31b", CONF_IMAGES_ENABLED: False},
    )
    with pytest.raises(HomeAssistantError):
        await ai_task.async_generate_image(
            hass, task_name="cat", entity_id=TASK, instructions="A cat"
        )
    mock_client.generate_image.assert_not_called()


# ---------------------------------------------------------------- Evaluation


async def test_evaluate(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Every answer type is converted; score levels are ordered by index."""
    mock_client.decide.return_value = ANSWERS
    result = await ai_task.async_evaluate(
        hass,
        task_name="delivery",
        entity_id=EVAL,
        state={"event": "delivered"},
        questions=QUESTIONS,
    )
    assert result.answers == {
        "delivered": ai_task.NoulAnswer(noul=0.97),
        "where": ai_task.ChoiceAnswer(
            choice="door", probabilities={"door": 0.9, "other": 0.1}
        ),
        "urgency": ai_task.ScoreAnswer(score=0.15, probabilities=[0.9, 0.05, 0.05]),
    }
    model, state, questions = mock_client.decide.await_args.args
    assert model == "clef-flash"
    assert state == {"event": "delivered"}
    assert questions["where"]["criteria"] == {
        "door": "At the door",
        "other": "Elsewhere",
    }


async def test_evaluate_text_state_only_noul(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A plain text state is passed as is."""
    mock_client.decide.return_value = {"delivered": ANSWERS["delivered"]}
    await ai_task.async_evaluate(
        hass,
        task_name="d",
        entity_id=EVAL,
        state="Delivered",
        questions={"delivered": QUESTIONS["delivered"]},
    )
    assert mock_client.decide.await_args.args[1] == "Delivered"


async def test_evaluate_empty_state_when_none(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """An entity called with no state sends an empty string."""
    entity = hass.data["entity_components"]["ai_task"].get_entity(EVAL)
    mock_client.decide.return_value = {"delivered": ANSWERS["delivered"]}
    task = ai_task.EvaluationTask(
        name="d", questions={"delivered": QUESTIONS["delivered"]}
    )
    await entity._async_evaluate(task)
    assert mock_client.decide.await_args.args[1] == ""


async def test_evaluate_attachments_refused(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The evaluation entity is text only, even if it is called with files."""
    entity = hass.data["entity_components"]["ai_task"].get_entity(EVAL)
    assert isinstance(entity, BladeEvaluationEntity)
    task = ai_task.EvaluationTask(
        name="d",
        questions={"delivered": QUESTIONS["delivered"]},
        state="x",
        attachments=[MagicMock()],
    )
    with pytest.raises(HomeAssistantError) as err:
        await entity._async_evaluate(task)
    assert err.value.translation_key == "evaluate_text_only"
    mock_client.decide.assert_not_called()


async def test_evaluate_attachments_not_advertised(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Through the public API the attachment is stopped even earlier."""
    with pytest.raises(HomeAssistantError, match="attachments"):
        await ai_task.async_evaluate(
            hass,
            task_name="d",
            entity_id=EVAL,
            questions={"delivered": QUESTIONS["delivered"]},
            attachments=[
                {"media_content_id": "media-source://media_source/local/a.png"}
            ],
        )


async def test_evaluate_api_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """An API failure is raised as a Home Assistant error."""
    mock_client.decide.side_effect = BladeApiError(500, "boom", "offline")
    with pytest.raises(HomeAssistantError, match="offline"):
        await ai_task.async_evaluate(
            hass,
            task_name="d",
            entity_id=EVAL,
            state="x",
            questions={"delivered": QUESTIONS["delivered"]},
        )


@pytest.mark.parametrize("status", [503, 504])
async def test_evaluate_model_starting(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    status: int,
) -> None:
    """A model still booting on Blade's side says so, with its name."""
    mock_client.decide.side_effect = BladeApiError(status, None, "cold start")
    with pytest.raises(HomeAssistantError) as err:
        await ai_task.async_evaluate(
            hass,
            task_name="d",
            entity_id=EVAL,
            state="x",
            questions={"delivered": QUESTIONS["delivered"]},
        )
    assert err.value.translation_key == "model_starting"
    assert err.value.translation_placeholders == {"model": "clef-flash"}


@pytest.mark.parametrize(
    "answers",
    [
        {},  # no answer at all
        {"delivered": "yes"},  # not a mapping
        {"delivered": {"type": "mystery"}},  # unknown type
        {"delivered": {"type": "noul"}},  # missing value
        {"delivered": {"type": "noul", "noul": "high"}},  # not a number
        {"delivered": {"type": "choice", "choice": "a"}},  # no distribution
        {"delivered": {"type": "score", "score": 1, "probabilities": {"1": 0.5}}},
    ],
)
async def test_evaluate_invalid_answers(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    answers: dict[str, Any],
) -> None:
    """A malformed answer names the question it belongs to."""
    mock_client.decide.return_value = answers
    with pytest.raises(HomeAssistantError) as err:
        await ai_task.async_evaluate(
            hass,
            task_name="d",
            entity_id=EVAL,
            state="x",
            questions={"delivered": QUESTIONS["delivered"]},
        )
    assert err.value.translation_key == "invalid_answer"
    assert err.value.translation_placeholders == {"question": "delivered"}


# ------------------------------------------------------------------- Helpers


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain", "plain"),
        ("  padded \n", "padded"),
        ("```json\n{}\n```", "{}"),
        ("```\n[1]\n```", "[1]"),
        ('```{"a": 1}```', '{"a": 1}'),
        ('```json {"a": 1} ```', '{"a": 1}'),
    ],
)
def test_strip_fences(text: str, expected: str) -> None:
    """Fences are removed only when present."""
    assert _strip_fences(text) == expected


def test_sniff_image_mime() -> None:
    """Unknown data defaults to PNG."""
    assert _sniff_image_mime(b"\xff\xd8x") == "image/jpeg"
    assert _sniff_image_mime(b"") == "image/png"
