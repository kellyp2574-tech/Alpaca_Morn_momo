"""
Massive.com API client wrapper.

Handles authentication and rate-limited API calls for data collection.
"""

import os
import requests
from typing import Optional, Dict, Any
from datetime import date, datetime
import pandas as pd
from dotenv import load_dotenv
from .rate_limiter import rate_limited, get_rate_limiter

# Load environment variables
load_dotenv()


class MassiveClient:
    """Massive.com API client with authentication and rate limiting."""
    
    def __init__(self):
        """Initialize client with API key from environment."""
        self.api_key = os.getenv('MASSIVE_API_KEY')
        if not self.api_key:
            raise ValueError("MASSIVE_API_KEY environment variable not set")
        
        self.base_url = "https://api.massive.com/v2"
        self.rate_limiter = get_rate_limiter(20)  # 20 calls/minute
        
        # Session with authentication
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        })
    
    @rate_limited(max_retries=5)
    def get_grouped_daily(self, trade_date: date) -> Optional[Dict]:
        """
        Get grouped daily aggregates for all symbols on a specific date.
        
        This is the key optimization - 1 call gets ALL symbols!
        
        Args:
            trade_date: Trading date
            
        Returns:
            Dictionary with grouped daily data or None if error
        """
        date_str = trade_date.isoformat()
        url = f"{self.base_url}/aggs/grouped/locale/us/market/stocks/{date_str}"
        
        try:
            response = self.session.get(url)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Error fetching grouped daily for {date_str}: {e}")
            return None
    
    def get_minute_aggregates(self, symbol: str, trade_date: date, 
                              start_time: str = "04:00:00", 
                              end_time: str = "11:00:00") -> Optional[Dict]:
        """
        Get minute aggregates for a specific symbol and date.
        
        Args:
            symbol: Stock symbol
            trade_date: Trading date
            start_time: Start time in HH:MM:SS format
            end_time: End time in HH:MM:SS format
            
        Returns:
            Dictionary with minute data or None if error
        """
        date_str = trade_date.isoformat()
        url = f"{self.base_url}/aggs/ticker/{symbol}/range/1/minute/{date_str}/{date_str}"
        
        params = {
            'adjusted': 'true',
            'sort': 'timestamp',
            'order': 'asc'
        }
        
        try:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Error fetching minute data for {symbol} {date_str}: {e}")
            return None
    
    def get_ticker_details(self, symbol: str) -> Optional[Dict]:
        """Get basic ticker information."""
        url = f"{self.base_url}/reference/tickers/{symbol}"
        
        try:
            response = self.session.get(url)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Error fetching ticker details for {symbol}: {e}")
            return None
    
    def get_market_status(self) -> Optional[Dict]:
        """Get current market status."""
        url = f"{self.base_url}/market/status"
        
        try:
            response = self.session.get(url)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Error fetching market status: {e}")
            return None


# Global client instance
_massive_client: Optional[MassiveClient] = None


def get_massive_client() -> MassiveClient:
    """Get or create global Massive client."""
    global _massive_client
    if _massive_client is None:
        _massive_client = MassiveClient()
    return _massive_client
