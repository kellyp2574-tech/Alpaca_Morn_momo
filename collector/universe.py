"""
Tradable universe management.

Builds and maintains filtered universe of tradeable stocks
to keep data collection manageable.
"""

import pandas as pd
from datetime import date, datetime, timedelta
from typing import List, Set, Dict, Optional
from pathlib import Path
import requests
from collector.storage import DataLakeStorage


class UniverseManager:
    """Manages tradable universe filtering and selection."""
    
    def __init__(self, storage: DataLakeStorage):
        self.storage = storage
    
    def build_universe(self, asof_date: date, 
                      min_price: float = 1.0,
                      min_dollar_volume: float = 10_000_000,
                      exclude_otc: bool = True) -> pd.DataFrame:
        """
        Build tradable universe as of specified date.
        
        Args:
            asof_date: Date to build universe as of
            min_price: Minimum stock price
            min_dollar_volume: Minimum average daily dollar volume (20-day)
            exclude_otc: Whether to exclude OTC stocks
        
        Returns:
            DataFrame with universe symbols and metadata
        """
        # TODO: Implement Polygon API call to get all US stocks
        # For now, return placeholder universe
        
        universe_data = []
        
        # Placeholder universe - in real implementation, fetch from Polygon
        placeholder_symbols = [
            "AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "META", "NVDA", "AMD",
            "NFLX", "DIS", "BAC", "WMT", "PG", "JNJ", "UNH", "HD", "MA",
            "V", "PYPL", "ADBE", "CRM", "NFLX", "CMCSA", "INTC", "CSCO",
            "PEP", "COST", "TMO", "ABT", "DHR", "VZ", "TXN", "ABBV",
            "ACN", "QCOM", "MDT", "NEE", "HON", "UNP", "LIN", "IBM",
            "AMGN", "BA", "GE", "MMM", "CVX", "CAT", "DE", "GS", "TRV",
            "RTX", "KO", "JPM", "MS", "BLK", "SPGI", "ICE", "CME",
            "AON", "CB", "MMC", "AJG", "AIG", "ALL", "MET", "PRU",
            "LNC", "CINF", "HIG", "RJF", "AFL", "GNW", "MFC", "AIZ"
        ]
        
        for symbol in placeholder_symbols:
            universe_data.append({
                'symbol': symbol,
                'asof_date': asof_date.isoformat(),
                'price': 50.0,  # Placeholder
                'avg_dollar_volume_20d': 50_000_000,  # Placeholder
                'market_cap': 1_000_000_000,  # Placeholder
                'is_otc': False,
                'sector': 'Technology'  # Placeholder
            })
        
        df = pd.DataFrame(universe_data)
        
        # Apply filters
        df = df[df['price'] >= min_price]
        df = df[df['avg_dollar_volume_20d'] >= min_dollar_volume]
        
        if exclude_otc:
            df = df[~df['is_otc']]
        
        # Store universe snapshot
        filename = f"universe_{asof_date.isoformat()}.parquet"
        self.storage.write_meta(df, filename)
        
        return df
    
    def load_universe(self, asof_date: date) -> Optional[pd.DataFrame]:
        """Load previously built universe for date."""
        filename = f"universe_{asof_date.isoformat()}.parquet"
        return self.storage.read_meta(filename)
    
    def get_universe_symbols(self, asof_date: date) -> List[str]:
        """Get list of symbols in universe for date."""
        universe = self.load_universe(asof_date)
        
        if universe is None:
            universe = self.build_universe(asof_date)
        
        return universe['symbol'].tolist()
    
    def refresh_universe_monthly(self, year: int, month: int) -> pd.DataFrame:
        """Build universe for first trading day of specified month."""
        # TODO: Find first trading day of month
        asof_date = date(year, month, 1)
        
        # Adjust to first weekday if month starts on weekend
        while asof_date.weekday() >= 5:  # Saturday or Sunday
            asof_date += timedelta(days=1)
        
        return self.build_universe(asof_date)
    
    def get_universe_stats(self, asof_date: date) -> Dict:
        """Get statistics about universe for date."""
        universe = self.load_universe(asof_date)
        
        if universe is None:
            return {}
        
        return {
            'total_symbols': len(universe),
            'avg_price': universe['price'].mean(),
            'avg_dollar_volume': universe['avg_dollar_volume_20d'].mean(),
            'otc_count': universe['is_otc'].sum(),
            'sectors': universe['sector'].value_counts().to_dict() if 'sector' in universe.columns else {}
        }
