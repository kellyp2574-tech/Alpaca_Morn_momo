"""Risk guardrails for entries and daily exposure."""

from __future__ import annotations

from .config import Config


class RiskManager:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.trades_taken = 0
        self.realized_r_total = 0.0

    def can_enter(self, open_positions: int) -> bool:
        if self.realized_r_total <= self.cfg.daily_kill_r:
            return False
        if self.trades_taken >= self.cfg.max_trades_per_day:
            return False
        if open_positions >= self.cfg.max_concurrent:
            return False
        return True

    def on_new_trade(self) -> None:
        self.trades_taken += 1

    def on_trade_closed(self, realized_r: float) -> None:
        self.realized_r_total += realized_r
