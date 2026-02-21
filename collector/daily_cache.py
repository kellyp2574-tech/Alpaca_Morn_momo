"""
Daily bars caching and management.

Handles fetching, storing, and retrieving daily bar data
for universe symbols to enable candidate filtering.
"""

import pandas as pd
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional
from pathlib import Path
import requests
from collector.storage import DataLakeStorage
from collector.calendar import TradingCalendar


class DailyCache:
    """Manages daily bars data for universe symbols."""
    
    def __init__(self, storage: DataLakeStorage, calendar: TradingCalendar):
        self.storage = storage
        self.calendar = calendar
    
    def fetch_daily_bars(self, symbols: List[str], start_date: date, end_date: date) -> pd.DataFrame:
        """
        Fetch daily bars for specified symbols and date range.
        
        Args:
            symbols: List of symbols to fetch
            start_date: Start date
            end_date: End date
        
        Returns:
            DataFrame with daily bars data
        """
        # TODO: Implement Polygon API call
        # For now, generate placeholder data
        
        daily_data = []
        trading_days = self.calendar.get_trading_days(start_date, end_date)
        
        for symbol in symbols:
            prev_close = 100.0  # Start with base price
            
            for trade_date in trading_days:
                # Generate gap: 70% no gap, 20% small gap, 10% large gap
                gap_random = (hash(symbol + trade_date.isoformat()) % 100) / 100.0
                
                if gap_random < 0.7:
                    # No significant gap (small random movement)
                    gap_size = (hash(symbol + trade_date.isoformat() + 'small') % 1000 - 500) / 1000 * 0.005  # ±0.5%
                elif gap_random < 0.9:
                    # Small gap (1-3%)
                    gap_size = 0.01 + (hash(symbol + trade_date.isoformat() + 'gap') % 100) / 100 * 0.02
                else:
                    # Large gap (5-15%)
                    gap_size = 0.05 + (hash(symbol + trade_date.isoformat() + 'large') % 100) / 100 * 0.10
                
                # Apply gap direction
                if (hash(symbol + trade_date.isoformat() + 'dir') % 2) == 0:
                    gap_size = -gap_size
                
                # Calculate open price with gap
                open_price = prev_close * (1 + gap_size)
                
                # Generate intraday movement
                intraday_change = (hash(symbol + trade_date.isoformat() + 'intra') % 1000 - 500) / 1000 * 0.02  # ±2%
                close_price = open_price * (1 + intraday_change)
                
                daily_data.append({
                    'symbol': symbol,
                    'date': trade_date.isoformat(),
                    'open': open_price,
                    'high': max(open_price, close_price) * (1 + abs(intraday_change) * 0.5),
                    'low': min(open_price, close_price) * (1 - abs(intraday_change) * 0.5),
                    'close': close_price,
                    'volume': int(1_000_000 + (hash(symbol) % 5_000_000)),
                    'vwap': (open_price + close_price) / 2,
                    'trade_count': int(10000 + hash(symbol) % 50000)
                })
                
                # Update prev_close for next day
                prev_close = close_price
        
        df = pd.DataFrame(daily_data)
        df['date'] = pd.to_datetime(df['date'])
        
        # Store to cache
        self.storage.write_meta(df, "daily_bars.parquet")
        
        # Also store with gaps computed for candidate generation
        df_with_gaps = self.compute_gaps_from_dataframe(df)
        self.storage.write_meta(df_with_gaps, "daily_bars_with_gaps.parquet")
        
        return df
    
    def load_daily_bars(self) -> Optional[pd.DataFrame]:
        """Load daily bars from cache."""
        return self.storage.read_meta("daily_bars.parquet")
    
    def get_daily_bars_for_symbol(self, symbol: str, start_date: date, end_date: date) -> Optional[pd.DataFrame]:
        """Get daily bars for specific symbol in date range."""
        cached = self.load_daily_bars()
        
        if cached is None:
            return None
        
        start_str = start_date.isoformat()
        end_str = end_date.isoformat()
        
        mask = (cached['symbol'] == symbol) & (cached['date'] >= start_str) & (cached['date'] <= end_str)
        return cached[mask].sort_values('date')
    
    def get_daily_bars_for_date(self, trade_date: date) -> Optional[pd.DataFrame]:
        """Get daily bars for all symbols on specific date."""
        cached = self.load_daily_bars()
        
        if cached is None:
            return None
        
        date_str = trade_date.isoformat()
        return cached[cached['date'] == date_str].sort_values('symbol')
    
    def compute_gaps_from_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Compute gap percentages from existing daily bars dataframe.
        """
        # Sort by symbol and date
        df = df.sort_values(['symbol', 'date'])
        
        # Compute previous close for each symbol
        df['prev_close'] = df.groupby('symbol')['close'].shift(1)
        
        # Compute gap percentage
        df['gap_pct'] = (df['open'] - df['prev_close']) / df['prev_close']
        
        # Remove first day for each symbol (no previous close)
        df = df[df['prev_close'].notna()]
        
        return df
    
    def compute_gaps(self, symbols: List[str], start_date: date, end_date: date) -> pd.DataFrame:
        """
        Compute gap percentages for symbols.
        
        Gap = (open - prev_close) / prev_close
        """
        daily_data = self.load_daily_bars()
        
        if daily_data is None:
            daily_data = self.fetch_daily_bars(symbols, start_date, end_date)
        
        # Sort by symbol and date
        daily_data = daily_data.sort_values(['symbol', 'date'])
        
        # Compute previous close for each symbol
        daily_data['prev_close'] = daily_data.groupby('symbol')['close'].shift(1)
        
        # Compute gap percentage
        daily_data['gap_pct'] = (daily_data['open'] - daily_data['prev_close']) / daily_data['prev_close']
        
        # Remove first day for each symbol (no previous close)
        daily_data = daily_data[daily_data['prev_close'].notna()]
        
        return daily_data
    
    def get_liquidity_metrics(self, symbols: List[str], lookback_days: int = 20) -> pd.DataFrame:
        """
        Compute liquidity metrics for symbols.
        
        Args:
            symbols: List of symbols
            lookback_days: Number of days to look back for averages
        
        Returns:
            DataFrame with liquidity metrics
        """
        daily_data = self.load_daily_bars()
        
        if daily_data is None:
            return pd.DataFrame()
        
        # Compute rolling averages
        daily_data = daily_data.sort_values(['symbol', 'date'])
        
        # Dollar volume = close * volume
        daily_data['dollar_volume'] = daily_data['close'] * daily_data['volume']
        
        # Rolling averages by symbol
        daily_data['avg_volume_20d'] = daily_data.groupby('symbol')['volume'].rolling(lookback_days, min_periods=1).mean().reset_index(0, drop=True)
        daily_data['avg_dollar_volume_20d'] = daily_data.groupby('symbol')['dollar_volume'].rolling(lookback_days, min_periods=1).mean().reset_index(0, drop=True)
        
        return daily_data
