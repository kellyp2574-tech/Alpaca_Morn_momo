"""Position sizing helpers and live exit management."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Optional

from .clock import market_now
from .config import Config
from .execution import ExecutionClient
from .state_manager import StateStore
from .storage import PositionState

logger = logging.getLogger(__name__)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def initial_stop_pct(cfg: Config, atr: float, entry: float) -> float:
    if entry <= 0 or atr <= 0:
        return cfg.stop_max_pct
    raw = cfg.stop_atr_mult * (atr / entry)
    return clamp(raw, cfg.stop_min_pct, cfg.stop_max_pct)


def calc_qty(
    account_equity: float, risk_pct: float, entry: float, stop_pct: float
) -> float:
    risk_dollars = account_equity * risk_pct
    stop_dollars = entry * stop_pct
    if stop_dollars <= 0:
        return 0.0
    shares = risk_dollars / stop_dollars
    return max(0.0, shares)


class PositionManager:
    def __init__(
        self,
        cfg: Config,
        execution: ExecutionClient,
        risk_manager,
        *,
        state_store: Optional[StateStore] = None,
    ) -> None:
        self.cfg = cfg
        self.execution = execution
        self.risk_manager = risk_manager
        self.positions: Dict[str, PositionState] = {}
        self.state_store = state_store

    def load_states(self, states: Dict[str, PositionState]) -> None:
        self.positions = dict(states)
        self._persist()

    @property
    def open_count(self) -> int:
        return len(self.positions)

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def open_position(
        self,
        symbol: str,
        qty: int,
        entry_price: float,
        stop_pct: float,
        *,
        entry_time: Optional[datetime] = None,
    ) -> PositionState:
        entry_time = entry_time or market_now()
        stop_price = entry_price * (1 - stop_pct)
        state = PositionState(
            symbol=symbol,
            entry_time=entry_time,
            entry_price=entry_price,
            qty=qty,
            stop_price=stop_price,
            peak_price=entry_price,
            r_stop_pct=stop_pct,
        )
        self.positions[symbol] = state
        logger.info(
            "Opened position %s qty=%s entry=%.2f stop=%.2f (%.2f%%)",
            symbol,
            qty,
            entry_price,
            stop_price,
            stop_pct * 100,
        )
        self.risk_manager.on_new_trade()
        self._persist()
        return state

    def on_bar(self, symbol: str, bar, *, now: Optional[datetime] = None) -> None:
        state = self.positions.get(symbol)
        if not state:
            return

        now = now or market_now()
        state.peak_price = max(state.peak_price, bar.h)
        entry = state.entry_price
        price = bar.c

        # Breakeven move
        if not state.breakeven_set and state.peak_price >= entry * (
            1 + self.cfg.breakeven_at_pct
        ):
            state.stop_price = max(state.stop_price, entry)
            state.breakeven_set = True
            logger.info("%s stop moved to breakeven", symbol)

        # Trail activation and widening
        if not state.trail_active and state.peak_price >= entry * (
            1 + self.cfg.trail_activate_at_pct
        ):
            state.trail_active = True
            state.trail_pct = self.cfg.trail_pct_1
            logger.info("%s trail activated at %.2f%%", symbol, state.trail_pct * 100)

        if state.trail_active:
            trail_pct = state.trail_pct or self.cfg.trail_pct_1
            if state.peak_price >= entry * (1 + self.cfg.trail_widen_at_pct):
                trail_pct = self.cfg.trail_pct_2
                state.trail_pct = trail_pct
            trail_stop = state.peak_price * (1 - trail_pct)
            state.stop_price = max(state.stop_price, trail_stop)

        # Dead momentum check
        elapsed = (now - state.entry_time).total_seconds() / 60.0
        gain_pct = (price - entry) / entry if entry > 0 else 0.0
        if (
            elapsed >= self.cfg.dead_momo_minutes
            and gain_pct < self.cfg.dead_momo_min_gain
        ):
            logger.info(
                "%s dead momentum exit (elapsed %.1f min, gain %.2f%%)",
                symbol,
                elapsed,
                gain_pct * 100,
            )
            self._exit(symbol, state, price, now, reason="dead_momo")
            return

        # Stop hit (synthetic)
        if bar.l <= state.stop_price:
            exit_price = max(state.stop_price, bar.l)
            self._exit(symbol, state, exit_price, now, reason="stop")
            return

        self._persist()

    def force_exit_all(
        self, price_lookup: Dict[str, float], *, reason: str = "hard_exit"
    ) -> None:
        for symbol, state in list(self.positions.items()):
            price = price_lookup.get(symbol, state.peak_price)
            self._exit(symbol, state, price, market_now(), reason=reason)
        self._persist()

    # ------------------------------------------------------------------

    def _exit(
        self,
        symbol: str,
        state: PositionState,
        price: float,
        now: datetime,
        *,
        reason: str,
    ) -> None:
        qty = int(round(state.qty))
        order_id = self.execution.place_exit(symbol, qty, price)
        state.exit_time = now
        state.exit_price = price
        if state.r_stop_pct > 0:
            state.realized_r = (price - state.entry_price) / (
                state.entry_price * state.r_stop_pct
            )
        else:
            state.realized_r = 0.0
        self.risk_manager.on_trade_closed(state.realized_r or 0.0)
        logger.info(
            "EXIT %s qty=%s @ %.2f reason=%s R=%.2f order=%s",
            symbol,
            qty,
            price,
            reason,
            state.realized_r or 0.0,
            order_id,
        )
        self.positions.pop(symbol, None)
        self._persist()

    def _persist(self) -> None:
        if not self.state_store:
            return
        self.state_store.save_positions(self.positions)
