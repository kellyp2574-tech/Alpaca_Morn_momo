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
        min_price=args.min_price
    )
    
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
