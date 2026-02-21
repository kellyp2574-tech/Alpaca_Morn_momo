"""
Command-line interface for the data collection pipeline.

Provides commands for building universe, fetching daily data, generating candidates,
and downloading minute data.
"""

import argparse
import sys
from datetime import date, datetime, timedelta
from typing import Optional
import pandas as pd

from collector.storage import DataLakeStorage
from collector.calendar import TradingCalendar
from collector.universe import UniverseManager
from collector.daily_cache import DailyCache
from collector.candidates import CandidateGenerator
from collector.minute_downloader import MinuteDownloader
from collector.manifest import DownloadManifest


def build_universe(args):
    """Build tradable universe."""
    print(f"Building universe as of {args.asof}")
    
    storage = DataLakeStorage()
    universe = UniverseManager(storage)
    
    asof_date = date.fromisoformat(args.asof) if isinstance(args.asof, str) else args.asof
    
    universe_df = universe.build_universe(
        asof_date=asof_date,
        min_price=args.min_price,
        min_dollar_volume=args.min_dollar_volume,
        exclude_otc=args.exclude_otc
    )
    
    stats = universe.get_universe_stats(asof_date)
    
    print(f"Universe built with {len(universe_df)} symbols")
    print(f"Stats: {stats}")
    
    return universe_df


def fetch_daily(args):
    """Fetch daily bars data."""
    print(f"Fetching daily bars from {args.start} to {args.end}")
    
    storage = DataLakeStorage()
    calendar = TradingCalendar(storage)
    universe = UniverseManager(storage)
    daily_cache = DailyCache(storage, calendar)
    
    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)
    
    # Get universe symbols
    universe_symbols = universe.get_universe_symbols(start_date)
    
    print(f"Fetching daily bars for {len(universe_symbols)} symbols")
    
    daily_df = daily_cache.fetch_daily_bars(universe_symbols, start_date, end_date)
    
    print(f"Fetched {len(daily_df)} daily bars")
    
    return daily_df


def fetch_daily_grouped(args):
    """Fetch grouped daily aggregates for all market data."""
    from collector.grouped_daily import GroupedDailyFetcher
    
    storage = DataLakeStorage()
    calendar = TradingCalendar(storage)
    
    start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_date = datetime.strptime(args.end, "%Y-%m-%d").date()
    
    fetcher = GroupedDailyFetcher(storage, calendar)
    
    print(f"Fetching grouped daily data from {start_date} to {end_date}")
    print(f"Rate limit: 20 calls/minute")
    print(f"Estimated time: ~{calendar.get_trading_days(start_date, end_date).__len__() * 3:.0f} minutes")
    
    # Check cache first
    cached_data = fetcher.load_daily_cache()
    if cached_data is not None and not cached_data.empty and not args.force:
        print(f"Found cached data with {len(cached_data):,} records")
        return
    
    # Fetch data
    print("Starting fetch...")
    data = fetcher.fetch_range(start_date, end_date)
    print(f"Fetch complete. Records: {len(data) if data is not None else 'None'}")
    
    if data is not None and len(data) > 0:
        # Compute gaps and liquidity metrics
        try:
            data = fetcher.compute_gaps_and_liquidity(data)
        except Exception as e:
            print(f"Error in compute_gaps_and_liquidity: {e}")
            import traceback
            traceback.print_exc()
            return
        
        # Save to cache
        try:
            fetcher.save_daily_cache(data)
        except Exception as e:
            print(f"Error in save_daily_cache: {e}")
            import traceback
            traceback.print_exc()
            return
        
        print(f"Successfully fetched and cached {len(data):,} daily bars")
        print(f"Date range: {data['date'].min()} to {data['date'].max()}")
        print(f"Symbols: {data['symbol'].nunique():,}")
    else:
        print("No data fetched")


