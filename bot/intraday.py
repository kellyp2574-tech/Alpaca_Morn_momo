"""Intraday monitoring loop for managing open positions only."""

from __future__ import annotations

import argparse
import logging
from typing import Dict, Iterable, Optional

from .clock import config_window, market_now
from .config import Config
from .data_sources import init_data_stack
from .execution import ExecutionClient
from .position_manager import PositionManager
from .risk_manager import RiskManager
from .state_manager import StateStore

logger = logging.getLogger(__name__)


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Morning momentum intraday monitor")
    parser.add_argument("--state-path", type=str, default="state/positions.json", help="Path to persisted positions")
    parser.add_argument("--live", action="store_true", help="Use live trading account (default paper)")
    parser.add_argument("--dry-run", action="store_true", help="Log exits without submitting to Alpaca")
    parser.add_argument("--poll-timeout", type=int, default=5, help="Seconds to wait for stream bars before polling again")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)

    cfg = Config()
    data_stack = init_data_stack()
    state_store = StateStore(args.state_path)
    positions = state_store.load_positions()

    if not positions:
        logger.info("No open positions on disk — intraday loop can stop")
        return

    risk_manager = RiskManager(cfg)
    execution = ExecutionClient(paper=not args.live, dry_run=args.dry_run)
    position_mgr = PositionManager(cfg, execution, risk_manager, state_store=state_store)
    position_mgr.load_states(positions)

    symbols = list(position_mgr.positions.keys())
    logger.info("Monitoring %d symbols intraday: %s", len(symbols), ", ".join(symbols))

    intraday_window = config_window(cfg, "intraday_start", "intraday_end")
    last_prices: Dict[str, float] = {}

    data_stack.alpaca.subscribe_stream(symbols)

    while True:
        now = market_now()
        if now >= intraday_window.end:
            logger.info("Intraday window complete. Forcing exit of remaining positions.")
            break

        bar = data_stack.alpaca.next_bar(timeout=args.poll_timeout)
        if bar is None:
            continue
        if bar.symbol not in symbols:
            continue

        last_prices[bar.symbol] = bar.c
        position_mgr.on_bar(bar.symbol, bar, now=bar.timestamp)

        if not position_mgr.open_count:
            logger.info("All positions closed. Exiting monitor loop.")
            return

    if position_mgr.open_count:
        position_mgr.force_exit_all(last_prices, reason="intraday_end")


if __name__ == "__main__":
    main()
