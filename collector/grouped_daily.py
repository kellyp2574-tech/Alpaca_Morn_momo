"""
Grouped Daily Aggregates fetcher.

Uses Polygon's Grouped Daily endpoint to fetch all market data
in a single call per trading day - the most efficient approach.
"""

import pandas as pd
import random
from datetime import date, timedelta
from typing import List, Dict, Optional
from collector.storage import DataLakeStorage
from collector.calendar import TradingCalendar
from collector.rate_limiter import rate_limited, get_rate_limiter
from collector.massive_client import get_massive_client


class GroupedDailyFetcher:
    """Fetches market-wide daily data using grouped aggregates."""
    
    def __init__(self, storage: DataLakeStorage, calendar: TradingCalendar):
        self.storage = storage
        self.calendar = calendar
        # Set rate limiter to 20 calls/minute
        self.rate_limiter = get_rate_limiter(20)
    
    @rate_limited(max_retries=5)
    def fetch_grouped_daily(self, trade_date: date) -> Optional[pd.DataFrame]:
        """
        Fetch grouped daily aggregates for a specific date.
        
        This is the key optimization: 1 call per day gets ALL symbols.
        
        Args:
            trade_date: Trading date to fetch
            
        Returns:
            DataFrame with daily bars for all symbols
        """
        # TODO: Replace with actual Polygon API call
        # For now, generate realistic fake data
        
        print(f"Fetching grouped daily for {trade_date.isoformat()}...")
        
        # Simulate API call delay
        import time
        time.sleep(0.1)
        
        # Generate fake market data for the date
        return self._generate_fake_grouped_data(trade_date)
    
    def _generate_fake_grouped_data(self, trade_date: date) -> pd.DataFrame:
        """Generate realistic fake grouped daily data for testing."""
        
        # Get real data from Massive API
        try:
            client = get_massive_client()
            real_data = client.get_grouped_daily(trade_date)
            
            if real_data and 'results' in real_data:
                df = pd.DataFrame(real_data['results'])
                
                # Map abbreviated columns to full names
                column_map = {
                    'T': 'symbol',
                    'v': 'volume', 
                    'vw': 'vwap',
                    'o': 'open',
                    'c': 'close',
                    'h': 'high',
                    'l': 'low',
                    't': 'timestamp',
                    'n': 'transactions'
                }
                
                df = df.rename(columns=column_map)
                
                # Convert timestamp to date string (keep as string to avoid parquet/date issues)
                df['date'] = pd.to_datetime(df['timestamp'], unit='ms').dt.strftime('%Y-%m-%d')
                
                # Calculate dollar volume
                df['dollar_volume'] = df['close'] * df['volume']
                
                # Calculate gaps (need previous close)
                df = df.sort_values(['symbol', 'timestamp'])
                df['prev_close'] = df.groupby('symbol')['close'].shift(1)
                df['gap_pct'] = (df['open'] - df['prev_close']) / df['prev_close']
                df['gap_magnitude'] = abs(df['gap_pct'])
                
                # Calculate 20-day average dollar volume (simplified)
                df['avg_dollar_volume_20d'] = df.groupby('symbol')['dollar_volume'].rolling(20, min_periods=1).mean().reset_index(0, drop=True)
                
                print(f"✅ Real data loaded: {len(df)} symbols for {trade_date}")
                return df
                
        except Exception as e:
            print(f"⚠️  Using fake data due to API error: {e}")
        
        # Fall back to fake data
        return self._generate_fake_grouped_data_fallback(trade_date)
    
    def _generate_fake_grouped_data_fallback(self, trade_date: date) -> pd.DataFrame:
        """Generate realistic fake grouped daily data."""
        # Generate ~2000-3000 symbols per day
        num_symbols = random.randint(2000, 3000)
        
        data = []
        for i in range(num_symbols):
            symbol = f"SYMBOL{i:04d}"
            
            # Generate realistic price data
            base_price = random.uniform(10, 500)
            
            # Generate gap distribution
            gap_random = random.random()
            if gap_random < 0.7:
                # No gap (70%)
                gap_pct = random.uniform(-0.02, 0.02)
            elif gap_random < 0.85:
                # Small gap (15%)
                gap_pct = random.uniform(0.02, 0.05) if random.random() > 0.3 else random.uniform(-0.05, -0.02)
            elif gap_random < 0.95:
                # Medium gap (10%)
                gap_pct = random.uniform(0.05, 0.10) if random.random() > 0.3 else random.uniform(-0.10, -0.05)
            else:
                # Large gap (5%)
                gap_pct = random.uniform(0.10, 0.20) if random.random() > 0.3 else random.uniform(-0.20, -0.10)
            
            # Calculate OHLC based on gap
            prev_close = base_price
            open_price = prev_close * (1 + gap_pct)
            
            # Generate intraday movement
            intraday_vol = random.uniform(0.01, 0.05)
            high = open_price * (1 + random.uniform(0, intraday_vol))
            low = open_price * (1 - random.uniform(0, intraday_vol))
            close = open_price * (1 + random.uniform(-intraday_vol, intraday_vol))
            
            # Generate volume
            base_volume = random.uniform(100000, 5000000)
            volume = int(base_volume * (1 + random.uniform(-0.5, 0.5)))
            
            # Calculate dollar volume
            dollar_volume = volume * close
            
            data.append({
                'symbol': symbol,
                'date': trade_date.isoformat(),
                'open': open_price,
                'high': high,
                'low': low,
                'close': close,
                'volume': volume,
                'vwap': close * random.uniform(0.98, 1.02),  # Slight variation
                'transactions': random.randint(1000, 50000),
                'gap_pct': gap_pct,
                'gap_magnitude': abs(gap_pct),
                'dollar_volume': dollar_volume
            })
        
        return pd.DataFrame(data)
    
    def fetch_range(self, start_date: date, end_date: date) -> pd.DataFrame:
        """
        Fetch grouped daily data for a date range.
        
        Args:
            start_date: Start date
            end_date: End date
            
        Returns:
            Combined DataFrame with all daily data
        """
        trading_days = self.calendar.get_trading_days(start_date, end_date)
        all_data = []
        
        print(f"Fetching {len(trading_days)} trading days...")
        print(f"Estimated time: {len(trading_days) * 3:.0f} minutes at 20 calls/min")
        
        for i, trade_date in enumerate(trading_days):
            try:
                daily_data = self.fetch_grouped_daily(trade_date)
                if daily_data is not None and not daily_data.empty:
                    all_data.append(daily_data)
                
                # Progress indicator
                if (i + 1) % 10 == 0:
                    elapsed = i + 1
                    remaining = len(trading_days) - elapsed
                    print(f"Progress: {elapsed}/{len(trading_days)} days, {remaining} remaining")
                
            except Exception as e:
                print(f"Error fetching {trade_date}: {e}")
                continue
        
        if all_data:
            combined = pd.concat(all_data, ignore_index=True)
            print(f"Total records fetched: {len(combined):,}")
            return combined
        else:
            return pd.DataFrame()
    
    def save_daily_cache(self, data: pd.DataFrame):
        """Save daily data to cache, merging with existing data."""
        if data is not None and not data.empty:
            # Check if we already have data and merge
            existing = self.load_daily_cache()
            if existing is not None and not existing.empty:
                print(f"Merging with existing {len(existing):,} records...")
                # Combine and remove duplicates
                combined = pd.concat([existing, data], ignore_index=True)
                combined = combined.drop_duplicates(subset=['symbol', 'date'], keep='last')
                combined = combined.sort_values(['symbol', 'date'])
                data = combined
                print(f"Combined total: {len(data):,} records")
            
            # Sort by symbol and date for efficient querying
            data = data.sort_values(['symbol', 'date'])
            
            # Save to parquet
            self.storage.write_meta(data, "daily_bars_grouped.parquet")
            print(f"Saved {len(data):,} daily bars to cache")
            
            # Verify it was saved
            verify = self.storage.read_meta("daily_bars_grouped.parquet")
            if verify is not None:
                print(f"Verified: {len(verify):,} records in cache")
            else:
                print("WARNING: Save verification failed!")
        else:
            print("No data to save (empty or None)")
    
    def load_daily_cache(self) -> Optional[pd.DataFrame]:
        """Load daily data from cache."""
        return self.storage.read_meta("daily_bars_grouped.parquet")
    
    def compute_gaps_and_liquidity(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        Compute gap percentages and liquidity metrics with tradeability filters.
        
        Args:
            data: Raw daily bars data
            
        Returns:
            DataFrame with computed metrics and basic liquidity filters
        """
        # Sort by symbol and date
        data = data.sort_values(['symbol', 'date'])
        
        # Debug: check for Path objects in input
        for col in data.columns:
            if data[col].dtype == 'object':
                sample = data[col].dropna().head(1)
                if len(sample) > 0:
                    val = sample.iloc[0]
                    if hasattr(val, '__fspath__'):
                        print(f"WARNING: Found Path object in column {col}")
                        data[col] = data[col].astype(str)
        
        # Compute previous close for each symbol
        data['prev_close'] = data.groupby('symbol')['close'].shift(1)
        
        # Compute gap percentage
        data['gap_pct'] = (data['open'] - data['prev_close']) / data['prev_close']
        data['gap_magnitude'] = abs(data['gap_pct'])
        
        # Compute 20-day average dollar volume (use min_periods to allow partial windows)
        data['avg_dollar_volume_20d'] = data.groupby('symbol')['dollar_volume'].rolling(20, min_periods=5).mean().reset_index(0, drop=True)
        
        # Apply basic liquidity guardrails
        data['price'] = data['close']  # Use close as proxy for current price
        data['meets_min_price'] = data['price'] >= 2.0
        data['meets_min_dollar_volume'] = data['avg_dollar_volume_20d'] >= 10_000_000
        
        # Combined tradeability filter
        data['is_tradeable'] = data['meets_min_price'] & data['meets_min_dollar_volume']
        
        # Filter out records without sufficient history
        data = data.dropna(subset=['prev_close', 'avg_dollar_volume_20d'])
        
        print(f"Liquidity filter results:")
        total_records = len(data)
        print(f"  Total records: {total_records:,}")
        if total_records > 0:
            price_count = data['meets_min_price'].sum()
            volume_count = data['meets_min_dollar_volume'].sum()
            tradeable_count = data['is_tradeable'].sum()
            
            # Ensure we're doing numeric division
            price_pct = float(price_count) / float(total_records) * 100
            volume_pct = float(volume_count) / float(total_records) * 100
            tradeable_pct = float(tradeable_count) / float(total_records) * 100
            
            print(f"  Meets price >= $2: {price_count:,} ({price_pct:.1f}%)")
            print(f"  Meets volume >= $10M: {volume_count:,} ({volume_pct:.1f}%)")
            print(f"  Tradeable (both): {tradeable_count:,} ({tradeable_pct:.1f}%)")
        
        return data
