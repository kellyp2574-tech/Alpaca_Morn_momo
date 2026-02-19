"""Position sizing helpers and live exit management."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from .main import SessionStats

from .clock import market_now
from .config import Config
from .execution import ExecutionClient, FillResult
from .state_manager import StateStore
from .storage import PositionState

_SESSION_DATE: Optional[str] = None


def _session_date() -> str:
    """Return today's date string once per process for stable client_order_id generation."""
    global _SESSION_DATE
    if _SESSION_DATE is None:
        _SESSION_DATE = market_now().strftime("%Y%m%d")
    return _SESSION_DATE


_CLIENT_ID_MAX_LEN: int = 48  # Alpaca client_order_id maximum length


def _norm_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def _entry_client_id(symbol: str, attempt: int = 1) -> str:
    raw = f"ENTRY:{_norm_symbol(symbol)}:{_session_date()}:{attempt}"
    if len(raw) > _CLIENT_ID_MAX_LEN:
        raise ValueError(f"client_order_id too long ({len(raw)}): {raw!r}")
    return raw


def _exit_client_id(symbol: str, attempt: int) -> str:
    raw = f"EXIT:{_norm_symbol(symbol)}:{_session_date()}:{attempt}"
    if len(raw) > _CLIENT_ID_MAX_LEN:
        raise ValueError(f"client_order_id too long ({len(raw)}): {raw!r}")
    return raw

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
        self.stats: Optional[SessionStats] = None

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
        entry_order_id: Optional[str] = None,
        entry_client_order_id: Optional[str] = None,
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
            entry_order_id=entry_order_id,
            entry_client_order_id=entry_client_order_id,
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

        # If an exit is in flight, run reconcile and skip new exit intents,
        # but still update defensive state (trail, peak) so it stays current.
        if state.exit_pending:
            self._reconcile_pending_exit(symbol, state, now)
            state.peak_price = max(state.peak_price, bar.h)
            self._update_trail(state)
            self._persist()
            return

        state.peak_price = max(state.peak_price, bar.h)
        entry = state.entry_price
        price = bar.c

        self._update_breakeven(symbol, state)
        self._update_trail(state)

        # Dead momentum check
        elapsed = (now - state.entry_time).total_seconds() / 60.0
        gain_pct = (price - entry) / entry if entry > 0 else 0.0
        if (
            elapsed >= self.cfg.dead_momo_minutes
            and gain_pct < self.cfg.dead_momo_min_gain
        ):
            logger.info(
                "%s dead momentum exit (elapsed %.1f min, gain %.2f%%)",
                symbol, elapsed, gain_pct * 100,
            )
            self._exit(symbol, state, price, now, reason="dead_momo")
            return

        # Stop hit (synthetic): trigger on bar.low, fill modeled at stop_price
        if bar.l <= state.stop_price:
            self._exit(symbol, state, state.stop_price, now, reason="stop")
            return

        self._persist()

    def _update_breakeven(self, symbol: str, state: PositionState) -> None:
        if not state.breakeven_set and state.peak_price >= state.entry_price * (
            1 + self.cfg.breakeven_at_pct
        ):
            state.stop_price = max(state.stop_price, state.entry_price)
            state.breakeven_set = True
            logger.info("%s stop moved to breakeven", symbol)

    def _update_trail(self, state: PositionState) -> None:
        entry = state.entry_price
        if not state.trail_active and state.peak_price >= entry * (
            1 + self.cfg.trail_activate_at_pct
        ):
            state.trail_active = True
            state.trail_pct = self.cfg.trail_pct_1
            logger.info("%s trail activated at %.2f%%", state.symbol, state.trail_pct * 100)

        if state.trail_active:
            trail_pct = state.trail_pct or self.cfg.trail_pct_1
            if state.peak_price >= entry * (1 + self.cfg.trail_widen_at_pct):
                trail_pct = self.cfg.trail_pct_2
                state.trail_pct = trail_pct
            trail_stop = state.peak_price * (1 - trail_pct)
            state.stop_price = max(state.stop_price, trail_stop)

    def exit_position(
        self, symbol: str, price: float, now: datetime, *, reason: str
    ) -> None:
        """Public interface to exit a single named position."""
        state = self.positions.get(symbol)
        if state:
            self._exit(symbol, state, price, now, reason=reason)

    def force_exit_all(
        self, price_lookup: Dict[str, float], *, reason: str = "hard_exit"
    ) -> None:
        for symbol, state in list(self.positions.items()):
            price = price_lookup.get(symbol, state.peak_price)
            self._exit(symbol, state, price, market_now(), reason=reason)

    # ------------------------------------------------------------------

    def _reconcile_pending_exit(
        self, symbol: str, state: PositionState, now: datetime
    ) -> None:
        """Slow-path reconcile: called each bar while exit_pending.
        Waits exit_ack_timeout_seconds before querying broker.
        Falls back to client_order_id search if order_id was lost.
        """
        if not state.exit_submitted_ts:
            # Shouldn't happen, but clear after timeout to avoid permanent lock
            logger.warning("%s exit_pending with no submitted_ts; clearing", symbol)
            state.exit_pending = False
            return

        age = time.time() - state.exit_submitted_ts
        if age < self.cfg.exit_ack_timeout_seconds:
            return  # still within grace window, wait

        fallback = state.exit_price or state.peak_price

        # Prefer order_id lookup; fall back to client_order_id search
        fill: Optional[FillResult] = None
        if state.exit_order_id:
            logger.info(
                "Reconciling exit for %s via order_id %s (age %.0fs)",
                symbol, state.exit_order_id, age,
            )
            try:
                fill = self.execution.poll_order_fill(
                    state.exit_order_id, fallback_price=fallback
                )
            except Exception:
                logger.exception("Reconcile poll failed for %s", symbol)
                return
        elif state.exit_client_order_id:
            logger.info(
                "Reconciling exit for %s via client_order_id %s (order_id lost)",
                symbol, state.exit_client_order_id,
            )
            fill = self.execution.find_order_by_client_id(state.exit_client_order_id)

        if fill is None:
            logger.warning("%s reconcile found no order; clearing pending", symbol)
            state.exit_pending = False
            state.exit_order_id = None
            state.exit_client_order_id = None
            state.exit_submitted_ts = None
            return

        self._apply_fill_result(symbol, state, fill, now)

    def _apply_fill_result(
        self, symbol: str, state: PositionState, fill: FillResult, now: datetime
    ) -> None:
        """Dispatch a FillResult to the appropriate handler. Used by both _exit and reconcile."""
        if fill.status in {"filled", "dry_run"}:
            self._record_fill(symbol, state, fill, now)
        elif fill.status == "partial":
            logger.warning(
                "%s partial fill on exit: %.0f/%.0f shares @ %.2f",
                symbol, fill.filled_qty, state.qty, fill.avg_price,
            )
            if self.stats is not None:
                latency = time.time() - state.exit_submitted_ts if state.exit_submitted_ts else 0.0
                self.stats.record_exit(
                    status="partial",
                    latency=latency,
                    decision_price=state.exit_price or fill.avg_price,
                    fill_price=fill.avg_price,
                )
            state.qty -= fill.filled_qty
            state.exit_pending = False
            state.exit_order_id = None
            state.exit_client_order_id = None
            state.exit_submitted_ts = None
            self._persist()
        elif fill.status == "unfilled":
            logger.warning(
                "Exit order for %s was unfilled; position remains open", symbol
            )
            if self.stats is not None:
                self.stats.record_exit(
                    status="unfilled", latency=0.0,
                    decision_price=0.0, fill_price=0.0,
                )
            state.exit_pending = False
            state.exit_order_id = None
            state.exit_client_order_id = None
            state.exit_submitted_ts = None
            self._persist()
        elif fill.status == "unknown":
            logger.warning("%s exit order still unknown after reconcile", symbol)
            if self.stats is not None:
                self.stats.record_exit(
                    status="unknown", latency=0.0,
                    decision_price=0.0, fill_price=0.0,
                )
            # Leave pending; will retry next bar

    def _record_fill(
        self, symbol: str, state: PositionState, fill: FillResult, now: datetime
    ) -> None:
        """Finalize a confirmed fill: compute R, notify risk manager, remove position."""
        decision_price = state.exit_price or state.peak_price  # provisional price set at _exit time
        price = fill.avg_price if fill.avg_price > 0 else decision_price
        if state.r_stop_pct > 0:
            state.realized_r = (price - state.entry_price) / (
                state.entry_price * state.r_stop_pct
            )
        else:
            state.realized_r = 0.0
        state.exit_time = now
        state.exit_price = price
        self.risk_manager.on_trade_closed(state.realized_r or 0.0)
        logger.info(
            "EXIT %s qty=%.0f @ %.2f reason=%s R=%.2f order=%s",
            symbol, fill.filled_qty, price, state.exit_reason,
            state.realized_r or 0.0, fill.order_id,
        )
        if self.stats is not None:
            latency = (
                time.time() - state.exit_submitted_ts
                if state.exit_submitted_ts else 0.0
            )
            self.stats.record_exit(
                status=fill.status,
                latency=latency,
                decision_price=decision_price,
                fill_price=price,
            )
        self.positions.pop(symbol, None)
        self._persist()

    def _exit(
        self,
        symbol: str,
        state: PositionState,
        price: float,
        now: datetime,
        *,
        reason: str,
    ) -> None:
        if state.exit_pending:
            logger.debug("Exit already in flight for %s, skipping duplicate", symbol)
            return

        state.exit_attempts += 1
        client_id = _exit_client_id(symbol, state.exit_attempts)
        state.exit_pending = True
        state.exit_submitted_ts = time.time()
        state.exit_reason = reason
        state.exit_client_order_id = client_id
        state.exit_price = price  # provisional; overwritten by actual fill
        self._persist()

        qty = int(round(state.qty))
        fill = self.execution.place_exit(symbol, qty, price, client_order_id=client_id)
        state.exit_order_id = fill.order_id
        self._persist()

        self._apply_fill_result(symbol, state, fill, now)

    def _persist(self) -> None:
        if not self.state_store:
            return
        self.state_store.save_positions(self.positions)
