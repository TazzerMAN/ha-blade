"""Tests for the blade.ocr, blade.translate and blade.forecast actions."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import probatio
import pytest
from homeassistant.components.camera import Image
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.blade.api import BladeApiError, BladeConnectionError
from custom_components.blade.const import (
    CONF_FORECAST_ENABLED,
    CONF_OCR_ENABLED,
    CONF_TRANSLATE_ENABLED,
    DEFAULT_ENTRY_OPTIONS,
    DOMAIN,
)

from .conftest import call


def completion(text: str | None) -> dict[str, Any]:
    """Return a chat completion answering with a text."""
    return {"choices": [{"message": {"content": text}}]}


@pytest.fixture
def media_dir(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Allow reading files from a temporary directory."""
    directory = tmp_path / "media"
    directory.mkdir()
    hass.config.allowlist_external_dirs = {str(directory)}
    return directory


# ----------------------------------------------------------------------- OCR


async def test_ocr_file(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    media_dir: Path,
) -> None:
    """A file under an allowed directory is read with the OCR model."""
    scan = media_dir / "letter.jpg"
    scan.write_bytes(b"jpeg-bytes")
    mock_client.chat.return_value = completion("  # Dear Sir\n ")
    result = await call(hass, "ocr", {"file_path": str(scan)})
    assert result == {"text": "# Dear Sir"}

    (body,) = mock_client.chat.await_args.args
    assert body["model"] == "deepseek-ocr-2"
    text_part, image_part = body["messages"][0]["content"]
    assert text_part["text"].startswith("Convert this document to Markdown")
    encoded = base64.b64encode(b"jpeg-bytes").decode()
    assert image_part["image_url"]["url"] == f"data:image/jpeg;base64,{encoded}"


async def test_ocr_file_unknown_extension_and_instructions(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    media_dir: Path,
) -> None:
    """An unknown extension is sent as PNG; custom instructions replace the prompt."""
    scan = media_dir / "scan.unknownext"
    scan.write_bytes(b"x")
    mock_client.chat.return_value = completion(None)
    result = await call(
        hass, "ocr", {"file_path": str(scan), "instructions": "Only the totals"}
    )
    assert result == {"text": ""}
    content = mock_client.chat.await_args.args[0]["messages"][0]["content"]
    assert content[0]["text"] == "Only the totals"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_ocr_camera(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A camera snapshot is read with its own content type."""
    mock_client.chat.return_value = completion("PLATE 123")
    with patch(
        "homeassistant.components.camera.async_get_image",
        return_value=Image("image/png", b"snap"),
    ) as get_image:
        result = await call(hass, "ocr", {"camera_entity_id": "camera.front"})
    get_image.assert_awaited_once_with(hass, "camera.front")
    assert result == {"text": "PLATE 123"}
    url = mock_client.chat.await_args.args[0]["messages"][0]["content"][1]["image_url"][
        "url"
    ]
    assert url == f"data:image/png;base64,{base64.b64encode(b'snap').decode()}"


async def test_ocr_forbidden_path(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    media_dir: Path,
    tmp_path: Path,
) -> None:
    """A path outside the allowlist is never read."""
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"x")
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "ocr", {"file_path": str(secret)})
    assert err.value.translation_key == "path_not_allowed"
    mock_client.chat.assert_not_called()


async def test_ocr_missing_file(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    media_dir: Path,
) -> None:
    """A file that does not exist is reported."""
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "ocr", {"file_path": str(media_dir / "nope.png")})
    assert err.value.translation_key == "file_not_found"


async def test_ocr_directory_is_not_a_file(
    hass: HomeAssistant, init_integration: MockConfigEntry, media_dir: Path
) -> None:
    """A directory is not an image."""
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "ocr", {"file_path": str(media_dir)})
    assert err.value.translation_key == "file_not_found"


async def test_ocr_without_source(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Either a camera or a file is required."""
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "ocr", {"instructions": "read"})
    assert err.value.translation_key == "ocr_source_missing"


