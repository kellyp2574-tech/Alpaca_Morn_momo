"""Financial Modeling Prep API adapter for float data."""

from __future__ import annotations

import os
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()  # loads FMP_API_KEY from .env if present


class FMPClient:
    def __init__(self, api_key: Optional[str] = None, session: Optional[requests.Session] = None):
        self.api_key = api_key or os.getenv("FMP_API_KEY")
        if not self.api_key:
            raise ValueError("FMP_API_KEY must be provided via args or environment")
        self.session = session or requests.Session()

    def get_float(self, symbol: str) -> Optional[float]:
        url = f"https://financialmodelingprep.com/api/v3/profile/{symbol}?apikey={self.api_key}"
        resp = self.session.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data:
            return None
        float_shares = data[0].get("floatShares")
        return float(float_shares) if float_shares else None
