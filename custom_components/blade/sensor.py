"""Estimated spend and account status of a Blade account."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .usage import STATUSES, UsageTracker

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import BladeConfigEntry

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class BladeSensorDescription(SensorEntityDescription):
    """How to read one value from the tracker."""

    value: Callable[[UsageTracker], Any]
    attributes: Callable[[UsageTracker], dict[str, Any]] = lambda _: {}


def _budget_attributes(tracker: UsageTracker) -> dict[str, Any]:
    attributes: dict[str, Any] = {"by_service": tracker.month.as_dict()["by_service"]}
    if tracker.budget > 0:
        attributes["budget"] = tracker.budget
        attributes["remaining"] = round(max(tracker.budget - tracker.month.cost, 0), 4)
    return attributes


SENSORS: tuple[BladeSensorDescription, ...] = (
    BladeSensorDescription(
        key="cost_today",
        translation_key="cost_today",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=4,
        value=lambda t: round(t.day.cost, 6),
        attributes=lambda t: {"by_service": t.day.as_dict()["by_service"]},
    ),
    BladeSensorDescription(
        key="cost_month",
        translation_key="cost_month",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="EUR",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=4,
        value=lambda t: round(t.month.cost, 6),
        attributes=_budget_attributes,
    ),
    BladeSensorDescription(
        key="tokens_month",
        translation_key="tokens_month",
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="tokens",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda t: t.month.tokens,
        attributes=lambda t: {
            "cached_tokens": t.month.cached_tokens,
            "audio_seconds": t.month.as_dict()["audio_seconds"],
            "images": t.month.images,
        },
    ),
    BladeSensorDescription(
        key="account_status",
        translation_key="account_status",
        device_class=SensorDeviceClass.ENUM,
        options=STATUSES,
        value=lambda t: t.status,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: BladeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the account sensors."""
    async_add_entities(
        BladeUsageSensor(config_entry, description) for description in SENSORS
    )


class BladeUsageSensor(SensorEntity):
    """One figure of the account, refreshed after every Blade call."""

    entity_description: BladeSensorDescription
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, entry: BladeConfigEntry, description: BladeSensorDescription
    ) -> None:
        """Attach to the account device."""
        self.entity_description = description
        self._tracker = entry.runtime_data.usage
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Blade",
            manufacturer="Blade",
            model="Account",
            entry_type=dr.DeviceEntryType.SERVICE,
            configuration_url="https://console.blade.sh",
        )

    @override
    async def async_added_to_hass(self) -> None:
        """Follow the tracker."""
        self.async_on_remove(
            self._tracker.async_add_listener(self.async_write_ha_state)
        )

    @property
    @override
    def native_value(self) -> Any:
        """Return the current figure."""
        return self.entity_description.value(self._tracker)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the breakdown."""
        return self.entity_description.attributes(self._tracker)
