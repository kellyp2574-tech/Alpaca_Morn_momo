"""
Optimized minute data downloader with rate limiting.

Only downloads minute data for symbols that pass candidate filters.
Uses token bucket rate limiting to respect API limits.
"""

import pandas as pd
from datetime import date, datetime, timedelta
from datetime import time as tm
from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import time as time_module
import random

from collector.storage import DataLakeStorage
from collector.calendar import TradingCalendar
from collector.rate_limiter import get_rate_limiter
from collector.massive_client import get_massive_client


class OptimizedMinuteDownloader:
    """Downloads minute data only for filtered candidates."""
    
    def __init__(self, storage: DataLakeStorage, calendar: TradingCalendar):
        self.storage = storage
        self.calendar = calendar
        # Rate limiter: 20 calls/minute
        self.rate_limiter = get_rate_limiter(20)
    
    def download_for_candidates(self, 
                               candidates: pd.DataFrame,
                               workers: int = 3,
                               delay: float = 3.0) -> Dict:
        """
        Download minute data for candidate symbols only.
        
        Args:
            candidates: DataFrame of candidate symbols and dates
            workers: Number of parallel workers
            delay: Delay between requests in seconds
            
        Returns:
            Download statistics
        """
        print(f"Downloading minute data for {len(candidates)} candidate symbol-days")
        print(f"Rate limit: 20 calls/minute")
        print(f"Workers: {workers}, Delay: {delay}s")
        
        # Estimate time
        estimated_minutes = len(candidates) / 20  # 20 calls per minute
        print(f"Estimated time: {estimated_minutes:.1f} minutes")
        
        # Group candidates by date for efficient processing
        candidates['date_str'] = candidates['date'].astype(str)
        grouped = candidates.groupby('date_str')
        
        stats = {
            'total': len(candidates),
            'success': 0,
            'failed': 0,
            'skipped': 0,
            'start_time': time_module.time()
        }
        
        # Process with worker pool
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = []
            
            for date_str, day_candidates in grouped:
                for _, candidate in day_candidates.iterrows():
                    future = executor.submit(
                        self._download_symbol_day,
                        candidate['symbol'],
                        candidate['date'],
                        delay
                    )
                    futures.append((future, candidate['symbol'], candidate['date']))
            
            # Process results
            for future, symbol, trade_date in futures:
                try:
                    success = future.result()
                    if success:
                        stats['success'] += 1
                    else:
                        stats['failed'] += 1
                except Exception as e:
                    print(f"Error downloading {symbol} {trade_date}: {e}")
                    stats['failed'] += 1
                
                # Progress indicator
                if (stats['success'] + stats['failed']) % 100 == 0:
                    elapsed = time_module.time() - stats['start_time']
                    processed = stats['success'] + stats['failed']
                    remaining = stats['total'] - processed
                    rate = processed / elapsed if elapsed > 0 else 0
                    eta = remaining / rate if rate > 0 else 0
                    print(f"Progress: {processed}/{stats['total']} ({rate:.1f}/min, ETA: {eta:.0f}s)")
        
        stats['end_time'] = time_module.time()
        stats['duration'] = stats['end_time'] - stats['start_time']
        
        return stats
    
    def _download_symbol_day(self, symbol: str, trade_date: str, delay: float) -> bool:
        """
        Download minute data for a single symbol on a single date.
        
        Args:
            symbol: Stock symbol
            trade_date: Trading date (YYYY-MM-DD string)
            delay: Delay between requests
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Parse date string to date object
            if isinstance(trade_date, str):
                trade_date_obj = datetime.strptime(trade_date, "%Y-%m-%d").date()
            else:
                trade_date_obj = trade_date
            
            # Check if already downloaded
            if self.storage.read_minute_data(trade_date_obj, symbol) is not None:
                return True
            
            # Rate limiting is handled by the @rate_limited decorator in massive_client
            # Just add small delay between requests to avoid overwhelming the API
            if delay > 0:
                time_module.sleep(delay)
            
            # Fetch REAL minute data from API
            minute_data = self._fetch_real_minute_data(symbol, trade_date_obj)
            
            if minute_data is not None and not minute_data.empty:
                # Save to storage - correct order: date, symbol, df
                self.storage.write_minute_data(trade_date_obj, symbol, minute_data)
                return True
            else:
                return False
                
        except Exception as e:
            print(f"Error downloading {symbol} {trade_date}: {e}")
            return False
    
    def _generate_fake_minute_data(self, symbol: str, trade_date: date) -> Optional[pd.DataFrame]:
        """Generate realistic fake minute data for testing."""
        try:
            # trade_date is already a date object
            trade_dt = trade_date
            
            # Generate minutes from 4:00 AM to 11:00 AM
            minutes = []
            current_time = datetime.combine(trade_dt, tm(4, 0))
            end_time = datetime.combine(trade_dt, tm(11, 0))
            
            # Base price from daily data (simulate)
            base_price = random.uniform(10, 200)
            
            while current_time <= end_time:
                # Generate OHLC for this minute
                volatility = random.uniform(0.001, 0.005)
                
                open_price = base_price * (1 + random.uniform(-volatility, volatility))
                high_price = open_price * (1 + random.uniform(0, volatility))
                low_price = open_price * (1 - random.uniform(0, volatility))
                close_price = open_price * (1 + random.uniform(-volatility, volatility))
                
                volume = random.randint(100, 10000)
                
                minutes.append({
                    'timestamp': current_time,
                    'open': open_price,
                    'high': high_price,
                    'low': low_price,
                    'close': close_price,
                    'volume': volume,
                    'vwap': close_price
                })
                
                # Update base price for next minute
                base_price = close_price
                current_time += timedelta(minutes=1)
            
            return pd.DataFrame(minutes)
            
        except Exception as e:
            print(f"Error generating minute data for {symbol} {trade_date}: {e}")
            return None
    
    def _fetch_real_minute_data(self, symbol: str, trade_date: date) -> Optional[pd.DataFrame]:
        """Fetch real minute data from Massive API."""
        try:
            client = get_massive_client()
            data = client.get_minute_aggregates(symbol, trade_date)
            
            if data is None or 'results' not in data:
                return None
            
            results = data['results']
            if not results:
                return None
            
            # Convert to DataFrame
            df = pd.DataFrame(results)
            
            # Map column names
            column_map = {
                'o': 'open',
                'c': 'close',
                'h': 'high',
                'l': 'low',
                'v': 'volume',
                't': 'timestamp',
                'vw': 'vwap'
            }
            df = df.rename(columns=column_map)
            
            # Convert timestamp to datetime
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            return df
            
        except Exception as e:
            # Silently return None on error - API failures are expected with rate limiting
            return None
    
    def download_by_priority(self, 
                           start_date: date,
                           end_date: date,
                           min_dollar_volume: float = 10_000_000,
                           priority_order: List[str] = ['15pct', '10pct', '7pct', '5pct', '3pct']) -> Dict:
        """
        Download minute data by bucket priority.
        
        Downloads high-value buckets first for early results.
        
        Args:
            start_date: Start date
            end_date: End date
            min_dollar_volume: Minimum dollar volume filter
            priority_order: Order of buckets to download
            
        Returns:
            Combined download statistics
        """
        print(f"Downloading minute data by priority: {priority_order}")
        
        total_stats = {
            'buckets': {},
            'total_success': 0,
            'total_failed': 0,
            'total_duration': 0
        }
        
        for bucket in priority_order:
            print(f"\n=== Downloading {bucket} bucket ===")
            
            # Load candidates for this bucket
            filename = f"candidate_days_{bucket}.parquet"
            candidates = self.storage.read_meta(filename)
            
            if candidates is None or candidates.empty:
                print(f"No candidates found for {bucket}")
                continue
            
            # Filter by date range and dollar volume
            candidates['date'] = pd.to_datetime(candidates['date']).dt.date
            mask = (candidates['date'] >= start_date) & (candidates['date'] <= end_date)
            mask &= candidates['avg_dollar_volume_20d'] >= min_dollar_volume
            filtered = candidates[mask]
            
            print(f"Found {len(filtered)} candidates in {bucket} bucket")
            
            if filtered.empty:
                continue
            
            # Download for this bucket
            bucket_stats = self.download_for_candidates(
                filtered, workers=3, delay=3.0
            )
            
            total_stats['buckets'][bucket] = bucket_stats
            total_stats['total_success'] += bucket_stats['success']
            total_stats['total_failed'] += bucket_stats['failed']
            total_stats['total_duration'] += bucket_stats['duration']
            
            print(f"Bucket {bucket} complete: {bucket_stats['success']} success, {bucket_stats['failed']} failed")
        
        # Summary
        print(f"\n=== Priority Download Summary ===")
        print(f"Total success: {total_stats['total_success']}")
        print(f"Total failed: {total_stats['total_failed']}")
        print(f"Total duration: {total_stats['total_duration']:.1f} seconds")
        
        for bucket, stats in total_stats['buckets'].items():
            print(f"  {bucket}: {stats['success']}/{stats['total']} ({stats['success']/stats['total']:.1%})")
        
        return total_stats
