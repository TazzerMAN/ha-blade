"""Tests for the blade.forecast action, which needs the recorder."""

from __future__ import annotations

import functools
import inspect
from annotationlib import Format
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import probatio
import pytest
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from custom_components.blade.api import BladeApiError
from custom_components.blade.services import QUANTILES

from .conftest import call


@pytest.fixture
def recorder_annotations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let mock autospec recorder functions annotated with TYPE_CHECKING names.

    Python 3.14.2 evaluates annotations in inspect.signature, so the harness's
    own autospec of recorder.migration._find_schema_errors raises NameError.
    """
    monkeypatch.setattr(
        inspect,
        "signature",
        functools.partial(inspect.signature, annotation_format=Format.FORWARDREF),
    )


@pytest.fixture(autouse=True)
async def setup_ha(
    recorder_annotations: None, recorder_mock: Any, hass: HomeAssistant
) -> None:
    """Start the recorder before Home Assistant, as its fixture requires."""
    from homeassistant.setup import async_setup_component  # noqa: PLC0415

    assert await async_setup_component(hass, "homeassistant", {})


def import_hourly(
    hass: HomeAssistant,
    entity_id: str,
    values: list[tuple[float | None, float | None]],
    *,
    total: bool = False,
) -> None:
    """Import one hourly statistic per (mean, state), ending at the last full hour.

    A total sensor keeps a state and a sum but no mean.
    """
    last = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(
        hours=1
    )
    count = len(values)
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE
            if total
            else StatisticMeanType.ARITHMETIC,
            "has_sum": total,
            "name": None,
            "source": "recorder",
            "statistic_id": entity_id,
            "unit_class": None,
            "unit_of_measurement": "°C",
        },
        [
            {
                "start": last - timedelta(hours=count - 1 - index),
                "mean": mean,
                "state": state,
                **({"sum": float(index)} if total else {}),
            }
            for index, (mean, state) in enumerate(values)
        ],
    )


@pytest.fixture
def forecast_result() -> list[dict[str, Any]]:
    """Return what the forecast endpoint answers for one series."""
    return [
        {
            "quantiles": {
                "0.1": [[1.00001, 2.0, 3.0]],
                "0.5": [[5.123456, 6.0, 7.0]],
                "0.9": [[9.0, 10.0, 11.0]],
            }
        }
    ]


async def test_forecast(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    forecast_result: list[dict[str, Any]],
) -> None:
    """Hourly means feed the model and the quantiles come back as a timeline."""
    import_hourly(hass, "sensor.temp", [(float(i), None) for i in range(30)])
    await async_wait_recording_done(hass)
    mock_client.forecast.return_value = forecast_result

    result = await call(
        hass,
        "forecast",
        {"entity_id": "sensor.temp", "history_hours": 48, "horizon_hours": 3},
    )

    model, context, horizon, quantiles = mock_client.forecast.await_args.args
    assert model == "timestral-28m"
    assert horizon == 3
    assert quantiles == QUANTILES
    (series,) = context
    assert len(series) == 30
    assert series[:5] == [0.0, 1.0, 2.0, 3.0, 4.0]

    last_end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    assert result["entity_id"] == "sensor.temp"
    assert result["forecast"] == [
        {
            "datetime": (last_end + timedelta(hours=step)).isoformat(),
            "value": value,
            "low": low,
            "high": high,
        }
        for step, (value, low, high) in enumerate(
            [(5.1235, 1.0, 9.0), (6.0, 2.0, 10.0), (7.0, 3.0, 11.0)]
        )
    ]


async def test_forecast_total_sensor_uses_state(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    forecast_result: list[dict[str, Any]],
) -> None:
    """A sensor without a mean (a total) is forecast from its state."""
    import_hourly(
        hass, "sensor.energy", [(None, 100.0 + i) for i in range(30)], total=True
    )
    await async_wait_recording_done(hass)
    mock_client.forecast.return_value = forecast_result
    await call(hass, "forecast", {"entity_id": "sensor.energy", "horizon_hours": 3})
    (series,) = mock_client.forecast.await_args.args[1]
    assert series[:3] == [100.0, 101.0, 102.0]


async def test_forecast_default_window(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    forecast_result: list[dict[str, Any]],
) -> None:
    """History and horizon default to a week and a day."""
    import_hourly(hass, "sensor.temp", [(1.0, None)] * 30)
    await async_wait_recording_done(hass)
    forecast_result[0]["quantiles"] = {
        level: [[0.0] * 24] for level in ("0.1", "0.5", "0.9")
    }
    mock_client.forecast.return_value = forecast_result
    result = await call(hass, "forecast", {"entity_id": "sensor.temp"})
    assert len(result["forecast"]) == 24
    assert mock_client.forecast.await_args.args[2] == 24


@pytest.mark.parametrize(
    "values",
    [
        [],
        [(1.0, None)] * 23,
        # Enough rows, but too few usable values.
        [(None, None)] * 30,
    ],
)
async def test_forecast_not_enough_history(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    values: list[tuple[float | None, float | None]],
) -> None:
    """Fewer than 24 hourly points is refused before any request."""
    if values:
        import_hourly(hass, "sensor.temp", values)
        await async_wait_recording_done(hass)
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "forecast", {"entity_id": "sensor.temp"})
    assert err.value.translation_key == "not_enough_history"
    assert err.value.translation_placeholders == {"entity_id": "sensor.temp"}
    mock_client.forecast.assert_not_called()


async def test_forecast_api_error(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
) -> None:
    """An API error is raised as a Home Assistant error."""
    import_hourly(hass, "sensor.temp", [(1.0, None)] * 30)
    await async_wait_recording_done(hass)
    mock_client.forecast.side_effect = BladeApiError(500, "boom", "gpu busy")
    with pytest.raises(HomeAssistantError, match="gpu busy"):
        await call(hass, "forecast", {"entity_id": "sensor.temp"})


@pytest.mark.parametrize(
    "data",
    [
        {"entity_id": "sensor.temp", "history_hours": 3},
        {"entity_id": "sensor.temp", "horizon_hours": 0},
        {"entity_id": "sensor.temp", "horizon_hours": 24 * 14 + 1},
    ],
)
async def test_forecast_range_validation(
    hass: HomeAssistant, init_integration: MockConfigEntry, data: dict[str, Any]
) -> None:
    """Out-of-range windows are rejected by the schema."""
    with pytest.raises(probatio.Invalid):
        await call(hass, "forecast", data)