def make_candidates(args):
    """Generate candidate list."""
    print(f"Generating candidates from {args.start} to {args.end}")
    
    storage = DataLakeStorage()
    calendar = TradingCalendar(storage)
    universe = UniverseManager(storage)
    daily_cache = DailyCache(storage, calendar)
    candidates = CandidateGenerator(storage, daily_cache, universe)
    
    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)
    
    # Handle legacy single gap vs multiple thresholds
    if args.gap is not None:
        # Legacy mode - single threshold
        gap_thresholds = [args.gap]
        print(f"Using legacy single gap threshold: {args.gap:.1%}")
    else:
        # Multi-threshold mode
        gap_thresholds = args.thresholds
        print(f"Using gap thresholds: {[f'{g:.1%}' for g in gap_thresholds]}")
    
    candidates_dict = candidates.generate_candidates(
        start_date=start_date,
        end_date=end_date,
        gap_thresholds=gap_thresholds,
        min_price=args.min_price,
        min_dollar_volume=args.min_dollar_volume,
        exclude_etfs=args.exclude_etfs
    )
    
    # Show stats for each threshold
    print("\n=== Candidate Statistics ===")
    for threshold_name, candidates_df in candidates_dict.items():
        stats = candidates.get_candidate_stats(start_date, end_date, threshold_name)
        print(f"\n{threshold_name}+:")
        print(f"  Total candidates: {stats.get('total_candidates', 0)}")
        print(f"  Avg per day: {stats.get('avg_candidates_per_day', 0):.1f}")
        print(f"  Gap up/down: {stats.get('gap_up_count', 0)}/{stats.get('gap_down_count', 0)}")
        print(f"  Avg gap size: {stats.get('avg_gap_magnitude', 0):.1%}")
    
    # Show overall stats
    overall_stats = candidates.get_candidate_stats(start_date, end_date)
    if overall_stats:
        print(f"\nOverall (all thresholds):")
        print(f"  Total candidates: {overall_stats['total_candidates']}")
        print(f"  Unique dates: {overall_stats['unique_dates']}")
        print(f"  Unique symbols: {overall_stats['unique_symbols']}")
    
    return candidates_dict


def fetch_minutes_optimized(args):
    """Download minute data using optimized rate-limited approach."""
    from collector.minute_downloader_optimized import OptimizedMinuteDownloader
    from datetime import timedelta
    
    storage = DataLakeStorage()
    calendar = TradingCalendar(storage)
    
    downloader = OptimizedMinuteDownloader(storage, calendar)
    
    if args.threshold:
        # Download specific threshold
        print(f"Downloading minute data for {args.threshold} threshold")
        
        # Load candidates for this threshold
        filename = f"candidate_days_{args.threshold}.parquet"
        candidates = storage.read_meta(filename)
        
        if candidates is None or candidates.empty:
            print(f"No candidates found for {args.threshold}")
            return
        
        # Download
        stats = downloader.download_for_candidates(
            candidates, 
            workers=args.workers, 
            delay=args.delay
        )
        
        print(f"Download complete: {stats['success']} success, {stats['failed']} failed")
        print(f"Duration: {stats['duration']:.1f} seconds")
        
    else:
        # Download by priority (recommended)
        print("Downloading minute data by priority (high-value buckets first)")
        
        # Use priority order for early results
        priority_order = ['15pct', '10pct', '7pct', '5pct', '3pct']
        
        stats = downloader.download_by_priority(
            start_date=datetime.now().date() - timedelta(days=365*5),  # Last 5 years
            end_date=datetime.now().date(),
            min_dollar_volume=10_000_000,
            priority_order=priority_order
        )
        
        print(f"Priority download complete")
        print(f"Total: {stats['total_success']} success, {stats['total_failed']} failed")
        print(f"Duration: {stats['total_duration']:.1f} seconds")


def fetch_minutes(args):
    """Download minute data for candidates."""
    print(f"Downloading minute data with {args.workers} workers")
    
    storage = DataLakeStorage()
    manifest = DownloadManifest(storage)
    downloader = MinuteDownloader(storage, manifest)
    
    # Get download plan from candidates
    if args.threshold:
        # Download for specific threshold
        filename = f"candidate_days_{args.threshold}.parquet"
        candidates = storage.read_meta(filename)
        print(f"Downloading for threshold: {args.threshold}")
    else:
        # Download all candidates
        candidates = storage.read_meta("candidate_days.parquet")
        print("Downloading for all thresholds")
    
    if candidates is None or candidates.empty:
        print("No candidates found. Run 'make-candidates' first.")
        return
    
    # Convert to download plan
    download_plan = [(row['date'], row['symbol']) for _, row in candidates.iterrows()]
    
    print(f"Download plan contains {len(download_plan)} symbol-days")
    
    # Download data
    stats = downloader.download_batch(
        download_plan=download_plan,
        max_workers=args.workers,
        delay_seconds=args.delay
    )
    
    print(f"Download stats: {stats}")
    
    # Show progress
    progress = downloader.get_download_progress()
    print(f"Overall progress: {progress}")
    
    return stats


def retry_downloads(args):
    """Retry failed downloads."""
    print(f"Retrying failed downloads with {args.workers} workers")
    
    storage = DataLakeStorage()
    manifest = DownloadManifest(storage)
    downloader = MinuteDownloader(storage, manifest)
    
    stats = downloader.retry_failed_downloads(
        max_attempts=args.max_attempts,
        max_workers=args.workers
    )
    
    print(f"Retry stats: {stats}")
    
    return stats


