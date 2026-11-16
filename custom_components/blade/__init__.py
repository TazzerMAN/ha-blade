"""The Blade integration: European OpenAI-compatible inference for Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.typing import ConfigType

from .api import (
    BladeAuthenticationError,
    BladeBudgetError,
    BladeClient,
    BladeError,
    BladeModel,
    current_service,
)
from .const import (
    CONF_CREATE_PIPELINE,
    CONF_MONTHLY_BUDGET,
    DEFAULT_ENTRY_OPTIONS,
    DOMAIN,
    PIPELINE_NAME,
)
from .services import async_setup_services
from .usage import BudgetReachedError, UsageTracker

PLATFORMS = [
    Platform.AI_TASK,
    Platform.CONVERSATION,
    Platform.SENSOR,
    Platform.STT,
    Platform.TTS,
]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
CATALOG_REFRESH = timedelta(hours=1)


@dataclass(slots=True)
class BladeRuntimeData:
    """Objects shared by every platform of one Blade account."""

    client: BladeClient
    models: dict[str, BladeModel]
    usage: UsageTracker


type BladeConfigEntry = ConfigEntry[BladeRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the Blade actions."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BladeConfigEntry) -> bool:
    """Check the key, load the catalog and set up the platforms."""
    client = BladeClient(async_get_clientsession(hass), entry.data[CONF_API_KEY])
    try:
        models = await client.list_models()
    except BladeAuthenticationError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except BladeError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err

    options = {**DEFAULT_ENTRY_OPTIONS, **entry.options}
    tracker = UsageTracker(hass, entry.entry_id, float(options[CONF_MONTHLY_BUDGET]))
    await tracker.async_load()
    client.hooks = _UsageHooks(tracker, models)
    entry.async_on_unload(
        async_track_time_change(
            hass, callback(lambda _: tracker.async_roll()), hour=0, minute=0, second=1
        )
    )

    async def refresh_catalog(_: datetime) -> None:
        # Prices follow GPU demand; the multiplier in the catalog moves with it.
        try:
            fresh = await client.list_models()
        except BladeError:
            return
        models.clear()
        models.update(fresh)

    entry.async_on_unload(
        async_track_time_interval(hass, refresh_catalog, CATALOG_REFRESH)
    )
    entry.runtime_data = BladeRuntimeData(client=client, models=models, usage=tracker)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if entry.data.get(CONF_CREATE_PIPELINE):
        await _async_create_pipeline(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


class _UsageHooks:
    """Feed the client's usage reports to the tracker."""

    def __init__(self, tracker: UsageTracker, models: dict[str, BladeModel]) -> None:
        self._tracker = tracker
        self._models = models

    def before_call(self) -> None:
        try:
            self._tracker.check_budget()
        except BudgetReachedError as err:
            raise BladeBudgetError(
                "monthly budget set in Home Assistant reached"
            ) from err

    def usage(self, model: str, **metrics: float) -> None:
        self._tracker.record(
            current_service.get(), self._models.get(model), model, **metrics
        )

    def account_error(self, code: str | None) -> None:
        self._tracker.account_error(code)


async def _async_create_pipeline(hass: HomeAssistant, entry: BladeConfigEntry) -> None:
    """Build the "Blade" voice assistant once, from this entry's own entities."""
    data = {k: v for k, v in entry.data.items() if k != CONF_CREATE_PIPELINE}
    hass.config_entries.async_update_entry(entry, data=data)
    if "assist_pipeline" not in hass.config.components:
        return
    from homeassistant.components.assist_pipeline import (  # noqa: PLC0415
        async_create_default_pipeline,
        async_get_pipelines,
        async_update_pipeline,
    )

    engines = {
        entity.domain: entity.entity_id
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }
    if not {"stt", "tts", "conversation"} <= engines.keys():
        return
    # A reinstall reuses the pipeline left by the previous one.
    pipeline = next(
        (p for p in async_get_pipelines(hass) if p.name == PIPELINE_NAME), None
    ) or await async_create_default_pipeline(
        hass,
        stt_engine_id=engines["stt"],
        tts_engine_id=engines["tts"],
        pipeline_name=PIPELINE_NAME,
    )
    if pipeline is not None:
        await async_update_pipeline(
            hass,
            pipeline,
            conversation_engine=engines["conversation"],
            stt_engine=engines["stt"],
            tts_engine=engines["tts"],
        )


async def _async_update_listener(hass: HomeAssistant, entry: BladeConfigEntry) -> None:
    """Reload when options or subentries change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: BladeConfigEntry) -> None:
    """Forget the stored spend figures of a removed account."""
    await UsageTracker(hass, entry.entry_id, 0).async_remove()


async def async_unload_entry(hass: HomeAssistant, entry: BladeConfigEntry) -> bool:
    """Unload the platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
