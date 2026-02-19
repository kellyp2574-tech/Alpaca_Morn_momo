"""Risk guardrails for entries and daily exposure."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from .config import Config


class RiskManager:
    def __init__(self, cfg: Config, *, state_store: Optional["StateStore"] = None) -> None:
        self.cfg = cfg
        self.state_store = state_store
        self.trades_taken = 0
        self.realized_r_total = 0.0
        self.day: Optional[date] = None

    def load_state(self, payload: Dict[str, Any]) -> None:
        if not payload:
            return
        self.trades_taken = int(payload.get("trades_taken", 0))
        self.realized_r_total = float(payload.get("realized_r_total", 0.0))
        day_str = payload.get("day")
        self.day = date.fromisoformat(day_str) if day_str else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trades_taken": self.trades_taken,
            "realized_r_total": self.realized_r_total,
            "day": self.day.isoformat() if self.day else None,
        }

    def maybe_reset(self, today: date) -> None:
        if self.day is None or today != self.day:
            self.day = today
            self.trades_taken = 0
            self.realized_r_total = 0.0
            self._persist()

    def can_enter(self, open_positions: int) -> Tuple[bool, str]:
        if self.realized_r_total <= self.cfg.daily_kill_r:
            return False, "daily_kill"
        if self.trades_taken >= self.cfg.max_trades_per_day:
            return False, "max_trades"
        if open_positions >= self.cfg.max_concurrent:
            return False, "max_concurrent"
        return True, "ok"

    def on_new_trade(self) -> None:
        self.trades_taken += 1
        self._persist()

    def on_trade_closed(self, realized_r: float) -> None:
        self.realized_r_total += realized_r
        self._persist()

    def _persist(self) -> None:
        if self.state_store:
            self.state_store.save_risk_state(self.to_dict())


if TYPE_CHECKING:  # pragma: no cover
    from .state_manager import StateStore
