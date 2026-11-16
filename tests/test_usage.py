"""Tests for the spend tracker, its storage and its repair issue."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import ai_task, conversation, stt
from homeassistant.const import CONF_API_KEY
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.blade.api import (
    BladeApiError,
    BladeBudgetError,
    BladeModel,
    current_service,
)
from custom_components.blade.const import DEFAULT_ENTRY_OPTIONS, DOMAIN
from custom_components.blade.usage import (
    ACCOUNT_CODES,
    SAVE_DELAY,
    STATUS_BUDGET,
    STATUS_OK,
    BudgetReachedError,
    Period,
    UsageTracker,
    model_prices,
)

from .conftest import call, script_chat, text_round

BASE = "https://api.models.blade.sh/v1"
ENTRY_ID = "entry-1"
STORAGE_KEY = f"{DOMAIN}.usage.{ENTRY_ID}"
ISSUE_ID = f"account_{ENTRY_ID}"

# Batch price 0.50 / 1M in, 1.00 / 1M out at a 0.5 batch multiplier: list price
# is double (1.00 and 2.00), then spot makes it x1.5.
PRICED = BladeModel(
    "priced",
    "chat",
    batch={"multiplier": 0.5, "price_in": 0.5, "price_out": 1.0},
    spot_multiplier=1.5,
)


def tracker(hass: HomeAssistant, budget: float = 0.0) -> UsageTracker:
    """Return a fresh tracker."""
    return UsageTracker(hass, ENTRY_ID, budget)


def issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    """Return the account issue, if any."""
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID)


# ------------------------------------------------------------------- Prices


def test_prices_without_model_or_fallback() -> None:
    """An unknown model without catalog entry has no price."""
    assert model_prices(None, "nope") == {}


def test_prices_fallback_without_catalog() -> None:
    """Models with a published price work without a catalog entry."""
    assert model_prices(None, "kokoro-82m") == {"audio_min": 0.00798}


def test_prices_batch_all_fields() -> None:
    """The list price is the batch price divided by the batch multiplier."""
    model = BladeModel(
        "m",
        "chat",
        batch={
            "multiplier": 0.5,
            "price_in": 1,
            "price_out": 2,
            "price_audio_min": 0.1,
            "price_image": 0.04,
        },
    )
    assert model_prices(model, "m") == {
        "in": 2.0,
        "out": 4.0,
        "audio_min": 0.2,
        "image": 0.08,
    }


def test_prices_spot_multiplier() -> None:
    """The spot multiplier applies to the list price."""
    assert model_prices(PRICED, "priced") == {"in": 1.5, "out": 3.0}


def test_prices_batch_partial_overrides_fallback() -> None:
    """Missing batch fields keep the fallback; present ones replace it."""
    model = BladeModel(
        "clef-flash",
        "decision",
        batch={"multiplier": 1, "price_out": 3},
    )
    assert model_prices(model, "clef-flash") == {"in": 0.0217, "out": 3.0}


@pytest.mark.parametrize("batch", [{}, {"multiplier": 0, "price_in": 1}])
def test_prices_without_batch(batch: dict[str, Any]) -> None:
    """Without a usable batch multiplier only the fallback applies."""
    model = BladeModel("kyutai-tts-1.6b-en-fr", "speech", batch=batch)
    assert model_prices(model, "kyutai-tts-1.6b-en-fr") == {"audio_min": 0.01086}
    assert model_prices(BladeModel("x", "chat", batch=batch), "x") == {}


def test_prices_fallback_gets_spot() -> None:
    """Fallback prices are scaled by the spot multiplier too."""
    model = BladeModel("kokoro-82m", "speech", spot_multiplier=2.0)
    assert model_prices(model, "kokoro-82m") == {"audio_min": 0.01596}


# ------------------------------------------------------------------- Period


def test_period_from_dict_roundtrip() -> None:
    """A stored period with the same key is restored."""
    period = Period("2026-11")
    period.add("svc", 1.5, {"tokens_in": 10, "tokens_out": 5, "images": 2})
    period.add("svc", 0.5, {"audio_seconds": 12.34})
    restored = Period.from_dict(period.as_dict(), "2026-11")
    assert restored.cost == period.cost
    assert restored.by_service == {"svc": 2.0}
    assert (restored.tokens, restored.images) == (15, 2)
    assert restored.audio_seconds == 12.3


@pytest.mark.parametrize("data", [None, {}, {"key": "2026-10", "cost": 9}])
def test_period_from_dict_other_key(data: dict[str, Any] | None) -> None:
    """A missing or stale period starts empty."""
    assert Period.from_dict(data, "2026-11") == Period("2026-11")


def test_period_from_dict_missing_fields() -> None:
    """A stored period lacking fields is filled with zeros."""
    assert Period.from_dict({"key": "k"}, "k") == Period("k")


# ------------------------------------------------------------------- Record


async def test_record_cost(hass: HomeAssistant) -> None:
    """Tokens, audio, images and extra cost add up in both periods."""
    usage = tracker(hass)
    cost = usage.record(
        "svc", PRICED, "priced", tokens_in=1_000_000, tokens_out=500_000
    )
    assert cost == pytest.approx(1.5 + 1.5)
    cost = usage.record("svc", None, "kokoro-82m", audio_seconds=60)
    assert cost == pytest.approx(0.00798)
    cost = usage.record("other", None, "unknown", images=3, extra_cost=0.25)
    assert cost == pytest.approx(0.25)
    for period in (usage.day, usage.month):
        assert period.cost == pytest.approx(3.25798)
        assert period.tokens == 1_500_000
        assert period.images == 3
        assert period.audio_seconds == 60
        assert period.by_service["svc"] == pytest.approx(3.00798)
        assert period.by_service["other"] == pytest.approx(0.25)


async def test_listeners(hass: HomeAssistant) -> None:
    """Listeners hear every change until they are removed."""
    usage = tracker(hass)
    calls = MagicMock()
    remove = usage.async_add_listener(calls)
    usage.record("svc", None, "x", tokens_in=1)
    assert calls.call_count == 1
    usage.account_error("invoice_unpaid")
    assert calls.call_count == 3  # status change, then the usage update
    remove()
    usage.record("svc", None, "x", tokens_in=1)
    assert calls.call_count == 3


# ------------------------------------------------------------------ Rollover


async def test_day_rollover(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The day restarts at midnight, the month keeps counting."""
    freezer.move_to("2026-11-16 20:00:00")
    usage = tracker(hass)
    usage.record("svc", PRICED, "priced", tokens_in=1_000_000)
    freezer.move_to("2026-11-17 20:00:00")
    usage.record("svc", PRICED, "priced", tokens_in=1_000_000)
    assert usage.day.key == "2026-11-17"
    assert usage.day.cost == pytest.approx(1.5)
    assert usage.month.cost == pytest.approx(3.0)


