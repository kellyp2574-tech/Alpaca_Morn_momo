"""Financial Modeling Prep API adapter for float data."""

from __future__ import annotations

import os
import time
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()  # loads FMP_API_KEY from .env if present


class FMPClient:
    def __init__(
        self, api_key: Optional[str] = None, session: Optional[requests.Session] = None
    ):
        self.api_key = api_key or os.getenv("FMP_API_KEY")
        if not self.api_key:
            raise ValueError("FMP_API_KEY must be provided via args or environment")
        self.session = session or requests.Session()

    def get_float(self, symbol: str) -> Optional[float]:
        url = f"https://financialmodelingprep.com/api/v3/profile/{symbol}?apikey={self.api_key}"
        backoff = 1.0
        for attempt in range(3):
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                break
            if resp.status_code in {429, 500, 502, 503} and attempt < 2:
                time.sleep(backoff)
                backoff *= 2
                continue
            return None

        data = resp.json()
        if not data:
            return None
        entry = data[0]
        float_shares = entry.get("floatShares") or entry.get("sharesOutstanding")
        try:
            return float(float_shares) if float_shares else None
        except (TypeError, ValueError):
            return None
