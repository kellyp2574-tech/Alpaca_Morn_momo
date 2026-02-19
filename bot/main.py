"""Entry loop orchestration for the morning momentum bot."""

from __future__ import annotations

import argparse
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional, Tuple

from .clock import MARKET_TZ, config_window, market_datetime, market_now
from .config import Config
from .data_alpaca import Quote
from .data_sources import DataStack, init_data_stack
from .execution import ExecutionClient, ExecutionConfig, FillResult
from .indicators import VWAPState, atr_1m
from .position_manager import PositionManager, calc_qty, initial_stop_pct, _entry_client_id
from .premarket_scan import build_candidates
from .risk_manager import RiskManager
from .state_manager import StateStore
from .storage import Candidate, PendingEntryState

logger = logging.getLogger(__name__)


@dataclass
class SessionStats:
    """Accumulates per-order observations for end-of-session rollup."""

    # Entry outcomes
    entry_filled: int = 0
    entry_partial: int = 0
    entry_unfilled: int = 0
    entry_unknown: int = 0

    # Exit outcomes
    exit_filled: int = 0
    exit_partial: int = 0
    exit_unfilled: int = 0
    exit_unknown: int = 0

    # Latency split by outcome type (seconds from submit to status confirmation):
    #   filled  = time to complete terminal fill
    #   partial = time to first/partial fill (IOC canceled-with-fill)
    entry_latencies_filled: List[float] = field(default_factory=list)
    entry_latencies_partial: List[float] = field(default_factory=list)
    exit_latencies_filled: List[float] = field(default_factory=list)
    exit_latencies_partial: List[float] = field(default_factory=list)

    # Slippage: fractional (fill - decision) / decision for entries,
    #           (decision - fill) / decision for exits (positive = worse)
    entry_slippages: List[float] = field(default_factory=list)
    exit_slippages: List[float] = field(default_factory=list)

    def record_entry(
        self,
        status: str,
        latency: float,
        decision_price: float,
        fill_price: float,
    ) -> None:
        if status == "filled":
            self.entry_filled += 1
            if latency > 0:
                self.entry_latencies_filled.append(latency)
        elif status == "partial":
            self.entry_partial += 1
            if latency > 0:
                self.entry_latencies_partial.append(latency)
        elif status == "unfilled":
            self.entry_unfilled += 1
        else:
            self.entry_unknown += 1
        if status in {"filled", "partial"} and decision_price > 0 and fill_price > 0:
            self.entry_slippages.append((fill_price - decision_price) / decision_price)

    def record_exit(
        self,
        status: str,
        latency: float,
        decision_price: float,
        fill_price: float,
    ) -> None:
        if status in {"filled", "dry_run"}:
            self.exit_filled += 1
            if latency > 0:
                self.exit_latencies_filled.append(latency)
        elif status == "partial":
            self.exit_partial += 1
            if latency > 0:
                self.exit_latencies_partial.append(latency)
        elif status == "unfilled":
            self.exit_unfilled += 1
        else:
            self.exit_unknown += 1
        if status in {"filled", "partial", "dry_run"} and decision_price > 0 and fill_price > 0:
            self.exit_slippages.append((decision_price - fill_price) / decision_price)

    def rollup(self) -> None:
        def _stat(xs: List[float], unit: str = "") -> str:
            """Return 'mean/median(n=N)unit' or 'n/a'."""
            if not xs:
                return "n/a"
            s = sorted(xs)
            mid = len(s) // 2
            median = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
            mean = sum(xs) / len(xs)
            return f"{mean:.3f}/{median:.3f}(n={len(xs)}){unit}"

        entry_total = self.entry_filled + self.entry_partial + self.entry_unfilled + self.entry_unknown
        exit_total = self.exit_filled + self.exit_partial + self.exit_unfilled + self.exit_unknown
        logger.info(
            "SESSION_STATS "
            "entries=%d(filled=%d partial=%d unfilled=%d unknown=%d) "
            "exits=%d(filled=%d partial=%d unfilled=%d unknown=%d) "
            "entry_lat_filled=%s entry_lat_partial=%s "
            "exit_lat_filled=%s exit_lat_partial=%s "
            "entry_slip=%s exit_slip=%s",
            entry_total,
            self.entry_filled, self.entry_partial, self.entry_unfilled, self.entry_unknown,
            exit_total,
            self.exit_filled, self.exit_partial, self.exit_unfilled, self.exit_unknown,
            _stat(self.entry_latencies_filled, "s"),
            _stat(self.entry_latencies_partial, "s"),
            _stat(self.exit_latencies_filled, "s"),
            _stat(self.exit_latencies_partial, "s"),
            _stat(self.entry_slippages),
            _stat(self.exit_slippages),
        )


