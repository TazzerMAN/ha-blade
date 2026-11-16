"""Estimated spend, monthly cap and account status for one Blade account.

Blade does not expose the wallet to API keys, so cost is estimated from the
usage each response reports and the catalog prices.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

if TYPE_CHECKING:
    from .api import BladeModel

STORAGE_VERSION: Final = 1
SAVE_DELAY: Final = 30
STATUS_OK: Final = "ok"
STATUS_BUDGET: Final = "local_budget_reached"
# Blade 402 codes; anything else maps to "payment_error".
ACCOUNT_CODES: Final = (
    "insufficient_balance",
    "paid_credit_required",
    "invoice_unpaid",
    "monthly_budget_exceeded",
)
STATUSES: Final = [STATUS_OK, *ACCOUNT_CODES, "payment_error", STATUS_BUDGET]

# Published list prices for models whose catalog entry carries none (EUR).
FALLBACK_PRICES: Final[dict[str, dict[str, float]]] = {
    "kyutai-tts-1.6b-en-fr": {"audio_min": 0.01086},
    "kokoro-82m": {"audio_min": 0.00798},
    "clef-flash": {"in": 0.0217},
}


class BudgetReachedError(Exception):
    """The monthly cap set in the options is reached."""


def model_prices(model: BladeModel | None, model_id: str) -> dict[str, float]:
    """Return list prices in EUR: per million tokens, audio minute or image."""
    prices = dict(FALLBACK_PRICES.get(model_id, {}))
    if model is None:
        return prices
    batch = model.batch
    multiplier = float(batch.get("multiplier") or 0)
    if multiplier > 0:
        for source, target in (
            ("price_in", "in"),
            ("price_out", "out"),
            ("price_audio_min", "audio_min"),
            ("price_image", "image"),
        ):
            if (value := batch.get(source)) is not None:
                prices[target] = float(value) / multiplier
    return {key: value * model.spot_multiplier for key, value in prices.items()}


@dataclass(slots=True)
class Period:
    """Spend accumulated over one day or one month."""

    key: str
    cost: float = 0.0
    tokens: int = 0
    cached_tokens: int = 0
    audio_seconds: float = 0.0
    images: int = 0
    by_service: dict[str, float] = field(default_factory=dict)

    def add(self, service: str, cost: float, metrics: dict[str, float]) -> None:
        """Add one call."""
        self.cost += cost
        self.tokens += int(metrics.get("tokens_in", 0) + metrics.get("tokens_out", 0))
        self.cached_tokens += int(metrics.get("tokens_cached", 0))
        self.audio_seconds += metrics.get("audio_seconds", 0.0)
        self.images += int(metrics.get("images", 0))
        self.by_service[service] = self.by_service.get(service, 0.0) + cost

    def as_dict(self) -> dict[str, Any]:
        """Serialize for storage and attributes."""
        return {
            "key": self.key,
            "cost": round(self.cost, 6),
            "tokens": self.tokens,
            "cached_tokens": self.cached_tokens,
            "audio_seconds": round(self.audio_seconds, 1),
            "images": self.images,
            "by_service": {k: round(v, 6) for k, v in self.by_service.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, key: str) -> Period:
        """Restore a period, or start a new one when the key changed."""
        if not data or data.get("key") != key:
            return cls(key=key)
        return cls(
            key=key,
            cost=float(data.get("cost", 0.0)),
            tokens=int(data.get("tokens", 0)),
            cached_tokens=int(data.get("cached_tokens", 0)),
            audio_seconds=float(data.get("audio_seconds", 0.0)),
            images=int(data.get("images", 0)),
            by_service=dict(data.get("by_service") or {}),
        )


def _day_key() -> str:
    return dt_util.now().strftime("%Y-%m-%d")


def _month_key() -> str:
    return dt_util.now().strftime("%Y-%m")


class UsageTracker:
    """Count spend per entry, enforce the cap and surface account problems."""

    def __init__(self, hass: HomeAssistant, entry_id: str, budget: float) -> None:
        """Bind to the entry's storage."""
        self.hass = hass
        self.entry_id = entry_id
        self.budget = budget
        self.status = STATUS_OK
        self.day = Period(_day_key())
        self.month = Period(_month_key())
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.usage.{entry_id}"
        )
        self._listeners: list[Callable[[], None]] = []

    async def async_load(self) -> None:
        """Restore today's and this month's totals and the cap status."""
        data = await self._store.async_load() or {}
        self.day = Period.from_dict(data.get("day"), _day_key())
        self.month = Period.from_dict(data.get("month"), _month_key())
        # A previous run's issue may describe a cap or refusal that no longer applies.
        ir.async_delete_issue(self.hass, DOMAIN, f"account_{self.entry_id}")
        if self._over_budget():
            self._set_status(STATUS_BUDGET)

    async def async_remove(self) -> None:
        """Delete the stored totals."""
        await self._store.async_remove()

    def _over_budget(self) -> bool:
        return self.budget > 0 and self.month.cost >= self.budget

    @callback
    def async_roll(self) -> None:
        """Reset the totals at midnight even when no call happens."""
        day, month = self.day.key, self.month.key
        self._roll()
        if (day, month) == (self.day.key, self.month.key):
            return
        if self.status == STATUS_BUDGET and not self._over_budget():
            self._set_status(STATUS_OK)
        self._changed()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call back on every change; return the remover."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def _changed(self) -> None:
        self._store.async_delay_save(
            lambda: {"day": self.day.as_dict(), "month": self.month.as_dict()},
            SAVE_DELAY,
        )
        for listener in list(self._listeners):
            listener()

    @callback
    def _roll(self) -> None:
        """Start new periods at midnight and on the first of the month."""
        if self.day.key != (day := _day_key()):
            self.day = Period(day)
        if self.month.key != (month := _month_key()):
            self.month = Period(month)

    @callback
    def check_budget(self) -> None:
        """Refuse new calls once the monthly cap is reached."""
        self._roll()
        if self._over_budget():
            self._set_status(STATUS_BUDGET)
            raise BudgetReachedError

    @callback
    def record(
        self, service: str, model: BladeModel | None, model_id: str, **metrics: float
    ) -> float:
        """Add one call's usage and return its estimated cost."""
        self._roll()
        prices = model_prices(model, model_id)
        # Cached input is priced in full: Blade does not publish a cache rate.
        cost = (
            metrics.get("tokens_in", 0) * prices.get("in", 0) / 1e6
            + metrics.get("tokens_out", 0) * prices.get("out", 0) / 1e6
            + metrics.get("audio_seconds", 0) / 60 * prices.get("audio_min", 0)
            + metrics.get("images", 0) * prices.get("image", 0)
            + metrics.get("extra_cost", 0)
        )
        self.day.add(service, cost, metrics)
        self.month.add(service, cost, metrics)
        if self.status != STATUS_OK and not (
            self.status == STATUS_BUDGET and self._over_budget()
        ):
            self._set_status(STATUS_OK)
        self._changed()
        return cost

    @callback
    def account_error(self, code: str | None) -> None:
        """Remember a payment refusal from Blade."""
        self._set_status(code if code in ACCOUNT_CODES else "payment_error")
        self._changed()

    @callback
    def _set_status(self, status: str) -> None:
        if status == self.status:
            return
        self.status = status
        issue_id = f"account_{self.entry_id}"
        if status == STATUS_OK:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        else:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=status,
                translation_placeholders={
                    "budget": f"{self.budget:.2f}",
                    "console_url": "https://console.blade.sh",
                },
            )
        for listener in list(self._listeners):
            listener()
