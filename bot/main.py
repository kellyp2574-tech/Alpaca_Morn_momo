"""Entry loop orchestration for the morning momentum bot."""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, Dict, Iterable, List, Optional

from .clock import MARKET_TZ, config_window, market_datetime, market_now
from .config import Config
from .data_alpaca import Quote
from .data_sources import DataStack, init_data_stack
from .execution import ExecutionClient, ExecutionConfig
from .indicators import VWAPState, atr_1m
from .premarket_scan import build_candidates
from .position_manager import PositionManager, calc_qty, initial_stop_pct
from .risk_manager import RiskManager
from .state_manager import StateStore
from .storage import Candidate

logger = logging.getLogger(__name__)


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
    subscribe_symbols: List[str]
    max_notional: float

    def watch_symbols(self) -> List[str]:
        return self.subscribe_symbols


@dataclass
class EntryDecision:
    price: float
    qty: int
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
        self.market_open_dt = market_datetime(config_window(ctx.cfg, "entry_start", "entry_cutoff").start, ctx.cfg.market_open)
        self.latest_quotes: Dict[str, Quote] = {}
        self.last_quote_refresh = datetime.fromtimestamp(0, tz=MARKET_TZ)

    def run(self) -> None:
        cfg = self.ctx.cfg
        entry_window = config_window(cfg, "entry_start", "entry_cutoff")
        hard_exit_window = config_window(cfg, "entry_start", "hard_exit")
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
            self.risk_manager.maybe_reset(now.date())
            self._maybe_refresh_quotes(now, watch_symbols)
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
            self._place_entry(bar.symbol, history[-1], decision, now)

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
        notional = qty * entry_price
        max_notional = self.ctx.max_notional
        if max_notional > 0 and notional > max_notional:
            qty = int(max_notional / entry_price)
        else:
            qty = int(qty)
        if qty < 1:
            return None

        return EntryDecision(price=entry_price, qty=qty, stop_pct=stop_pct, atr=atr)

    def _place_entry(self, symbol: str, bar, decision: EntryDecision, now: datetime) -> None:
        """Send the entry order (placeholder logs until execution module is ready)."""

        if self.positions.has_position(symbol):
            return

        logger.info(
            "ENTER %s qty %s @ %.2f | stop %.2f%% | ATR %.3f",
            symbol,
            decision.qty,
            decision.price,
            decision.stop_pct * 100,
            decision.atr,
        )
        exec_price = decision.price * (1 + self.ctx.cfg.entry_slip_pct)
        cap_price = decision.price * (1 + self.ctx.cfg.entry_max_slip_pct)
        exec_price = min(exec_price, cap_price)
        exec_price = round(exec_price, 2)
        order_id = self.ctx.execution.place_entry(symbol, decision.qty, exec_price)
        if order_id:
            logger.info("Order %s acknowledged for %s", order_id, symbol)
        elif not self.ctx.execution.dry_run:
            logger.warning("Entry order for %s was not acknowledged; skipping position open", symbol)
            return

        self.positions.open_position(
            symbol,
            decision.qty,
            exec_price,
            decision.stop_pct,
            entry_time=now,
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


def main(argv: Optional[Iterable[str]] = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = parse_args(argv)

    cfg = Config()
    data = init_data_stack()

    subscribe_count = max(args.watchlist, args.subscribe_count)
    candidates = fetch_candidates(
        cfg,
        data,
        most_active_count=args.most_active,
    )

    if not candidates:
        logger.warning("No qualified candidates returned from premarket scan.")

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
        risk_manager.trades_taken = max(
            risk_manager.trades_taken, len(existing_positions)
        )

    risk_payload = state_store.load_risk_state()
    risk_manager.load_state(risk_payload)
    risk_manager.maybe_reset(market_now().date())

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
        subscribe_symbols=subscribe_symbols,
        max_notional=args.equity * args.max_notional_pct,
    )
    loop = EntryLoop(ctx)
    loop.run()


if __name__ == "__main__":
    main()