@dataclass
class EntryContext:
    cfg: Config
    data: DataStack
    watchlist: List[Candidate]
    max_bar_history: int
    candidate_map: Dict[str, Candidate]
    risk_manager: RiskManager
    account_equity: float
    execution: ExecutionClient
    positions: PositionManager
    state_store: StateStore
    subscribe_symbols: List[str]
    max_notional: float

    def watch_symbols(self) -> List[str]:
        return self.subscribe_symbols


@dataclass
class EntryDecision:
    price: float
    qty: float
    stop_pct: float
    atr: float


class EntryLoop:
    """Consumes live 1m bars and emits entry signals."""

    def __init__(self, ctx: EntryContext) -> None:
        self.ctx = ctx
        self.alpaca = ctx.data.alpaca
        self.bar_history: Dict[str, Deque] = defaultdict(
            lambda: deque(maxlen=ctx.max_bar_history)
        )
        self.vwap_state: Dict[str, VWAPState] = defaultdict(VWAPState)
        self.risk_manager = ctx.risk_manager
        self.positions = ctx.positions
        self.last_prices: Dict[str, float] = {}
        self.market_open_dt = market_datetime(None, ctx.cfg.market_open)
        self.latest_quotes: Dict[str, Quote] = {}
        self.last_quote_refresh = datetime.fromtimestamp(0, tz=MARKET_TZ)
        self.stats = SessionStats()
        self.positions.stats = self.stats

    def run(self) -> None:
        cfg = self.ctx.cfg
        entry_window = config_window(cfg, "entry_start", "entry_cutoff")
        hard_exit_window = config_window(cfg, "entry_start", "hard_exit")
        # Second reconcile at 9:31:30 ET (90s after open) catches fills that
        # landed during the broker's slow-open window before the first bar.
        second_reconcile_dt = market_datetime(None, cfg.market_open) + timedelta(seconds=90)
        second_reconcile_done = False
        watch_symbols = self.ctx.watch_symbols()

        if not watch_symbols:
            logger.warning("No symbols qualified for the live session. Nothing to do.")
            return

        logger.info(
            "Subscribing to %d symbols: %s",
            len(watch_symbols),
            ", ".join(watch_symbols),
        )
        self.alpaca.subscribe_stream(watch_symbols)

        while True:
            now = market_now()

            if not second_reconcile_done and now >= second_reconcile_dt:
                logger.info("Running scheduled post-open reconcile at %s", now.strftime("%H:%M:%S"))
                _reconcile_pending_entries(
                    self.ctx.state_store, self.ctx.execution,
                    self.positions, cfg,
                )
                second_reconcile_done = True
            self.risk_manager.maybe_reset(now.date())
            self._maybe_refresh_quotes(now, watch_symbols)
            self._check_spread_exits(now)
            if now >= hard_exit_window.end:
                logger.info(
                    "Hard exit reached (%s). Stopping entry loop.", hard_exit_window.end
                )
                self._hard_exit()
                break

            bar = self.alpaca.next_bar(timeout=5)
            if bar is None:
                continue
            if bar.symbol not in watch_symbols:
                continue

            bar_ts = self._normalize_timestamp(bar.timestamp)
            bar.timestamp = bar_ts

            if (now - bar_ts).total_seconds() > self.ctx.cfg.max_bar_delay_seconds:
                logger.warning(
                    "Skipping stale bar for %s (delay %.1fs)",
                    bar.symbol,
                    (now - bar_ts).total_seconds(),
                )
                continue

            history = self.bar_history[bar.symbol]
            history.append(bar)
            if bar_ts >= self.market_open_dt:
                self.vwap_state[bar.symbol].update(bar)
            self.last_prices[bar.symbol] = bar.c

            self.positions.on_bar(bar.symbol, bar, now=now)

            if not entry_window.contains(now):
                continue

            if self.positions.has_position(bar.symbol):
                continue

            decision = self._evaluate_entry(bar.symbol, history)
            if not decision:
                continue
            allowed, reason = self.risk_manager.can_enter(self.positions.open_count)
            if not allowed:
                logger.info("Risk guardrail %s prevented entry for %s", reason, bar.symbol)
                continue
            self._place_entry(bar.symbol, decision, now)

        self.stats.rollup()

    # ------------------------------------------------------------------
    def _evaluate_entry(self, symbol: str, bars: Deque) -> Optional[EntryDecision]:
        cfg = self.ctx.cfg
        candidate = self.ctx.candidate_map.get(symbol)
        if candidate is None:
            return None

        bars_seq = list(bars)
        rth_bars = [b for b in bars_seq if b.timestamp >= self.market_open_dt]
        min_len = max(cfg.atr_len + 1, cfg.volume_avg_window + 1)
        if len(rth_bars) < min_len:
            return None

        atr = atr_1m(rth_bars, cfg.atr_len)
        if atr <= 0:
            return None
        if atr < cfg.min_atr_dollars:
            return None

        last_bar = rth_bars[-1]
        vwap = self.vwap_state[symbol].vwap
        if vwap <= 0:
            return None
        if last_bar.c < vwap:
            return None
        if last_bar.c <= candidate.pm_high:
            return None
        if last_bar.c > candidate.pm_high * (1 + cfg.max_breakout_extension_pct):
            return None

        vol_window = cfg.volume_avg_window
        if vol_window > 0:
            recent = rth_bars[-(vol_window + 1) : -1]
            prev_vols = [bar.v for bar in recent]
            if len(prev_vols) < vol_window:
                return None
            avg_vol = sum(prev_vols) / vol_window if vol_window else 0.0
            if avg_vol <= 0:
                return None
            if last_bar.v < cfg.volume_spike_mult * avg_vol:
                return None
        if last_bar.v < cfg.min_1m_volume:
            return None
        if last_bar.v * last_bar.c < cfg.min_1m_dollar_volume:
            return None

        quote = self.latest_quotes.get(symbol)
        if not quote:
            return None
        if quote.ask_price <= 0 or quote.bid_price <= 0:
            return None
        spread = quote.ask_price - quote.bid_price
        if spread <= 0:
            return None
        max_spread = max(cfg.max_spread_dollars, cfg.max_spread_pct * quote.ask_price)
        if spread > max_spread:
            return None

        entry_price = quote.ask_price

        stop_pct = initial_stop_pct(cfg, atr, entry_price)
        qty = calc_qty(
            self.ctx.account_equity, cfg.risk_per_trade, entry_price, stop_pct
        )
        if qty < 1:
            return None

        # Check if fractional trading is allowed for this symbol
        fractionable = self.ctx.execution.is_fractionable(symbol)

        notional = qty * entry_price
        max_notional = self.ctx.max_notional
        if max_notional > 0 and notional > max_notional:
            # Apply max notional cap, then check fractionability
            qty = max_notional / entry_price
            if not fractionable:
                qty = int(qty)  # floor to whole shares
        else:
            if not fractionable:
                qty = int(qty)  # floor to whole shares
            # else: keep fractional qty (floor applied via calc_qty already)

        if qty < 1:
            return None

        return EntryDecision(price=entry_price, qty=qty, stop_pct=stop_pct, atr=atr)

    def _place_entry(self, symbol: str, decision: EntryDecision, now: datetime) -> None:
        if self.positions.has_position(symbol):
            return

        # Load existing pending record to get current attempt count
        existing_pending = self.ctx.state_store.load_pending_entries()
        prev = existing_pending.get(symbol)
        attempt = (prev.attempts + 1) if prev else 1
        client_id = _entry_client_id(symbol, attempt)

        logger.info(
            "ENTER %s qty=%s @ %.2f stop=%.2f%% ATR=%.3f client_id=%s",
            symbol, decision.qty, decision.price, decision.stop_pct * 100, decision.atr, client_id,
        )

        # Persist BEFORE submit so a crash after submit is always recoverable
        submitted_ts = time.time()
        pending_record = PendingEntryState(
            symbol=symbol,
            client_order_id=client_id,
            submitted_ts=submitted_ts,
            attempts=attempt,
            stop_pct=decision.stop_pct,
            intended_qty=decision.qty,
            intended_price=decision.price,
        )
        existing_pending[symbol] = pending_record
        self.ctx.state_store.save_pending_entries(existing_pending)

        fill = self.ctx.execution.place_entry(symbol, decision.qty, decision.price, client_order_id=client_id)

        age = time.time() - submitted_ts

        if fill.status == "unfilled":
            logger.warning(
                "ENTRY_RESULT symbol=%s client_id=%s broker_status=unfilled "
                "filled_qty=0 avg_price=0 action=clear age=%.1fs",
                symbol, client_id, age,
            )
            self.stats.record_entry(status="unfilled", latency=age, decision_price=0.0, fill_price=0.0)
            self.ctx.state_store.clear_pending_entry(symbol)
            return

        if fill.status == "unknown":
            logger.warning(
                "ENTRY_RESULT symbol=%s client_id=%s order_id=%s broker_status=unknown "
                "action=keep_pending age=%.1fs",
                symbol, client_id, fill.order_id, age,
            )
            self.stats.record_entry(status="unknown", latency=age, decision_price=0.0, fill_price=0.0)
            # Record is already persisted; nothing more to do until reconcile
            return

        # filled, partial, or dry_run: open position with actual broker fill data
        # Always prefer broker-reported qty/price; fall back to intended only if missing
        filled_qty = int(round(fill.filled_qty)) if fill.filled_qty > 0 else decision.qty
        exec_price = fill.avg_price if fill.avg_price > 0 else round(
            decision.price * (1 + self.ctx.cfg.entry_slip_pct), 2
        )

        logger.info(
            "ENTRY_RESULT symbol=%s client_id=%s order_id=%s broker_status=%s "
            "filled_qty=%.0f avg_price=%.2f action=open_position age=%.1fs",
            symbol, client_id, fill.order_id, fill.status,
            filled_qty, exec_price, age,
        )
        self.stats.record_entry(
            status=fill.status, latency=age,
            decision_price=decision.price, fill_price=exec_price,
        )
        self.ctx.state_store.clear_pending_entry(symbol)
        # Stop is computed from actual fill price, not intended price
        self.positions.open_position(
            symbol,
            filled_qty,
            exec_price,
            decision.stop_pct,
            entry_time=now,
            entry_order_id=fill.order_id,
            entry_client_order_id=client_id,
        )

    def _hard_exit(self) -> None:
        if not self.positions.open_count:
            return
        logger.info("Force exiting all open positions")
        self.positions.force_exit_all(self.last_prices, reason="hard_exit")

    def _normalize_timestamp(self, ts: datetime) -> datetime:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(MARKET_TZ)

    def _check_spread_exits(self, now: datetime) -> None:
        """Exit any open position whose bid/ask spread has expanded beyond the exit threshold."""
        cfg = self.ctx.cfg

        # Stale quote guard: don't act on quotes that are too old
        if (now - self.last_quote_refresh).total_seconds() > cfg.quote_refresh_seconds * 2:
            return

        for symbol, state in list(self.positions.positions.items()):
            if state.exit_pending:
                continue

            # Grace period: ignore spread right after entry
            age = (now - state.entry_time).total_seconds()
            if age < cfg.spread_exit_grace_seconds:
                continue

            quote = self.latest_quotes.get(symbol)
            if not quote or quote.ask_price <= 0 or quote.bid_price <= 0:
                continue

            spread = quote.ask_price - quote.bid_price
            max_spread = max(cfg.exit_spread_dollars, cfg.exit_spread_pct * quote.ask_price)

            if spread > max_spread:
                state.spread_bad_count += 1
            else:
                state.spread_bad_count = 0

            if state.spread_bad_count < cfg.spread_exit_consecutive:
                continue

            logger.warning(
                "Spread expansion exit %s bid=%.2f ask=%.2f spread=%.4f thresh=%.4f",
                symbol, quote.bid_price, quote.ask_price, spread, max_spread,
            )
            self.positions.exit_position(symbol, quote.bid_price, now, reason="spread_expansion")

    def _maybe_refresh_quotes(self, now: datetime, symbols: List[str]) -> None:
        refresh_interval = self.ctx.cfg.quote_refresh_seconds
        if refresh_interval <= 0:
            return
        if (now - self.last_quote_refresh).total_seconds() < refresh_interval:
            return
        try:
            quotes = self.alpaca.get_latest_quotes(symbols)
        except Exception:
            logger.exception("Failed to refresh quotes")
            return
        if quotes:
            self.latest_quotes.update(quotes)
            self.last_quote_refresh = now


