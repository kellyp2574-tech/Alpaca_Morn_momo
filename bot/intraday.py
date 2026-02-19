"""Intraday monitoring loop for managing open positions only."""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from typing import Dict, Iterable, Optional

from .clock import MARKET_TZ, config_window, market_now
from .config import Config
from .data_alpaca import Quote
from .data_sources import init_data_stack
from .execution import ExecutionClient, ExecutionConfig
from .position_manager import PositionManager
from .risk_manager import RiskManager
from .state_manager import StateStore

logger = logging.getLogger(__name__)


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Morning momentum intraday monitor")
    parser.add_argument(
        "--state-path",
        type=str,
        default="state/positions.json",
        help="Path to persisted positions",
    )
    parser.add_argument(
        "--live", action="store_true", help="Use live trading account (default paper)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Log exits without submitting to Alpaca"
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=5,
        help="Seconds to wait for stream bars before polling again",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = parse_args(argv)

    cfg = Config()
    data_stack = init_data_stack()
    state_store = StateStore(args.state_path)
    positions = state_store.load_positions()

    if not positions:
        logger.info("No open positions on disk — intraday loop can stop")
        return

    risk_manager = RiskManager(cfg, state_store=state_store)
    exec_cfg = ExecutionConfig(
        buy_slippage_pct=cfg.exec_slippage_buy_pct,
        sell_slippage_pct=cfg.exec_slippage_sell_pct,
    )
    execution = ExecutionClient(
        paper=not args.live,
        dry_run=args.dry_run,
        cfg=exec_cfg,
    )
    position_mgr = PositionManager(
        cfg, execution, risk_manager, state_store=state_store
    )
    position_mgr.load_states(positions)

    # Item 6: reconcile any in-flight exits immediately at handoff so the
    # intraday monitor starts with a broker-truth view, not a stale local view.
    now_reconcile = market_now()
    logger.info("INTRADAY_RECONCILE starting at handoff %s", now_reconcile.strftime("%H:%M:%S"))
    for symbol, state in list(position_mgr.positions.items()):
        if state.exit_pending:
            logger.info(
                "INTRADAY_RECONCILE symbol=%s exit_pending=True order_id=%s — reconciling",
                symbol, state.exit_order_id,
            )
            position_mgr._reconcile_pending_exit(symbol, state, now_reconcile)
    logger.info(
        "INTRADAY_RECONCILE done open_positions=%d",
        position_mgr.open_count,
    )

    symbols = list(position_mgr.positions.keys())
    if not symbols:
        logger.info("All positions resolved at handoff reconcile — intraday loop not needed")
        return

    logger.info("Monitoring %d symbols intraday: %s", len(symbols), ", ".join(symbols))

    intraday_window = config_window(cfg, "intraday_start", "intraday_end")
    last_prices: Dict[str, float] = {}
    latest_quotes: Dict[str, Quote] = {}
    last_quote_refresh = datetime.fromtimestamp(0, tz=MARKET_TZ)

    data_stack.alpaca.subscribe_stream(symbols)

    while True:
        now = market_now()
        if now >= intraday_window.end:
            logger.info(
                "Intraday window complete. Forcing exit of remaining positions."
            )
            break

        # Refresh quotes and check for spread expansion exits
        refresh_interval = cfg.quote_refresh_seconds
        if refresh_interval > 0 and (now - last_quote_refresh).total_seconds() >= refresh_interval:
            try:
                fresh = data_stack.alpaca.get_latest_quotes(list(position_mgr.positions.keys()))
                if fresh:
                    latest_quotes.update(fresh)
                    last_quote_refresh = now
            except Exception:
                logger.exception("Failed to refresh quotes in intraday loop")

        # Stale quote guard: don't act on quotes that are too old
        quote_age = (now - last_quote_refresh).total_seconds()
        if quote_age <= cfg.quote_refresh_seconds * 2:
            for symbol, state in list(position_mgr.positions.items()):
                if state.exit_pending:
                    continue

                # Grace period: ignore spread right after entry
                age = (now - state.entry_time).total_seconds()
                if age < cfg.spread_exit_grace_seconds:
                    continue

                quote = latest_quotes.get(symbol)
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
                position_mgr.exit_position(symbol, quote.bid_price, now, reason="spread_expansion")

        bar = data_stack.alpaca.next_bar(timeout=args.poll_timeout)
        if bar is None:
            continue
        if bar.symbol not in symbols:
            continue

        last_prices[bar.symbol] = bar.c
        position_mgr.on_bar(bar.symbol, bar, now=now)

        if not position_mgr.open_count:
            logger.info("All positions closed. Exiting monitor loop.")
            return

    if position_mgr.open_count:
        position_mgr.force_exit_all(last_prices, reason="intraday_end")


if __name__ == "__main__":
    main()
