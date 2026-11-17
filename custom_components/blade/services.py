"""Blade actions: OCR, translation and time-series forecast."""

from __future__ import annotations

import base64
from datetime import timedelta
from mimetypes import guess_file_type
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import probatio
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_CONFIG_ENTRY_ID, ATTR_ENTITY_ID
from homeassistant.core import (
    HassJobType,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .api import BladeError, current_service
from .const import (
    CONF_FORECAST_ENABLED,
    CONF_FORECAST_MODEL,
    CONF_OCR_ENABLED,
    CONF_OCR_MODEL,
    CONF_TRANSLATE_ENABLED,
    CONF_TRANSLATE_MODEL,
    DEFAULT_ENTRY_OPTIONS,
    DOMAIN,
)
from .entity import blade_error

if TYPE_CHECKING:
    from . import BladeConfigEntry

SERVICE_OCR: Final = "ocr"
SERVICE_TRANSLATE: Final = "translate"
SERVICE_FORECAST: Final = "forecast"

ATTR_CAMERA: Final = "camera_entity_id"
ATTR_FILE: Final = "file_path"
ATTR_INSTRUCTIONS: Final = "instructions"
ATTR_TEXT: Final = "text"
ATTR_TARGET: Final = "target_language"
ATTR_SOURCE: Final = "source_language"
ATTR_HISTORY: Final = "history_hours"
ATTR_HORIZON: Final = "horizon_hours"

DEFAULT_OCR_PROMPT: Final = "Convert this document to Markdown. Output only the text."
QUANTILES: Final = [0.1, 0.5, 0.9]
# Below this many hourly points a forecast is noise.
MIN_HISTORY_POINTS: Final = 24

_ENTRY = probatio.Optional(ATTR_CONFIG_ENTRY_ID)
OCR_SCHEMA = probatio.Schema(
    {
        _ENTRY: cv.string,
        probatio.Exclusive(ATTR_CAMERA, "source"): cv.entity_id,
        probatio.Exclusive(ATTR_FILE, "source"): cv.string,
        probatio.Optional(ATTR_INSTRUCTIONS): cv.string,
    }
)
TRANSLATE_SCHEMA = probatio.Schema(
    {
        _ENTRY: cv.string,
        probatio.Required(ATTR_TEXT): cv.string,
        probatio.Required(ATTR_TARGET): cv.string,
        probatio.Optional(ATTR_SOURCE): cv.string,
    }
)
FORECAST_SCHEMA = probatio.Schema(
    {
        _ENTRY: cv.string,
        probatio.Required(ATTR_ENTITY_ID): cv.entity_id,
        probatio.Optional(ATTR_HISTORY, default=168): probatio.All(
            int, probatio.Range(min=MIN_HISTORY_POINTS, max=24 * 365)
        ),
        probatio.Optional(ATTR_HORIZON, default=24): probatio.All(
            int, probatio.Range(min=1, max=24 * 14)
        ),
    }
)


def _get_entry(call: ServiceCall, enabled_option: str) -> BladeConfigEntry:
    """Find the loaded Blade entry the call targets and check the action is on."""
    entries: list[BladeConfigEntry] = [
        entry
        for entry in call.hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        entries = [entry for entry in entries if entry.entry_id == entry_id]
    if not entries:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="entry_not_loaded"
        )
    entry = entries[0]
    current_service.set(f"{DOMAIN}.{call.service}")
    if not {**DEFAULT_ENTRY_OPTIONS, **entry.options}[enabled_option]:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="action_disabled",
            translation_placeholders={"action": call.service},
        )
    return entry


def _option(entry: BladeConfigEntry, key: str) -> Any:
    return {**DEFAULT_ENTRY_OPTIONS, **entry.options}[key]


async def _chat_text(entry: BladeConfigEntry, body: dict[str, Any]) -> str:
    try:
        result = await entry.runtime_data.client.chat(body)
    except BladeError as err:
        raise blade_error(err, str(body["model"])) from err
    return str(result["choices"][0]["message"].get("content") or "").strip()