# ----------------------------------------------------------------------


_PENDING_ENTRY_NOT_FOUND_GRACE_S: float = 60.0    # keep if broker lookup returns None within window
_PENDING_ENTRY_LIVE_ORDER_CANCEL_AFTER_S: float = 300.0  # cancel still-open entry after 5 min
_PENDING_ENTRY_CANCEL_BACKOFF_S: float = 120.0   # minimum seconds between cancel attempts
_PENDING_ENTRY_CANCEL_MAX_ATTEMPTS: int = 3       # escalate to ERROR after this many attempts


def _should_cancel_stale_entry(
    p: PendingEntryState,
    age: float,
    wall: float,
    entry_window_open: bool,
) -> Tuple[bool, str]:
    """Return (should_cancel: bool, reason: str).

    Cancel only when:
      - age >= threshold, AND
      - cancel backoff has elapsed (or never attempted), AND
      - cancel_attempts < max (beyond max: log ERROR, keep, don't spam)
    The reason string distinguishes stale-in-window vs outside-entry-window.
    """
    if age < _PENDING_ENTRY_LIVE_ORDER_CANCEL_AFTER_S:
        return False, ""

    if p.cancel_attempts >= _PENDING_ENTRY_CANCEL_MAX_ATTEMPTS:
        return False, "max_cancel_attempts_exceeded"

    since_last = wall - p.cancel_requested_ts if p.cancel_requested_ts > 0 else float("inf")
    if since_last < _PENDING_ENTRY_CANCEL_BACKOFF_S:
        return False, "cancel_backoff"

    reason = "outside_entry_window" if not entry_window_open else "stale_live_order"
    return True, reason