async def test_month_rollover(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The month restarts on the first and frees a reached cap."""
    freezer.move_to("2026-11-30 20:00:00")
    usage = tracker(hass, budget=1.0)
    usage.record("svc", PRICED, "priced", tokens_in=1_000_000)
    with pytest.raises(BudgetReachedError):
        usage.check_budget()
    assert issue(hass) is not None
    freezer.move_to("2026-12-01 20:00:00")
    usage.check_budget()
    assert usage.month == Period("2026-12")
    assert usage.day == Period("2026-12-01")


# -------------------------------------------------------------------- Budget


async def test_budget_below_and_above(hass: HomeAssistant) -> None:
    """Calls pass under the cap and are refused once it is reached."""
    usage = tracker(hass, budget=2.0)
    usage.check_budget()
    usage.record("svc", PRICED, "priced", tokens_in=1_000_000)  # 1.5
    usage.check_budget()
    assert usage.status == STATUS_OK
    usage.record("svc", PRICED, "priced", tokens_in=1_000_000)  # 3.0
    with pytest.raises(BudgetReachedError):
        usage.check_budget()
    assert usage.status == STATUS_BUDGET
    created = issue(hass)
    assert created is not None
    assert created.translation_key == STATUS_BUDGET
    assert created.translation_placeholders == {
        "budget": "2.00",
        "console_url": "https://console.blade.sh",
    }
    assert created.severity is ir.IssueSeverity.ERROR
    assert created.is_fixable is False


async def test_budget_zero_means_no_cap(hass: HomeAssistant) -> None:
    """A cap of 0 never refuses anything."""
    usage = tracker(hass, budget=0)
    usage.record("svc", None, "x", extra_cost=1_000)
    usage.check_budget()
    assert usage.status == STATUS_OK
    assert issue(hass) is None


async def test_budget_raised_returns_to_ok(hass: HomeAssistant) -> None:
    """A successful call after the cap is lifted clears status and issue."""
    usage = tracker(hass, budget=1.0)
    usage.record("svc", None, "x", extra_cost=2)
    with pytest.raises(BudgetReachedError):
        usage.check_budget()
    usage.budget = 100.0
    usage.check_budget()
    usage.record("svc", None, "x", extra_cost=0)
    assert usage.status == STATUS_OK
    assert issue(hass) is None


async def test_record_keeps_budget_status_while_over_cap(hass: HomeAssistant) -> None:
    """A late usage report over the cap does not clear the cap status."""
    usage = tracker(hass, budget=1.0)
    usage.record("svc", None, "x", extra_cost=2)
    with pytest.raises(BudgetReachedError):
        usage.check_budget()
    usage.record("svc", None, "x", extra_cost=1)
    assert usage.status == STATUS_BUDGET
    assert issue(hass) is not None


async def test_record_clears_budget_status_when_no_cap(hass: HomeAssistant) -> None:
    """With the cap removed (0), the cap status is cleared by the next report."""
    usage = tracker(hass, budget=1.0)
    usage.record("svc", None, "x", extra_cost=2)
    with pytest.raises(BudgetReachedError):
        usage.check_budget()
    usage.budget = 0
    usage.record("svc", None, "x", extra_cost=1)
    assert usage.status == STATUS_OK


# ------------------------------------------------------------ Account errors


@pytest.mark.parametrize("code", ACCOUNT_CODES)
async def test_account_error_known_codes(hass: HomeAssistant, code: str) -> None:
    """Each known 402 code is kept and raises its own issue."""
    usage = tracker(hass)
    usage.account_error(code)
    assert usage.status == code
    created = issue(hass)
    assert created is not None
    assert created.translation_key == code


@pytest.mark.parametrize("code", ["something_new", None])
async def test_account_error_unknown_code(
    hass: HomeAssistant, code: str | None
) -> None:
    """Any other code is a generic payment error."""
    usage = tracker(hass)
    usage.account_error(code)
    assert usage.status == "payment_error"
    created = issue(hass)
    assert created is not None
    assert created.translation_key == "payment_error"


async def test_account_error_then_ok(hass: HomeAssistant) -> None:
    """The next call that goes through restores the status and clears the issue."""
    usage = tracker(hass)
    usage.account_error("insufficient_balance")
    usage.account_error("insufficient_balance")  # same status: nothing changes
    assert issue(hass) is not None
    usage.record("svc", None, "x", tokens_in=1)
    assert usage.status == STATUS_OK
    assert issue(hass) is None


# ------------------------------------------------------------------- Storage


async def test_persistence(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Totals are saved after a delay and restored by a new tracker."""
    freezer.move_to("2026-11-16 20:00:00")
    usage = tracker(hass)
    await usage.async_load()
    usage.record("svc", PRICED, "priced", tokens_in=1_000_000, images=1)
    assert STORAGE_KEY not in hass_storage
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SAVE_DELAY + 1))
    await hass.async_block_till_done()
    saved = hass_storage[STORAGE_KEY]["data"]
    assert saved["day"]["key"] == "2026-11-16"
    assert saved["month"]["cost"] == pytest.approx(1.5)

    restored = tracker(hass)
    await restored.async_load()
    assert restored.month == usage.month
    assert restored.day == usage.day