def validate_data(args):
    """Validate downloaded data."""
    print(f"Validating data from {args.start} to {args.end}")
    
    storage = DataLakeStorage()
    manifest = DownloadManifest(storage)
    downloader = MinuteDownloader(storage, manifest)
    
    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)
    
    # Get candidates for validation
    if args.threshold:
        filename = f"candidate_days_{args.threshold}.parquet"
        candidates = storage.read_meta(filename)
        print(f"Validating threshold: {args.threshold}")
    else:
        candidates = storage.read_meta("candidate_days.parquet")
        print("Validating all thresholds")
    
    if candidates is None or candidates.empty:
        print("No candidates found to validate.")
        return
    
    # Filter by date range
    start_str = start_date.isoformat()
    end_str = end_date.isoformat()
    mask = (candidates['date'].astype(str) >= start_str) & (candidates['date'].astype(str) <= end_str)
    filtered_candidates = candidates[mask]
    
    print(f"Validating {len(filtered_candidates)} expected downloads")
    
    # Validate each
    missing = []
    valid = []
    empty = []
    
    for _, row in filtered_candidates.iterrows():
        trade_date = row['date']
        symbol = row['symbol']
        
        # Check if manifest says downloaded
        if not manifest.is_downloaded(trade_date, symbol, 'raw_minute'):
            missing.append((trade_date, symbol))
            continue
        
        # Check if file exists and has data
        data = storage.read_minute_data(trade_date, symbol)
        
        if data is None or data.empty:
            empty.append((trade_date, symbol))
        else:
            valid.append((trade_date, symbol))
    
    validation = {
        'expected': len(filtered_candidates),
        'missing': len(missing),
        'empty': len(empty),
        'valid': len(valid),
        'missing_items': missing[:10],  # First 10 missing items
        'empty_items': empty[:10]      # First 10 empty items
    }
    
    print(f"Validation results: {validation}")
    
    return validation