def _reconcile_pending_entries(
    state_store: StateStore,
    execution: ExecutionClient,
    positions: PositionManager,
    cfg: Config,
) -> None:
    """On startup, resolve any entries that were in-flight when the process last crashed."""
    pending = state_store.load_pending_entries()
    if not pending:
        return

    count = len(pending)
    logger.info("RECONCILE_START pending_entries=%d", count)

    now = market_now()
    wall = time.time()
    entry_window = config_window(cfg, "entry_start", "entry_cutoff")
    entry_window_open = now < entry_window.end
    updated: Dict[str, PendingEntryState] = {}

    # Metrics counters (item 5)
    m_adopted = m_unfilled = m_cleared_stale = m_kept = m_cancel_sent = 0

    for symbol, p in pending.items():
        age = wall - p.submitted_ts

        # Already adopted by a prior run or position load
        if positions.has_position(symbol):
            logger.info(
                "RECONCILE symbol=%s client_id=%s age=%.0fs action=clear "
                "reason=position_already_open",
                symbol, p.client_order_id, age,
            )
            continue

        fill = execution.find_order_by_client_id(p.client_order_id)

        # ── Broker lookup returned None: transient API error ─────────
        # (Definitive 404 now returns FillResult(status='unfilled') instead)
        if fill is None:
            if age < _PENDING_ENTRY_NOT_FOUND_GRACE_S:
                logger.warning(
                    "RECONCILE symbol=%s client_id=%s age=%.0fs action=keep "
                    "reason=transient_broker_error_within_grace",
                    symbol, p.client_order_id, age,
                )
                updated[symbol] = p
                m_kept += 1
            else:
                logger.warning(
                    "RECONCILE symbol=%s client_id=%s age=%.0fs action=clear "
                    "reason=transient_broker_error_beyond_grace",
                    symbol, p.client_order_id, age,
                )
                m_cleared_stale += 1
            continue

        # ── Terminal: filled or partial — verify broker position then adopt ──
        if fill.status in {"filled", "partial"}:
            filled_qty = int(round(fill.filled_qty)) if fill.filled_qty > 0 else p.intended_qty
            exec_price = fill.avg_price if fill.avg_price > 0 else p.intended_price

            # Item 4: confirm broker actually holds shares before opening
            broker_qty = execution._get_broker_qty(symbol) or 0
            if broker_qty <= 0:
                logger.warning(
                    "RECONCILE symbol=%s client_id=%s order_id=%s age=%.0fs "
                    "broker_status=%s filled_qty=%.0f avg_price=%.2f "
                    "action=clear reason=order_filled_but_no_broker_position",
                    symbol, p.client_order_id, fill.order_id, age,
                    fill.status, filled_qty, exec_price,
                )
                m_cleared_stale += 1
                continue

            logger.info(
                "RECONCILE symbol=%s client_id=%s order_id=%s age=%.0fs "
                "broker_status=%s filled_qty=%.0f avg_price=%.2f "
                "broker_qty=%d action=adopt",
                symbol, p.client_order_id, fill.order_id, age,
                fill.status, filled_qty, exec_price, broker_qty,
            )
            # Stop anchored to actual fill price; one entry per symbol so no
            # blended-cost ambiguity — stop_pct * exec_price is always correct.
            positions.open_position(
                symbol,
                filled_qty,
                exec_price,
                p.stop_pct,
                entry_time=now,
                entry_order_id=fill.order_id,
                entry_client_order_id=p.client_order_id,
            )
            m_adopted += 1
            continue

        # ── Terminal: definitively unfilled (includes 404 not-found) ──
        if fill.status == "unfilled":
            reason = "order_not_found_at_broker" if fill.order_id is None else "order_unfilled"
            logger.info(
                "RECONCILE symbol=%s client_id=%s order_id=%s age=%.0fs "
                "broker_status=unfilled action=clear reason=%s",
                symbol, p.client_order_id, fill.order_id, age, reason,
            )
            m_unfilled += 1
            continue

        # ── Non-terminal: order still live at broker ──────────────────
        # fill.status == "unknown" covers new/accepted/pending_new/held
        should_cancel, cancel_reason = _should_cancel_stale_entry(p, age, wall, entry_window_open)

        if p.cancel_attempts >= _PENDING_ENTRY_CANCEL_MAX_ATTEMPTS:
            logger.error(
                "RECONCILE symbol=%s client_id=%s order_id=%s age=%.0fs "
                "broker_status=%s cancel_attempts=%d action=keep "
                "reason=max_cancel_attempts_exceeded — manual intervention required",
                symbol, p.client_order_id, fill.order_id, age,
                fill.status, p.cancel_attempts,
            )
            updated[symbol] = p
            m_kept += 1
            continue

        if not should_cancel:
            logger.warning(
                "RECONCILE symbol=%s client_id=%s order_id=%s age=%.0fs "
                "broker_status=%s cancel_attempts=%d action=keep reason=%s",
                symbol, p.client_order_id, fill.order_id, age,
                fill.status, p.cancel_attempts,
                cancel_reason if cancel_reason else "order_still_live_within_threshold",
            )
            updated[symbol] = p
            m_kept += 1
            continue

        # Attempt cancel with backoff tracking
        logger.warning(
            "RECONCILE symbol=%s client_id=%s order_id=%s age=%.0fs "
            "broker_status=%s cancel_attempts=%d action=cancel reason=%s",
            symbol, p.client_order_id, fill.order_id, age,
            fill.status, p.cancel_attempts + 1, cancel_reason,
        )
        if execution.client is not None and fill.order_id:
            try:
                execution.client.cancel_order_by_id(fill.order_id)
                m_cancel_sent += 1
            except Exception:
                logger.exception(
                    "Failed to cancel stale entry order %s for %s", fill.order_id, symbol
                )
        p.cancel_requested_ts = wall
        p.cancel_attempts += 1
        updated[symbol] = p  # keep; next startup sees terminal state

    state_store.save_pending_entries(updated)
    logger.info(
        "RECONCILE_DONE pending=%d adopted=%d unfilled=%d "
        "cleared_stale=%d kept=%d cancel_sent=%d",
        count, m_adopted, m_unfilled, m_cleared_stale, m_kept, m_cancel_sent,
    )


