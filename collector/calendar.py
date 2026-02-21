"""
Trading calendar management.

Handles trading day lists, holiday management, and date utilities
for the backtesting framework.
"""

import pandas as pd
from datetime import date, datetime, timedelta
from typing import List, Optional, Set
from pathlib import Path
import requests
from collector.storage import DataLakeStorage


class TradingCalendar:
    """Manages trading calendar and date operations."""
    
    def __init__(self, storage: DataLakeStorage):
        self.storage = storage
        self._trading_days: Optional[pd.DataFrame] = None
    
    def load_trading_days(self) -> pd.DataFrame:
        """Load trading days from local cache or fetch if needed."""
        cached = self.storage.read_meta("trading_days.parquet")
        
        if cached is not None:
            self._trading_days = cached
            return cached
        
        # If no cache, we'll need to fetch from API
        # For now, generate weekdays as fallback
        return self._generate_weekday_calendar()
    
    def fetch_trading_days(self, start_date: date, end_date: date) -> pd.DataFrame:
        """Fetch trading days from Polygon API."""
        # TODO: Implement Polygon API call
        # For now, generate weekdays as placeholder
        return self._generate_weekday_calendar(start_date, end_date)
    
    def _generate_weekday_calendar(self, start_date: Optional[date] = None, 
                                 end_date: Optional[date] = None) -> pd.DataFrame:
        """Generate weekday calendar as fallback."""
        if start_date is None:
            start_date = date(2021, 1, 1)
        if end_date is None:
            end_date = date(2026, 12, 31)
        
        trading_days = []
        current = start_date
        
        while current <= end_date:
            # Monday = 0, Friday = 4
            if current.weekday() < 5:  # Weekday
                trading_days.append({
                    'date': current.isoformat(),
                    'is_trading_day': True,
                    'is_half_day': False
                })
            current += timedelta(days=1)
        
        df = pd.DataFrame(trading_days)
        df['date'] = pd.to_datetime(df['date'])
        
        # Store to cache
        self.storage.write_meta(df, "trading_days.parquet")
        self._trading_days = df
        
        return df
    
    def get_trading_days(self, start_date: date, end_date: date) -> List[date]:
        """Get list of trading days in range."""
        if self._trading_days is None:
            self.load_trading_days()
        
        start_str = start_date.isoformat()
        end_str = end_date.isoformat()
        
        mask = (self._trading_days['date'] >= start_str) & (self._trading_days['date'] <= end_str)
        filtered = self._trading_days[mask]
        
        return [d.date() for d in filtered['date']]
    
    def is_trading_day(self, check_date: date) -> bool:
        """Check if a date is a trading day."""
        if self._trading_days is None:
            self.load_trading_days()
        
        check_str = check_date.isoformat()
        return any(self._trading_days['date'] == check_str)
    
    def get_previous_trading_day(self, current_date: date) -> Optional[date]:
        """Get the previous trading day."""
        if self._trading_days is None:
            self.load_trading_days()
        
        current_str = current_date.isoformat()
        previous_days = self._trading_days[self._trading_days['date'] < current_str]
        
        if previous_days.empty:
            return None
        
        return previous_days.iloc[-1]['date'].date()
    
    def get_next_trading_day(self, current_date: date) -> Optional[date]:
        """Get the next trading day."""
        if self._trading_days is None:
            self.load_trading_days()
        
        current_str = current_date.isoformat()
        next_days = self._trading_days[self._trading_days['date'] > current_str]
        
        if next_days.empty:
            return None
        
        return next_days.iloc[0]['date'].date()
    
    def get_trading_day_count(self, start_date: date, end_date: date) -> int:
        """Get count of trading days in range."""
        return len(self.get_trading_days(start_date, end_date))