async def test_ocr_both_sources_rejected(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The camera and the file are mutually exclusive."""
    with pytest.raises(probatio.Invalid):
        await call(
            hass,
            "ocr",
            {"camera_entity_id": "camera.front", "file_path": "/media/x.png"},
        )


async def test_ocr_api_error(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    media_dir: Path,
) -> None:
    """An API error is raised as a Home Assistant error."""
    scan = media_dir / "a.png"
    scan.write_bytes(b"x")
    mock_client.chat.side_effect = BladeApiError(500, "boom", "ocr down")
    with pytest.raises(HomeAssistantError, match="ocr down"):
        await call(hass, "ocr", {"file_path": str(scan)})


# ----------------------------------------------------------------- Translate


@pytest.mark.parametrize(
    ("data", "instruction"),
    [
        (
            {"text": "Hello", "target_language": "French"},
            "Translate the following text into French.",
        ),
        (
            {
                "text": "Hello",
                "target_language": "French",
                "source_language": "English",
            },
            "Translate the following text from English into French.",
        ),
    ],
)
async def test_translate(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    data: dict[str, str],
    instruction: str,
) -> None:
    """The text is sent to the translation model, with or without a source."""
    mock_client.chat.return_value = completion(" Bonjour \n")
    assert await call(hass, "translate", data) == {"text": "Bonjour"}
    body = mock_client.chat.await_args.args[0]
    assert body["model"] == "hy-mt2-7b"
    assert body["temperature"] == 0.2
    assert body["messages"] == [
        {
            "role": "user",
            "content": f"{instruction} Output only the translation.\n\nHello",
        }
    ]


async def test_translate_api_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A connection error is raised as a Home Assistant error."""
    mock_client.chat.side_effect = BladeConnectionError("offline")
    with pytest.raises(HomeAssistantError, match="offline"):
        await call(hass, "translate", {"text": "x", "target_language": "fr"})


# ------------------------------------------------------- Enabling and entries


@pytest.mark.parametrize(
    ("service", "option", "data"),
    [
        ("ocr", CONF_OCR_ENABLED, {"camera_entity_id": "camera.front"}),
        ("translate", CONF_TRANSLATE_ENABLED, {"text": "x", "target_language": "fr"}),
        ("forecast", CONF_FORECAST_ENABLED, {"entity_id": "sensor.temp"}),
    ],
)
async def test_action_disabled_in_options(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    service: str,
    option: str,
    data: dict[str, Any],
) -> None:
    """An action switched off in the options is refused."""
    hass.config_entries.async_update_entry(
        init_integration, options={**DEFAULT_ENTRY_OPTIONS, option: False}
    )
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, service, data)
    assert err.value.translation_key == "action_disabled"
    assert err.value.translation_placeholders == {"action": service}


async def test_options_missing_fall_back_to_defaults(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """An entry without stored options still has every action enabled."""
    hass.config_entries.async_update_entry(init_integration, options={})
    await hass.async_block_till_done()
    mock_client.chat.return_value = completion("ok")
    assert await call(hass, "translate", {"text": "x", "target_language": "fr"}) == {
        "text": "ok"
    }


async def test_no_loaded_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The actions exist, but need a loaded entry."""
    mock_client.list_models.side_effect = BladeConnectionError("down")
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "translate")
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "translate", {"text": "x", "target_language": "fr"})
    assert err.value.translation_key == "entry_not_loaded"


async def test_config_entry_id_selects_the_entry(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
) -> None:
    """With two entries, config_entry_id picks which one answers."""
    second = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_KEY: "other-key"},
        options={**DEFAULT_ENTRY_OPTIONS, CONF_TRANSLATE_ENABLED: False},
    )
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()
    data = {"text": "x", "target_language": "fr"}

    mock_client.chat.return_value = completion("ok")
    assert await call(hass, "translate", data) == {"text": "ok"}
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "translate", {**data, "config_entry_id": second.entry_id})
    assert err.value.translation_key == "action_disabled"
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "translate", {**data, "config_entry_id": "missing"})
    assert err.value.translation_key == "entry_not_loaded"