def fetch_candidates(
    cfg: Config,
    data: DataStack,
    *,
    most_active_count: int,
    date: Optional[datetime] = None,
) -> List[Candidate]:
    date = date or market_now()
    symbols = data.alpaca.get_most_actives(count=most_active_count)
    candidates = build_candidates(
        cfg,
        alpaca=data.alpaca,
        fmp=data.fmp,
        float_cache=data.float_cache,
        symbols=symbols,
        date=date,
    )
    return candidates


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Morning momentum bot entry loop")
    parser.add_argument("--most-active", type=int, default=50, help="Number of symbols to request from Alpaca most-actives")
    parser.add_argument("--watchlist", type=int, default=12, help="Number of candidates to keep after ranking")
    parser.add_argument("--subscribe-count", type=int, default=25, help="Number of symbols to subscribe to for live bars")
    parser.add_argument("--max-bar-history", type=int, default=120, help="Bars kept per symbol for signal calculations")
    parser.add_argument("--equity", type=float, default=100_000.0, help="Account equity used for risk-based sizing")
    parser.add_argument("--max-notional-pct", type=float, default=0.15, help="Max notional per trade as fraction of equity")
    parser.add_argument("--live", action="store_true", help="Use live trading account (default paper)")
    parser.add_argument("--dry-run", action="store_true", help="Log orders without submitting to Alpaca")
    parser.add_argument("--state-path", type=str, default="state/positions.json", help="Path for persisted positions")
    return parser.parse_args(argv)