async def test_load_discards_old_periods(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Stored totals of a previous day or month are not carried over."""
    freezer.move_to("2026-12-02 20:00:00")
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "day": {"key": "2026-12-01", "cost": 1},
            "month": {"key": "2026-12", "cost": 4, "tokens": 7},
        },
    }
    usage = tracker(hass)
    await usage.async_load()
    assert usage.day == Period("2026-12-02")
    assert usage.month.cost == 4
    assert usage.month.tokens == 7


async def test_load_empty_store(hass: HomeAssistant) -> None:
    """Without a stored file the tracker starts at zero."""
    usage = tracker(hass)
    await usage.async_load()
    assert usage.month.cost == 0
    assert usage.status == STATUS_OK


# --------------------------------------------------------- Setup integration


async def test_setup_budget_from_options(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The cap comes from the entry options."""
    hass.config_entries.async_update_entry(
        mock_config_entry, options={**mock_config_entry.options, "monthly_budget": 7}
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.runtime_data.usage.budget == 7.0


async def test_hook_before_call_budget(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The cap turns into a BladeBudgetError before any request."""
    usage = init_integration.runtime_data.usage
    mock_client.hooks.before_call()  # no cap: passes
    usage.budget = 1.0
    usage.record("svc", None, "x", extra_cost=2)
    with pytest.raises(BladeBudgetError, match="budget") as err:
        mock_client.hooks.before_call()
    assert isinstance(err.value.__cause__, BudgetReachedError)


async def test_hook_usage_and_account_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Usage is priced from the catalog; 402 codes reach the tracker."""
    usage = init_integration.runtime_data.usage
    init_integration.runtime_data.models["priced"] = PRICED
    mock_client.hooks.usage("priced", tokens_in=1_000_000.0)
    assert usage.month.cost == pytest.approx(1.5)
    assert usage.month.by_service == {"actions": pytest.approx(1.5)}
    mock_client.hooks.usage("missing-from-catalog", tokens_in=5.0)
    assert usage.month.tokens == 1_000_005
    mock_client.hooks.account_error("paid_credit_required")
    assert usage.status == "paid_credit_required"


# ------------------------------------------------------ Labels and real client


def capture(seen: list[str], result: Any) -> Any:
    """Return a mock side effect noting the service label of each call."""

    def side_effect(*args: Any, **kwargs: Any) -> Any:
        seen.append(current_service.get())
        return result

    return side_effect


async def test_label_conversation(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The conversation labels its calls with the subentry title."""
    seen: list[str] = []
    script_chat(mock_client, text_round("ok"))
    inner = mock_client.chat_stream.side_effect

    def spy(body: dict[str, Any]) -> Any:
        seen.append(current_service.get())
        return inner(body)

    mock_client.chat_stream.side_effect = spy
    await conversation.async_converse(
        hass,
        "hi",
        None,
        Context(),
        agent_id="conversation.blade_conversation",
    )
    assert seen == ["Blade Conversation"]


async def test_label_translate_action(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Actions are labelled with their service name."""
    seen: list[str] = []
    mock_client.chat.side_effect = capture(
        seen, {"choices": [{"message": {"content": "Bonjour"}}]}
    )
    await call(hass, "translate", {"text": "Hello", "target_language": "fr"})
    assert seen == ["blade.translate"]


async def test_label_tts_stt_image(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Speech, transcription and image entities use their subentry titles."""
    seen: list[str] = []
    mock_client.speech.side_effect = capture(seen, b"RIFFwav")
    mock_client.transcribe.side_effect = capture(seen, "text")
    mock_client.generate_image.side_effect = capture(seen, b"\x89PNG....")
    tts_entity = hass.data["entity_components"]["tts"].get_entity("tts.blade_tts")
    await tts_entity.async_get_tts_audio("Hi", "fr-FR", tts_entity.default_options)
    stt_entity = hass.data["entity_components"]["stt"].get_entity("stt.blade_stt")

    async def pcm() -> AsyncIterator[bytes]:
        yield b"\x00\x00" * 10

    await stt_entity.async_process_audio_stream(
        stt.SpeechMetadata(
            language="fr-FR",
            format=stt.AudioFormats.WAV,
            codec=stt.AudioCodecs.PCM,
            bit_rate=stt.AudioBitRates.BITRATE_16,
            sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
            channel=stt.AudioChannels.CHANNEL_MONO,
        ),
        pcm(),
    )
    await ai_task.async_generate_image(
        hass,
        task_name="cat",
        entity_id="ai_task.blade_ai_task",
        instructions="A cat",
    )
    assert seen == ["Blade TTS", "Blade STT", "Blade AI Task"]


async def setup_real_client(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, budget: float = 0.0
) -> MockConfigEntry:
    """Load the entry with the real client talking to the mocked HTTP layer."""
    aioclient_mock.get(
        f"{BASE}/models",
        json={
            "data": [
                {
                    "id": "priced",
                    "kind": "chat",
                    "batch": {"multiplier": 1, "price_in": 2, "price_out": 4},
                }
            ]
        },
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_KEY: "k"},
        options={**DEFAULT_ENTRY_OPTIONS, "monthly_budget": budget},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_real_402_reaches_tracker(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 402 from the HTTP layer sets the status, the sensor and the issue."""
    entry = await setup_real_client(hass, aioclient_mock)
    aioclient_mock.post(
        f"{BASE}/chat/completions",
        status=402,
        json={"error": {"code": "invoice_unpaid", "message": "pay"}},
    )
    with pytest.raises(BladeApiError):
        await entry.runtime_data.client.chat({"model": "priced"})
    await hass.async_block_till_done()
    assert entry.runtime_data.usage.status == "invoice_unpaid"
    assert hass.states.get("sensor.blade_account_status").state == "invoice_unpaid"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"account_{entry.entry_id}")

    aioclient_mock.clear_requests()
    aioclient_mock.post(
        f"{BASE}/chat/completions",
        json={"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}},
    )
    await entry.runtime_data.client.chat({"model": "priced"})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.blade_account_status").state == "ok"
    assert hass.states.get("sensor.blade_estimated_cost_this_month").state == "2.0"
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"account_{entry.entry_id}") is None
    )


async def test_real_budget_blocks_requests(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Once the cap is reached no further request leaves Home Assistant."""
    entry = await setup_real_client(hass, aioclient_mock, budget=1.0)
    client = entry.runtime_data.client
    aioclient_mock.post(
        f"{BASE}/chat/completions",
        json={"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}},
    )
    await client.chat({"model": "priced"})
    calls = aioclient_mock.call_count
    with pytest.raises(BladeBudgetError):
        await client.chat({"model": "priced"})
    assert aioclient_mock.call_count == calls
    await hass.async_block_till_done()
    assert (
        hass.states.get("sensor.blade_account_status").state == "local_budget_reached"
    )
    assert (
        hass.states.get("sensor.blade_estimated_cost_this_month").attributes[
            "remaining"
        ]
        == 0
    )


# ------------------------------------------------- Midnight, reload, removal


async def test_midnight_reset_without_calls(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Totals and the cap status reset at midnight even when nothing is called."""
    await hass.config.async_set_time_zone("Europe/Paris")
    freezer.move_to("2026-11-30 23:59:00+01:00")
    usage = tracker(hass, budget=1.0)
    usage.record("svc", None, "m", extra_cost=2.0)
    with pytest.raises(BudgetReachedError):
        usage.check_budget()
    changes: list[None] = []
    usage.async_add_listener(lambda: changes.append(None))

    usage.async_roll()
    assert usage.status == STATUS_BUDGET
    assert not changes

    freezer.move_to("2026-12-01 00:00:05+01:00")
    usage.async_roll()
    assert usage.day.cost == 0
    assert usage.month.cost == 0
    assert usage.status == STATUS_OK
    assert changes


async def test_midnight_keeps_budget_status_when_still_over(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A new day in the same month keeps the cap if the month is still over it."""
    await hass.config.async_set_time_zone("Europe/Paris")
    freezer.move_to("2026-11-20 23:59:00+01:00")
    usage = tracker(hass, budget=1.0)
    usage.record("svc", None, "m", extra_cost=2.0)
    with pytest.raises(BudgetReachedError):
        usage.check_budget()
    freezer.move_to("2026-11-21 00:00:05+01:00")
    usage.async_roll()
    assert usage.day.cost == 0
    assert usage.status == STATUS_BUDGET


async def test_load_clears_stale_issue_and_restores_cap(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """On load, an old issue is dropped and the cap is re-applied if still due."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        "account_entry",
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="invoice_unpaid",
    )
    month = dt_util.now().strftime("%Y-%m")
    hass_storage[f"{DOMAIN}.usage.entry"] = {
        "version": 1,
        "data": {"month": {"key": month, "cost": 5.0}},
    }
    usage = UsageTracker(hass, "entry", 2.0)
    await usage.async_load()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "account_entry")
    assert usage.status == STATUS_BUDGET
    assert issue is not None
    assert issue.translation_key == STATUS_BUDGET

    relaxed = UsageTracker(hass, "entry", 10.0)
    await relaxed.async_load()
    assert relaxed.status == STATUS_OK
    assert ir.async_get(hass).async_get_issue(DOMAIN, "account_entry") is None


async def test_scheduled_roll_and_removal(
    hass: HomeAssistant,
    mock_client: MagicMock,
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Midnight triggers the roll; removing the entry deletes the stored totals."""
    await hass.config.async_set_time_zone("Europe/Paris")
    freezer.move_to("2026-11-16 23:59:58+01:00")
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_API_KEY: "k"}, options=dict(DEFAULT_ENTRY_OPTIONS)
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    usage = entry.runtime_data.usage
    usage.record("svc", None, "m", extra_cost=1.0)
    freezer.tick(timedelta(seconds=4))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert usage.day.cost == 0

    freezer.tick(timedelta(seconds=SAVE_DELAY + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.usage.{entry.entry_id}" in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.usage.{entry.entry_id}" not in hass_storage


async def test_cached_tokens_counted_at_full_price(hass: HomeAssistant) -> None:
    """Cached input is counted apart but priced in full."""
    usage = tracker(hass)
    model = BladeModel(
        "m", "chat", batch={"multiplier": 0.5, "price_in": 0.5}, spot_multiplier=1.0
    )
    cost = usage.record("svc", model, "m", tokens_in=1_000_000, tokens_cached=400_000)
    assert cost == pytest.approx(1.0)
    assert usage.month.cached_tokens == 400_000
    restored = Period.from_dict(usage.month.as_dict(), usage.month.key)
    assert restored.cached_tokens == 400_000


async def test_catalog_refreshed_every_hour(
    hass: HomeAssistant, mock_client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """Prices follow the hourly catalog; a failed refresh keeps the old one."""
    from custom_components.blade.api import BladeConnectionError  # noqa: PLC0415

    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_API_KEY: "k"}, options=dict(DEFAULT_ENTRY_OPTIONS)
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    models = entry.runtime_data.models
    fresh = BladeModel("new-model", "chat", spot_multiplier=2.0)
    mock_client.list_models.return_value = {fresh.id: fresh}
    freezer.tick(timedelta(hours=1, seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert set(models) == {"new-model"}

    mock_client.list_models.side_effect = BladeConnectionError("down")
    freezer.tick(timedelta(hours=1, seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert set(models) == {"new-model"}
