"""
Command-line interface for gap backtesting.
"""

import argparse
import sys
from datetime import date
from collector.storage import DataLakeStorage
from backtest.pure_signal_backtester import PureSignalBacktester
from backtest.strategy_simulator import GapStrategyBacktester


def run_pure_signal_backtest(args):
    """Run pure signal backtest (Test 1) - no filters, just gap signal."""
    print(f"Running Pure Signal Backtest from {args.start} to {args.end}")
    print(f"Minimum dollar volume: ${args.min_dollar_volume:,}")
    print(f"Minimum price: ${args.min_price}")
    print()
    
    storage = DataLakeStorage()
    backtester = PureSignalBacktester(storage)
    
    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)
    
    results = backtester.run_backtest(
        start_date=start_date,
        end_date=end_date,
        min_dollar_volume=args.min_dollar_volume,
        min_price=args.min_price,
        slippage=args.slippage,
        opening_strength=args.opening_strength,
        entry_time=args.entry_time,
        year=args.year,
        exclude_top_pct=args.exclude_top_pct,
        portfolio=args.portfolio,
        daily_deploy_pct=args.daily_deploy_pct,
        min_volume_5min=args.min_volume_5min,
        bucket_range=args.bucket_range,
        entry_randomize=args.entry_randomize,
        fill_haircut=args.fill_haircut,
        capacity_test=args.capacity_test,
        max_daily_deploy=args.max_daily_deploy,
        max_return_pct=args.max_return_pct,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
        hard_exit=args.hard_exit,
        pessimistic_tp_sl=args.pessimistic_tp_sl,
        partial_tp_pct=args.partial_tp_pct,
        trail_pct=args.trail_pct,
        monte_carlo=args.monte_carlo,
        block_size=args.block_size,
        stress_slippage=args.stress_slippage,
        stress_participation=args.stress_participation
    )
    
    # Skip print_summary for capacity test (it has its own output)
    if not args.capacity_test:
        backtester.print_summary(results)
    return results


def run_strategy_backtest(args):
    """Run full strategy backtest with all filters."""
    print(f"Running Strategy Backtest from {args.start} to {args.end}")
    print(f"Minimum dollar volume: ${args.min_dollar_volume:,}")
    print()
    
    storage = DataLakeStorage()
    backtester = GapStrategyBacktester(storage)
    
    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)
    
    results = backtester.run_backtest(
        start_date=start_date,
        end_date=end_date,
        min_dollar_volume=args.min_dollar_volume
    )
    
    backtester.print_summary(results)
    return results


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(description="Gap backtesting CLI")
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Pure signal backtest (Test 1)
    pure_parser = subparsers.add_parser('pure', help='Pure signal backtest (Test 1) - no filters')
    pure_parser.add_argument('--start', required=True, help='Start date (YYYY-MM-DD)')
    pure_parser.add_argument('--end', required=True, help='End date (YYYY-MM-DD)')
    pure_parser.add_argument('--min-dollar-volume', type=float, default=10_000_000,
                            help='Minimum dollar volume (default: 10M)')
    pure_parser.add_argument('--min-price', type=float, default=2.0,
                            help='Minimum price (default: $2)')
    pure_parser.add_argument('--slippage', type=float, default=0.0,
                            help='Round-trip slippage as decimal (e.g., 0.005 = 0.5%)')
    pure_parser.add_argument('--opening-strength', action='store_true',
                            help='Only enter if first 5-min candle is green (9:35 close > 9:30 open)')
    pure_parser.add_argument('--entry-time', default='9:35',
                            help='Entry time (HH:MM, default: 9:35)')
    pure_parser.add_argument('--hard-exit', type=str, default='10:30',
                            help='Hard exit time (HH:MM, default: 10:30)')
    pure_parser.add_argument('--year', type=int, choices=[2021, 2022, 2023, 2024, 2025],
                            help='Test single year only (2021, 2022, 2023, 2024 or 2025)')
    pure_parser.add_argument('--exclude-top-pct', type=float, default=0.0,
                            help='Exclude top X%% of trades by return (e.g., 1 for 1%%)')
    pure_parser.add_argument('--portfolio', action='store_true',
                            help='Simulate equal-weight portfolio growth (1%% per trade)')
    pure_parser.add_argument('--daily-deploy-pct', type=float, default=0.40,
                            help='Daily portfolio deploy %% (default: 0.40 = 40%%)')
    pure_parser.add_argument('--min-volume-5min', type=float, default=0.0,
                            help='Minimum volume in first 5 min (in dollars, e.g., 1000000 for $1M)')
    pure_parser.add_argument('--bucket-range', type=str, default=None,
                            help='Single bucket range (e.g., "7-15" for 7-15%%). Overrides default 7-10 and 10-15 buckets')
    pure_parser.add_argument('--entry-randomize', action='store_true',
                            help='Randomize entry by ±1 minute (9:34-9:36) for robustness testing')
    pure_parser.add_argument('--fill-haircut', type=float, default=0.0,
                            help='Partial fill haircut (e.g., 0.0025 = 0.25%% worse fill)')
    pure_parser.add_argument('--capacity-test', action='store_true',
                            help='Run capacity sensitivity analysis with multiple starting capital levels')
    pure_parser.add_argument('--max-daily-deploy', type=float, default=0.0,
                            help='Hard cap on daily deployment dollars (e.g., 50000 for $50k)')
    pure_parser.add_argument('--max-return-pct', type=float, default=80.0,
                            help='Sanity filter: exclude trades with return > X%% (default: 80)')
    pure_parser.add_argument('--take-profit', type=float, default=0.0,
                            help='Take profit threshold (e.g., 0.01 = 1%%). Exit if return >= TP before hard exit')
    pure_parser.add_argument('--stop-loss', type=float, default=0.0,
                            help='Stop loss threshold (e.g., 0.01 = 1%%). Exit if return <= -SL before hard exit')
    pure_parser.add_argument('--pessimistic-tp-sl', action='store_true',
                            help='Pessimistic mode: TP/SL triggers only on close price, not high/low')
    pure_parser.add_argument('--partial-tp-pct', type=float, default=0.0,
                            help='Partial TP: sell X%% of position at TP (e.g., 0.5 = 50%%). Remaining position uses trail/hard exit')
    pure_parser.add_argument('--trail-pct', type=float, default=0.0,
                            help='Trailing stop percentage for remaining position after partial TP')
    pure_parser.add_argument('--monte-carlo', type=int, default=0,
                            help='Run Monte Carlo simulation with N bootstrap iterations')
    pure_parser.add_argument('--block-size', type=int, default=10,
                            help='Block size for bootstrap (default: 10 days)')
    pure_parser.add_argument('--stress-slippage', type=float, default=0.0,
                            help='Stress test slippage: override default slippage for sensitivity analysis')
    pure_parser.add_argument('--stress-participation', type=float, default=0.0,
                            help='Stress test: override volume participation cap (e.g., 0.02 for 2%%)')
    
    # Full strategy backtest (Test 2)
    strategy_parser = subparsers.add_parser('strategy', help='Full strategy backtest - all filters')
    strategy_parser.add_argument('--start', required=True, help='Start date (YYYY-MM-DD)')
    strategy_parser.add_argument('--end', required=True, help='End date (YYYY-MM-DD)')
    strategy_parser.add_argument('--min-dollar-volume', type=float, default=10_000_000,
                                help='Minimum dollar volume (default: 10M)')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    try:
        if args.command == 'pure':
            run_pure_signal_backtest(args)
        elif args.command == 'strategy':
            run_strategy_backtest(args)
        else:
            print(f"Unknown command: {args.command}")
            parser.print_help()
    
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
