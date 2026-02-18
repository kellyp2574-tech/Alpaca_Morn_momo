"""Thin Alpaca market data adapter used by the morning momentum bot."""

from __future__ import annotations

import queue
import threading
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, MutableMapping, Optional, Sequence

from dotenv import load_dotenv

try:  # Run-time dependency on alpaca-py
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.live import StockDataStream
    from alpaca.data.requests import StockBarsRequest, StockMostActiveRequest
    from alpaca.data.timeframe import TimeFrame
except (
    ImportError
) as exc:  # pragma: no cover - surfaced when module imported without deps
    raise ImportError("alpaca-py must be installed to use data_alpaca") from exc


load_dotenv()  # loads ALPACA_* variables from .env if present


@dataclass
class MinuteBar:
    symbol: str
    timestamp: datetime
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass
class DailyStats:
    prev_close: float
    avg_vol_30d: float


class AlpacaDataAdapter:
    """Restricted surface area around Alpaca's market data endpoints."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        feed: Optional[str] = None,
    ) -> None:
        api_key = api_key or os.getenv("ALPACA_API_KEY")
        secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY")
        feed = feed or os.getenv("ALPACA_DATA_FEED", "sip")

        if not api_key or not secret_key:
            raise ValueError(
                "Alpaca API key/secret must be provided via args or environment"
            )

        self.feed = feed
        self._historical = StockHistoricalDataClient(api_key, secret_key)
        self._api_key = api_key
        self._secret_key = secret_key

        self._stream: Optional[StockDataStream] = None
        self._stream_thread: Optional[threading.Thread] = None
        self._bar_queue: "queue.Queue[MinuteBar]" = queue.Queue()

    # ------------------------------------------------------------------
    # Historical endpoints

    def get_most_actives(self, count: int = 50) -> List[str]:
        """Return the most-active symbols (by volume)."""
        request = StockMostActiveRequest(top=count, feed=self.feed)
        response = self._historical.get_stock_most_active(request)
        return [entry.symbol for entry in response.most_active]

    def get_bars(
        self,
        symbols: Sequence[str],
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> Dict[str, List[MinuteBar]]:
        """Fetch minute bars for symbols within [start, end]."""
        if timeframe != "1Min":
            raise ValueError("Only 1Min timeframe is supported for premarket scan")

        start_utc = self._ensure_utc(start)
        end_utc = self._ensure_utc(end)

        request = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame.Minute,
            start=start_utc,
            end=end_utc,
            feed=self.feed,
        )
        response = self._historical.get_stock_bars(request)
        out: Dict[str, List[MinuteBar]] = {}
        for symbol, barset in response.data.items():
            out[symbol] = [self._to_minute_bar(symbol, bar) for bar in barset]
        return out

    def get_daily_bars(
        self,
        symbols: Sequence[str],
        lookback_days: int = 35,
        end_dt: Optional[datetime] = None,
    ) -> MutableMapping[str, DailyStats]:
        """Return prev close + 30-day average volume for each symbol."""
        if not symbols:
            return {}
        if end_dt is None:
            end_dt = datetime.utcnow()
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        start_dt = end_dt - timedelta(days=lookback_days * 2)

        request = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame.Day,
            start=self._ensure_utc(start_dt),
            end=self._ensure_utc(end_dt),
            feed=self.feed,
        )
        response = self._historical.get_stock_bars(request)

        stats: Dict[str, DailyStats] = {}
        for symbol, barset in response.data.items():
            if not barset:
                continue
            closes = [bar.close for bar in barset]
            vols = [bar.volume for bar in barset]
            if len(closes) < 2:
                continue
            prev_close = closes[-2]
            vol_window = vols[-30:] if len(vols) >= 30 else vols
            avg_vol = sum(vol_window) / len(vol_window)
            stats[symbol] = DailyStats(prev_close=prev_close, avg_vol_30d=avg_vol)
        return stats

    # ------------------------------------------------------------------
    # Live stream

    def subscribe_stream(self, symbols: Iterable[str]) -> None:
        """Subscribe to live 1m bars for the provided symbols."""
        symbols = list(dict.fromkeys(symbols))
        if not symbols:
            return
        if self._stream is None:
            self._stream = StockDataStream(
                self._api_key, self._secret_key, feed=self.feed
            )

        for symbol in symbols:
            self._stream.subscribe_bars(self._on_stream_bar, symbol)

        if self._stream_thread is None or not self._stream_thread.is_alive():
            self._stream_thread = threading.Thread(target=self._stream.run, daemon=True)
            self._stream_thread.start()

    def next_bar(self, timeout: Optional[float] = None) -> Optional[MinuteBar]:
        """Blocking read for the next streamed bar."""
        try:
            return self._bar_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close_stream(self) -> None:
        if self._stream:
            self._stream.stop()
        if self._stream_thread and self._stream_thread.is_alive():
            self._stream_thread.join(timeout=1)
        self._stream = None
        self._stream_thread = None

    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _to_minute_bar(symbol: str, bar) -> MinuteBar:
        return MinuteBar(
            symbol=symbol,
            timestamp=bar.timestamp,
            o=bar.open,
            h=bar.high,
            l=bar.low,
            c=bar.close,
            v=bar.volume,
        )

    def _on_stream_bar(self, bar) -> None:
        self._bar_queue.put(
            MinuteBar(
                symbol=bar.symbol,
                timestamp=bar.timestamp,
                o=bar.open,
                h=bar.high,
                l=bar.low,
                c=bar.close,
                v=bar.volume,
            )
        )