def _acquire_pid_lock(lock_path: str = "state/bot.pid") -> None:
    """Write current PID to *lock_path*. Warn loudly if another process is running."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing_pid = int(path.read_text().strip())
        except (ValueError, OSError):
            existing_pid = None
        if existing_pid and existing_pid != os.getpid():
            try:
                os.kill(existing_pid, 0)  # signal 0: check existence only
                logger.error(
                    "PID_LOCK another bot instance appears to be running "
                    "(pid=%d lock=%s) — exiting to prevent split-brain trading",
                    existing_pid, lock_path,
                )
                raise SystemExit(1)
            except ProcessLookupError:
                pass  # stale lock from a dead process; safe to overwrite
    path.write_text(str(os.getpid()))
    logger.info("PID_LOCK acquired pid=%d path=%s", os.getpid(), lock_path)


def _release_pid_lock(lock_path: str = "state/bot.pid") -> None:
    """Remove the PID lock file on clean exit."""
    try:
        path = Path(lock_path)
        if path.exists() and path.read_text().strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


def main(argv: Optional[Iterable[str]] = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = parse_args(argv)

    lock_path = str(Path(args.state_path).parent / "bot.pid")
    _acquire_pid_lock(lock_path)
    try:
        _main_inner(args)
    finally:
        _release_pid_lock(lock_path)


def _main_inner(args: argparse.Namespace) -> None:
    cfg = Config()
    data = init_data_stack()

    subscribe_count = max(args.watchlist, args.subscribe_count)

    # Retry loop: keep scanning for candidates until found or entry window opens
    entry_window = config_window(cfg, "entry_start", "entry_cutoff")
    candidates = []
    while not candidates:
        candidates = fetch_candidates(
            cfg,
            data,
            most_active_count=args.most_active,
        )

        if not candidates:
            now = market_now()
            if now >= entry_window.end:
                logger.warning("No candidates found and entry window closed. Exiting.")
                return
            logger.warning(
                "No qualified candidates returned from premarket scan. "
                "Retrying in %d minutes...", cfg.candidate_retry_minutes
            )
            time.sleep(cfg.candidate_retry_minutes * 60)

    watchlist = candidates[: args.watchlist]
    subscribe_symbols = [c.symbol for c in candidates[:subscribe_count]]
    logger.info("Watchlist: %s", ", ".join(c.symbol for c in watchlist))
    logger.info("Subscribing to: %s", ", ".join(subscribe_symbols))

    candidate_map = {c.symbol: c for c in candidates[:subscribe_count]}
    state_store = StateStore(args.state_path)
    risk_manager = RiskManager(cfg, state_store=state_store)
    exec_cfg = ExecutionConfig(
        buy_slippage_pct=cfg.exec_slippage_buy_pct,
        sell_slippage_pct=cfg.exec_slippage_sell_pct,
    )
    execution = ExecutionClient(
        paper=(not args.live),
        dry_run=args.dry_run,
        cfg=exec_cfg,
        quote_provider=data.alpaca.get_latest_quote,
    )
    positions = PositionManager(cfg, execution, risk_manager, state_store=state_store)

    existing_positions = state_store.load_positions()
    if existing_positions:
        positions.load_states(existing_positions)

    risk_payload = state_store.load_risk_state()
    risk_manager.load_state(risk_payload)
    risk_manager.maybe_reset(market_now().date())

    _reconcile_pending_entries(state_store, execution, positions, cfg)

    entry_window = config_window(cfg, "entry_start", "entry_cutoff")
    logger.info(
        "ENTRY_WINDOW start=%s cutoff=%s tz=America/New_York",
        entry_window.start.strftime("%H:%M"),
        entry_window.end.strftime("%H:%M"),
    )

    ctx = EntryContext(
        cfg=cfg,
        data=data,
        watchlist=watchlist,
        max_bar_history=args.max_bar_history,
        candidate_map=candidate_map,
        risk_manager=risk_manager,
        account_equity=args.equity,
        execution=execution,
        positions=positions,
        state_store=state_store,
        subscribe_symbols=subscribe_symbols,
        max_notional=args.equity * args.max_notional_pct,
    )
    loop = EntryLoop(ctx)
    loop.run()


if __name__ == "__main__":
    main()
