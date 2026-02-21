"""
Minute data downloader with manifest tracking.

Downloads 1-minute bars for candidate symbol-days using Polygon API
with restart-safe operation and parallel processing.
"""

import pandas as pd
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import requests
import time
import concurrent.futures
from collector.storage import DataLakeStorage
from collector.manifest import DownloadManifest


class MinuteDownloader:
    """Downloads minute data for candidate symbols with manifest tracking."""
    
    def __init__(self, storage: DataLakeStorage, manifest: DownloadManifest):
        self.storage = storage
        self.manifest = manifest
        self.api_key = None  # TODO: Set from environment
    
    def download_minute_data(self, 
                           trade_date: date,
                           symbol: str,
                           start_time: str = "04:00",
                           end_time: str = "11:00",
                           timezone: str = "America/New_York") -> bool:
        """
        Download minute data for a single symbol-day.
        
        Args:
            trade_date: Trading date
            symbol: Symbol to download
            start_time: Start time in ET (default 04:00)
            end_time: End time in ET (default 11:00)
            timezone: Timezone for times
        
        Returns:
            True if successful, False otherwise
        """
        # Check if already downloaded
        if self.manifest.is_downloaded(trade_date, symbol, 'raw_minute'):
            return True
        
        try:
            # TODO: Implement Polygon API call
            # For now, generate placeholder data
            minute_data = self._generate_placeholder_minute_data(trade_date, symbol, start_time, end_time)
            
            if minute_data.empty:
                self.manifest.mark_failure(trade_date, symbol, 'raw_minute', "No data returned")
                return False
            
            # Store data
            self.storage.write_minute_data(minute_data, trade_date, symbol)
            
            # Get file info
            file_path = str(self.storage.get_minute_file_path(trade_date, symbol))
            file_size = Path(file_path).stat().st_size if Path(file_path).exists() else 0
            
            # Mark success
            self.manifest.mark_success(
                trade_date, symbol, 'raw_minute',
                len(minute_data), file_path, file_size
            )
            
            return True
            
        except Exception as e:
            error_msg = str(e)
            retry_after = datetime.now() + timedelta(minutes=5)  # Backoff
            
            self.manifest.mark_failure(trade_date, symbol, 'raw_minute', error_msg, retry_after)
            return False
    
    def _generate_placeholder_minute_data(self, trade_date: date, symbol: str, 
                                        start_time: str, end_time: str) -> pd.DataFrame:
        """Generate placeholder minute data for testing."""
        # Parse times
        start_hour, start_min = map(int, start_time.split(':'))
        end_hour, end_min = map(int, end_time.split(':'))
        
        # Generate minute timestamps
        timestamps = []
        current_time = datetime.combine(trade_date, datetime.min.time()).replace(
            hour=start_hour, minute=start_min
        )
        
        end_datetime = datetime.combine(trade_date, datetime.min.time()).replace(
            hour=end_hour, minute=end_min
        )
        
        while current_time <= end_datetime:
            timestamps.append(current_time)
            current_time += timedelta(minutes=1)
        
        # Generate placeholder OHLCV data
        base_price = 100.0 + (hash(symbol) % 50)
        data = []
        
        for i, ts in enumerate(timestamps):
            # Simple random walk
            price_change = (hash(symbol + str(i)) % 100 - 50) / 1000
            price = base_price + price_change
            
            data.append({
                'timestamp': ts,
                'open': price,
                'high': price + abs(price_change) + 0.1,
                'low': price - abs(price_change) - 0.1,
                'close': price + (hash(symbol + str(i+1)) % 100 - 50) / 1000,
                'volume': int(1000 + (hash(symbol + str(i)) % 10000)),
                'vwap': price
            })
        
        df = pd.DataFrame(data)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        
        return df
    
    def download_batch(self, 
                      download_plan: List[Tuple[date, str]],
                      max_workers: int = 10,
                      delay_seconds: float = 0.1) -> Dict:
        """
        Download minute data for multiple symbol-days in parallel.
        
        Args:
            download_plan: List of (date, symbol) tuples to download
            max_workers: Number of parallel workers
            delay_seconds: Delay between requests to avoid rate limiting
        
        Returns:
            Dictionary with download statistics
        """
        start_time = datetime.now()
        
        # Filter out already downloaded
        pending = []
        for trade_date, symbol in download_plan:
            if not self.manifest.is_downloaded(trade_date, symbol, 'raw_minute'):
                pending.append((trade_date, symbol))
        
        print(f"Total items: {len(download_plan)}, Already downloaded: {len(download_plan) - len(pending)}, Pending: {len(pending)}")
        
        if not pending:
            return {'total': len(download_plan), 'downloaded': 0, 'failed': 0, 'skipped': len(download_plan)}
        
        # Download in parallel with rate limiting
        successful = []
        failed = []
        
        def download_with_delay(item):
            trade_date, symbol = item
            time.sleep(delay_seconds)  # Rate limiting
            return self.download_minute_data(trade_date, symbol), item
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all jobs
            future_to_item = {executor.submit(download_with_delay, item): item for item in pending}
            
            # Process results as they complete
            for future in concurrent.futures.as_completed(future_to_item):
                try:
                    success, item = future.result()
                    if success:
                        successful.append(item)
                    else:
                        failed.append(item)
                except Exception as e:
                    item = future_to_item[future]
                    failed.append(item)
                    print(f"Error downloading {item}: {e}")
        
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        stats = {
            'total': len(download_plan),
            'downloaded': len(successful),
            'failed': len(failed),
            'skipped': len(download_plan) - len(pending),
            'duration_seconds': duration,
            'downloads_per_second': len(successful) / duration if duration > 0 else 0
        }
        
        return stats
    
    def retry_failed_downloads(self, max_attempts: int = 3, max_workers: int = 5) -> Dict:
        """Retry failed downloads."""
        failed_items = self.manifest.get_failed_downloads('raw_minute', max_attempts)
        
        if not failed_items:
            return {'retried': 0, 'successful': 0, 'failed': 0}
        
        print(f"Retrying {len(failed_items)} failed downloads")
        
        return self.download_batch(failed_items, max_workers=max_workers)
    
    def get_download_progress(self) -> Dict:
        """Get overall download progress statistics."""
        return self.manifest.get_download_stats('raw_minute')
    
    def validate_downloads(self, start_date: date, end_date: date) -> Dict:
        """
        Validate that all expected downloads exist and are not empty.
        
        Returns:
            Dictionary with validation results
        """
        # Get expected downloads from candidates
        candidates = self.storage.read_meta("candidate_days.parquet")
        
        if candidates is None:
            return {'expected': 0, 'missing': 0, 'empty': 0, 'valid': 0}
        
        start_str = start_date.isoformat()
        end_str = end_date.isoformat()
        
        mask = (candidates['date'].astype(str) >= start_str) & (candidates['date'].astype(str) <= end_str)
        expected_downloads = candidates[mask]
        
        missing = []
        empty = []
        valid = []
        
        for _, row in expected_downloads.iterrows():
            trade_date = row['date']
            symbol = row['symbol']
            
            # Check if manifest says downloaded
            if not self.manifest.is_downloaded(trade_date, symbol, 'raw_minute'):
                missing.append((trade_date, symbol))
                continue
            
            # Check if file exists and has data
            data = self.storage.read_minute_data(trade_date, symbol)
            
            if data is None or data.empty:
                empty.append((trade_date, symbol))
            else:
                valid.append((trade_date, symbol))
        
        return {
            'expected': len(expected_downloads),
            'missing': len(missing),
            'empty': len(empty),
            'valid': len(valid),
            'missing_items': missing[:10],  # First 10 missing items
            'empty_items': empty[:10]      # First 10 empty items
        }