async def _load_image(hass: HomeAssistant, call: ServiceCall) -> tuple[bytes, str]:
    """Return the image bytes and MIME type named by the call."""
    if camera_id := call.data.get(ATTR_CAMERA):
        from homeassistant.components.camera import async_get_image  # noqa: PLC0415

        image = await async_get_image(hass, camera_id)
        return image.content, image.content_type
    if file_path := call.data.get(ATTR_FILE):
        path = Path(file_path)
        if not hass.config.is_allowed_path(str(path)):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="path_not_allowed",
                translation_placeholders={"file_path": file_path},
            )
        if not await hass.async_add_executor_job(path.is_file):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="file_not_found",
                translation_placeholders={"file_path": file_path},
            )
        data = await hass.async_add_executor_job(path.read_bytes)
        return data, guess_file_type(path)[0] or "image/png"
    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key="ocr_source_missing"
    )


async def async_ocr(call: ServiceCall) -> ServiceResponse:
    """Read the text of a camera snapshot or an image file."""
    entry = _get_entry(call, CONF_OCR_ENABLED)
    data, mime = await _load_image(call.hass, call)
    encoded = base64.b64encode(data).decode()
    text = await _chat_text(
        entry,
        {
            "model": _option(entry, CONF_OCR_MODEL),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": call.data.get(ATTR_INSTRUCTIONS)
                            or DEFAULT_OCR_PROMPT,
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{encoded}"},
                        },
                    ],
                }
            ],
        },
    )
    return {"text": text}


async def async_translate(call: ServiceCall) -> ServiceResponse:
    """Translate a text with a dedicated translation model."""
    entry = _get_entry(call, CONF_TRANSLATE_ENABLED)
    source = call.data.get(ATTR_SOURCE)
    instruction = (
        f"Translate the following text from {source} into {call.data[ATTR_TARGET]}."
        if source
        else f"Translate the following text into {call.data[ATTR_TARGET]}."
    )
    text = await _chat_text(
        entry,
        {
            "model": _option(entry, CONF_TRANSLATE_MODEL),
            "messages": [
                {
                    "role": "user",
                    "content": f"{instruction} Output only the translation.\n\n"
                    f"{call.data[ATTR_TEXT]}",
                }
            ],
            "temperature": 0.2,
        },
    )
    return {"text": text}


async def async_forecast(call: ServiceCall) -> ServiceResponse:
    """Forecast a sensor from its hourly long-term statistics."""
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
        statistics_during_period,
    )

    entry = _get_entry(call, CONF_FORECAST_ENABLED)
    hass = call.hass
    entity_id: str = call.data[ATTR_ENTITY_ID]
    horizon: int = call.data[ATTR_HORIZON]
    start = dt_util.utcnow() - timedelta(hours=call.data[ATTR_HISTORY])
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        None,
        {entity_id},
        "hour",
        None,
        {"mean", "state"},
    )
    rows = stats.get(entity_id, [])
    series = [
        row.get("mean") if row.get("mean") is not None else row.get("state")
        for row in rows
    ]
    if sum(value is not None for value in series) < MIN_HISTORY_POINTS:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="not_enough_history",
            translation_placeholders={"entity_id": entity_id},
        )
    try:
        result = await entry.runtime_data.client.forecast(
            _option(entry, CONF_FORECAST_MODEL), [series], horizon, QUANTILES
        )
    except BladeError as err:
        raise blade_error(err, _option(entry, CONF_FORECAST_MODEL)) from err

    quantiles = result[0]["quantiles"]
    low, median, high = (quantiles[str(level)][0] for level in QUANTILES)
    last_end = dt_util.utc_from_timestamp(rows[-1]["end"])
    return {
        "entity_id": entity_id,
        "forecast": [
            {
                "datetime": (last_end + timedelta(hours=step)).isoformat(),
                "value": round(median[step], 4),
                "low": round(low[step], 4),
                "high": round(high[step], 4),
            }
            for step in range(horizon)
        ],
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Blade actions once for all entries."""
    for name, handler, schema in (
        (SERVICE_OCR, async_ocr, OCR_SCHEMA),
        (SERVICE_TRANSLATE, async_translate, TRANSLATE_SCHEMA),
        (SERVICE_FORECAST, async_forecast, FORECAST_SCHEMA),
    ):
        hass.services.async_register(
            DOMAIN,
            name,
            handler,
            schema=schema,
            supports_response=SupportsResponse.ONLY,
            job_type=HassJobType.Coroutinefunction,
        )