def show_status(args):
    """Show collection status."""
    storage = DataLakeStorage()
    manifest = DownloadManifest(storage)
    
    print("=== Data Collection Status ===")
    
    # Trading days
    trading_days = storage.read_meta("trading_days.parquet")
    if trading_days is not None:
        print(f"Trading days cached: {len(trading_days)}")
        print(f"Date range: {trading_days['date'].min()} to {trading_days['date'].max()}")
    
    # Daily bars
    daily_bars = storage.read_meta("daily_bars.parquet")
    if daily_bars is not None:
        print(f"Daily bars cached: {len(daily_bars)}")
        if not daily_bars.empty:
            print(f"Date range: {daily_bars['date'].min()} to {daily_bars['date'].max()}")
            print(f"Symbols: {daily_bars['symbol'].nunique()}")
    
    # Candidates by threshold
    if args.threshold:
        # Show specific threshold
        filename = f"candidate_days_{args.threshold}.parquet"
        candidates = storage.read_meta(filename)
        if candidates is not None:
            print(f"Candidates ({args.threshold}): {len(candidates)}")
            if not candidates.empty:
                print(f"Date range: {candidates['date'].min()} to {candidates['date'].max()}")
                print(f"Symbols: {candidates['symbol'].nunique()}")
    else:
        # Show all thresholds
        thresholds = ['3pct', '5pct', '7pct', '10pct', '15pct']
        for threshold in thresholds:
            filename = f"candidate_days_{threshold}.parquet"
            candidates = storage.read_meta(filename)
            if candidates is not None:
                print(f"Candidates ({threshold}): {len(candidates)}")
        
        # Also show combined
        combined = storage.read_meta("candidate_days.parquet")
        if combined is not None:
            print(f"Total candidates (all thresholds): {len(combined)}")
    
    # Download progress
    progress = manifest.get_download_stats('raw_minute')
    if progress:
        print(f"Minute downloads: {progress}")
    
    # Data size
    dates_with_data = storage.list_dates_with_data("minute")
    print(f"Dates with minute data: {len(dates_with_data)}")
    
    if dates_with_data:
        total_symbols = 0
        for trade_date in dates_with_data:
            symbols = storage.list_symbols_for_date(trade_date, "minute")
            total_symbols += len(symbols)
        
        print(f"Total symbol-days with minute data: {total_symbols}")


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(description="Data collection CLI for morning momentum backtesting")
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Build universe command
    universe_parser = subparsers.add_parser('build-universe', help='Build tradable universe')
    universe_parser.add_argument('--asof', required=True, help='Date to build universe as of (YYYY-MM-DD)')
    universe_parser.add_argument('--min-price', type=float, default=1.0, help='Minimum price')
    universe_parser.add_argument('--min-dollar-volume', type=float, default=10_000_000, help='Minimum dollar volume')
    universe_parser.add_argument('--exclude-otc', action='store_true', default=True, help='Exclude OTC stocks')
    
    # Fetch daily command
    daily_parser = subparsers.add_parser('fetch-daily', help='Fetch daily bars')
    daily_parser.add_argument('--start', required=True, help='Start date (YYYY-MM-DD)')
    daily_parser.add_argument('--end', required=True, help='End date (YYYY-MM-DD)')
    
    # Fetch grouped daily command (NEW - optimized)
    grouped_parser = subparsers.add_parser('fetch-daily-grouped', help='Fetch grouped daily aggregates (1 call/day)')
    grouped_parser.add_argument('--start', required=True, help='Start date (YYYY-MM-DD)')
    grouped_parser.add_argument('--end', required=True, help='End date (YYYY-MM-DD)')
    grouped_parser.add_argument('--force', action='store_true', help='Force refresh even if cached')
    
    # Make candidates command
    candidates_parser = subparsers.add_parser('make-candidates', help='Generate candidates')
    candidates_parser.add_argument('--start', required=True, help='Start date (YYYY-MM-DD)')
    candidates_parser.add_argument('--end', required=True, help='End date (YYYY-MM-DD)')
    candidates_parser.add_argument('--gap', type=float, help='Single gap threshold (legacy)')
    candidates_parser.add_argument('--thresholds', nargs='+', type=float, default=[0.03, 0.05, 0.07, 0.10, 0.15], help='Gap thresholds (default: 0.03 0.05 0.07 0.10 0.15)')
    candidates_parser.add_argument('--min-price', type=float, default=1.50, help='Minimum price')
    candidates_parser.add_argument('--min-dollar-volume', type=float, default=10_000_000, help='Minimum dollar volume')
    candidates_parser.add_argument('--exclude-etfs', action='store_true', default=True, help='Exclude ETFs')
    
    # Fetch minutes command
    minutes_parser = subparsers.add_parser('fetch-minutes', help='Download minute data')
    minutes_parser.add_argument('--workers', type=int, default=10, help='Number of parallel workers')
    minutes_parser.add_argument('--delay', type=float, default=0.1, help='Delay between requests (seconds)')
    minutes_parser.add_argument('--threshold', type=str, help='Specific gap threshold to download (e.g., "3pct", "5pct")')
    
    # Fetch minutes optimized command (NEW - rate limited)
    minutes_opt_parser = subparsers.add_parser('fetch-minutes-optimized', help='Download minute data with rate limiting (20 calls/min)')
    minutes_opt_parser.add_argument('--workers', type=int, default=3, help='Number of parallel workers (recommended: 3)')
    minutes_opt_parser.add_argument('--delay', type=float, default=3.0, help='Delay between requests (recommended: 3.0s)')
    minutes_opt_parser.add_argument('--threshold', type=str, help='Specific gap threshold to download (e.g., "3pct", "5pct")')
    
    # Retry downloads command
    retry_parser = subparsers.add_parser('retry-downloads', help='Retry failed downloads')
    retry_parser.add_argument('--workers', type=int, default=5, help='Number of parallel workers')
    retry_parser.add_argument('--max-attempts', type=int, default=3, help='Maximum retry attempts')
    
    # Validate data command
    validate_parser = subparsers.add_parser('validate', help='Validate downloaded data')
    validate_parser.add_argument('--start', required=True, help='Start date (YYYY-MM-DD)')
    validate_parser.add_argument('--end', required=True, help='End date (YYYY-MM-DD)')
    validate_parser.add_argument('--threshold', type=str, help='Specific gap threshold to validate (e.g., "3pct", "5pct")')
    
    # Status command
    status_parser = subparsers.add_parser('status', help='Show collection status')
    status_parser.add_argument('--threshold', type=str, help='Show status for specific threshold')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    try:
        if args.command == 'build-universe':
            build_universe(args)
        elif args.command == 'fetch-daily':
            fetch_daily(args)
        elif args.command == 'fetch-daily-grouped':
            fetch_daily_grouped(args)
        elif args.command == 'make-candidates':
            make_candidates(args)
        elif args.command == 'fetch-minutes':
            fetch_minutes(args)
        elif args.command == 'fetch-minutes-optimized':
            fetch_minutes_optimized(args)
        elif args.command == 'retry-downloads':
            retry_downloads(args)
        elif args.command == 'validate':
            validate_data(args)
        elif args.command == 'status':
            show_status(args)
        else:
            print(f"Unknown command: {args.command}")
            parser.print_help()
    
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
