"""Diagnostics for Blade."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import BladeConfigEntry

TO_REDACT = {CONF_API_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BladeConfigEntry
) -> dict[str, Any]:
    """Return the configuration and the catalog size, never the key."""
    models = entry.runtime_data.models
    return {
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "subentries": {
            subentry_id: {
                "type": subentry.subentry_type,
                "title": subentry.title,
                "data": dict(subentry.data),
                "model_listed": subentry.data.get("model") in models,
            }
            for subentry_id, subentry in entry.subentries.items()
        },
        "catalog": {
            kind: sorted(m.id for m in models.values() if m.kind == kind)
            for kind in sorted({m.kind for m in models.values()})
        },
    }
