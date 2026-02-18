"""Entry loop orchestration for the morning momentum bot."""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Deque, Dict, Iterable, List, Optional

from .clock import config_window, market_now
from .config import Config
from .data_sources import DataStack, init_data_stack
from .execution import ExecutionClient
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

    def watch_symbols(self) -> List[str]:
        return [c.symbol for c in self.watchlist]


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

            history = self.bar_history[bar.symbol]
            history.append(bar)
            self.vwap_state[bar.symbol].update(bar)
            self.last_prices[bar.symbol] = bar.c

            self.positions.on_bar(bar.symbol, bar, now=bar.timestamp)

            if not entry_window.contains(bar.timestamp):
                continue

            if self.positions.has_position(bar.symbol):
                continue

            decision = self._evaluate_entry(bar.symbol, history)
            if not decision:
                continue
            if not self.risk_manager.can_enter(self.positions.open_count):
                continue
            self._place_entry(bar.symbol, history[-1], decision)

    # ------------------------------------------------------------------
    def _evaluate_entry(self, symbol: str, bars: Deque) -> Optional[EntryDecision]:
        cfg = self.ctx.cfg
        candidate = self.ctx.candidate_map.get(symbol)
        if candidate is None:
            return None

        bars_seq = list(bars)
        min_len = max(cfg.atr_len + 1, cfg.volume_avg_window + 1)
        if len(bars_seq) < min_len:
            return None

        atr = atr_1m(bars_seq, cfg.atr_len)
        if atr <= 0:
            return None

        last_bar = bars_seq[-1]
        vwap = self.vwap_state[symbol].vwap
        if last_bar.c < vwap:
            return None
        if last_bar.c <= candidate.pm_high:
            return None

        vol_window = cfg.volume_avg_window
        if vol_window > 0:
            recent = bars_seq[-(vol_window + 1) : -1]
            prev_vols = [bar.v for bar in recent]
            if len(prev_vols) < vol_window:
                return None
            avg_vol = sum(prev_vols) / vol_window if vol_window else 0.0
            if avg_vol <= 0:
                return None
            if last_bar.v < cfg.volume_spike_mult * avg_vol:
                return None

        stop_pct = initial_stop_pct(cfg, atr, last_bar.c)
        qty = calc_qty(
            self.ctx.account_equity, cfg.risk_per_trade, last_bar.c, stop_pct
        )
        if qty < 1:
            return None
        qty = int(qty)

        return EntryDecision(price=last_bar.c, qty=qty, stop_pct=stop_pct, atr=atr)

    def _place_entry(self, symbol: str, bar, decision: EntryDecision) -> None:
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
        order_id = self.ctx.execution.place_entry(symbol, decision.qty, decision.price)
        if order_id:
            logger.info("Order %s acknowledged for %s", order_id, symbol)
        self.positions.open_position(
            symbol,
            decision.qty,
            decision.price,
            decision.stop_pct,
            entry_time=bar.timestamp,
        )

    def _hard_exit(self) -> None:
        if not self.positions.open_count:
            return
        logger.info("Force exiting all open positions")
        self.positions.force_exit_all(self.last_prices, reason="hard_exit")


# ----------------------------------------------------------------------


def build_watchlist(
    cfg: Config,
    data: DataStack,
    *,
    most_active_count: int,
    watchlist_size: int,
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
    watchlist = candidates[:watchlist_size]
    logger.info("Watchlist: %s", ", ".join(c.symbol for c in watchlist))
    return watchlist


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Morning momentum bot entry loop")
    parser.add_argument(
        "--most-active",
        type=int,
        default=50,
        help="Number of symbols to request from Alpaca most-actives",
    )
    parser.add_argument(
        "--watchlist",
        type=int,
        default=12,
        help="Number of candidates to keep after ranking",
    )
    parser.add_argument(
        "--max-bar-history",
        type=int,
        default=120,
        help="Bars kept per symbol for signal calculations",
    )
    parser.add_argument(
        "--equity",
        type=float,
        default=100_000.0,
        help="Account equity used for risk-based sizing",
    )
    parser.add_argument(
        "--live", action="store_true", help="Use live trading account (default paper)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Log orders without submitting to Alpaca"
    )
    parser.add_argument(
        "--state-path",
        type=str,
        default="state/positions.json",
        help="Path for persisted positions",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = parse_args(argv)

    cfg = Config()
    data = init_data_stack()

    watchlist = build_watchlist(
        cfg,
        data,
        most_active_count=args.most_active,
        watchlist_size=args.watchlist,
    )

    candidate_map = {c.symbol: c for c in watchlist}
    risk_manager = RiskManager(cfg)
    execution = ExecutionClient(paper=(not args.live), dry_run=args.dry_run)
    state_store = StateStore(args.state_path)
    positions = PositionManager(cfg, execution, risk_manager, state_store=state_store)

    existing_positions = state_store.load_positions()
    if existing_positions:
        positions.load_states(existing_positions)
        risk_manager.trades_taken = max(
            risk_manager.trades_taken, len(existing_positions)
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
    )
    loop = EntryLoop(ctx)
    loop.run()


if __name__ == "__main__":
    main()
