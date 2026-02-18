"""Premarket scanning pipeline for the morning momentum bot."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, List

from .clock import window_from_strings
from .config import Config
from .ranking import score_candidate
from .storage import Candidate


def build_candidates(
    cfg: Config,
    alpaca,
    fmp,
    float_cache,
    symbols: Iterable[str],
    date: datetime,
) -> List[Candidate]:
    """Build and rank symbols that pass the premarket filters.

    Args:
        cfg: Strategy configuration.
        alpaca: Adapter exposing Alpaca data helpers.
        fmp: Float data provider with get_float().
        float_cache: FloatCache-like instance.
        symbols: Iterable of symbols from most-actives list.
        date: Trading date.
    """

    floats = {}
    missing = []
    for symbol in symbols:
        fs = float_cache.get(symbol)
        if fs is None:
            missing.append(symbol)
        else:
            floats[symbol] = fs

    for symbol in missing:
        fs = fmp.get_float(symbol)
        if fs and fs > 0:
            float_cache.set(symbol, fs)
            floats[symbol] = fs

    tracked_symbols = [s for s in symbols if s in floats]
    if not tracked_symbols:
        return []

    scan_window = window_from_strings(
        reference=date,
        start_str=cfg.scan_start,
        end_str=cfg.scan_end,
    )
    pm_bars = alpaca.get_bars(
        tracked_symbols,
        timeframe="1Min",
        start=scan_window.start,
        end=scan_window.end,
    )
    daily = alpaca.get_daily_bars(
        tracked_symbols, lookback_days=35, end_dt=scan_window.start
    )

    candidates: List[Candidate] = []
    for symbol in tracked_symbols:
        bars = pm_bars.get(symbol, [])
        if len(bars) < 5:
            continue

        pm_volume = sum(bar.v for bar in bars)
        pm_high = max(bar.h for bar in bars)
        pm_last = bars[-1].c

        daily_stats = daily.get(symbol)
        if not daily_stats:
            continue

        prev_close = daily_stats.prev_close
        avg_vol_30d = daily_stats.avg_vol_30d

        fs = floats[symbol]
        gap_pct = (pm_last - prev_close) / prev_close if prev_close > 0 else 0.0
        pm_vol_float = pm_volume / fs if fs > 0 else 0.0
        relvol = pm_volume / avg_vol_30d if avg_vol_30d > 0 else 0.0

        price = pm_last
        if not (cfg.min_price <= price <= cfg.max_price):
            continue
        if fs > cfg.max_float:
            continue
        if gap_pct < cfg.min_gap_pct:
            continue
        if pm_vol_float < cfg.min_pm_vol_float:
            continue
        if relvol < cfg.min_relvol:
            continue

        score = score_candidate(gap_pct, pm_vol_float, relvol)
        candidates.append(
            Candidate(
                symbol=symbol,
                price=price,
                prev_close=prev_close,
                pm_last=pm_last,
                pm_high=pm_high,
                pm_volume=pm_volume,
                avg_vol_30d=avg_vol_30d,
                float_shares=fs,
                gap_pct=gap_pct,
                pm_vol_float=pm_vol_float,
                relvol=relvol,
                score=score,
            )
        )

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates
