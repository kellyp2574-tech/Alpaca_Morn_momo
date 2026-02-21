"""
Strategy simulator that replicates the live bot's entry and exit logic.

This module simulates the full trading strategy including:
- Entry window (09:33-10:30)
- ATR-based stops
- Volume filter
- VWAP filter
- Breakout extension filter
- Spread filter
- Trailing stops
- Breakeven stop
- Dead momentum exit
- Hard exit at 11:00
"""

import pandas as pd
import numpy as np
from datetime import date, datetime, time
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from collections import deque


@dataclass
class SimBar:
    """Simplified bar for backtesting."""
    timestamp: datetime
    o: float
    h: float
    l: float
    c: float
    v: int


@dataclass
class SimQuote:
    """Simplified quote for backtesting."""
    bid_price: float
    ask_price: float


@dataclass
class SimTradeResult:
    """Result of a simulated trade."""
    symbol: str
    trade_date: date
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str  # stop, trailing, breakeven, dead_momo, hard_exit
    gap_pct: float
    return_pct: float
    mfe: float  # Maximum Favorable Excursion
    mae: float  # Maximum Adverse Excursion
    r_multiples: float  # Return in terms of R (risk)
    stopped: bool


class StrategySimulator:
    """Simulates the full trading strategy with all filters and exits."""
    
    def __init__(self, cfg=None):
        if cfg is None:
            from bot.config import Config
            cfg = Config()
        self.cfg = cfg
        
    def simulate_trade(
        self,
        minute_data: pd.DataFrame,
        candidate: pd.Series,
        trade_date: date,
        symbol: str
    ) -> Optional[SimTradeResult]:
        """
        Simulate a single trade with full strategy logic.
        
        Returns None if the candidate doesn't pass entry filters.
        """
        if minute_data is None or minute_data.empty:
            return None
        
        # Convert to SimBar objects
        bars = self._convert_to_bars(minute_data)
        if not bars:
            return None
        
        # Filter to RTH bars (9:30 - 11:00)
        rth_bars = [b for b in bars if b.timestamp.time() >= time(9, 30) and b.timestamp.time() <= time(11, 0)]
        
        # Find entry window bars (09:33 - 10:30)
        entry_window_start = datetime.combine(trade_date, time(9, 33))
        entry_window_end = datetime.combine(trade_date, time(10, 30))
        
        entry_candidates = [b for b in rth_bars if entry_window_start <= b.timestamp <= entry_window_end]
        
        if not entry_candidates:
            return None
        
        # Get premarket data for candidate validation
        pm_high = candidate.get('pm_high', candidate.get('high', 0))
        
        # Simulate entry for each bar in entry window
        for bar in entry_candidates:
            entry_result = self._check_entry(bar, rth_bars, pm_high)
            if entry_result is not None:
                entry_price, stop_pct, atr = entry_result
                
                # Run the trade simulation
                result = self._simulate_position(
                    bars, bar, entry_price, stop_pct, atr, 
                    trade_date, symbol, candidate.get('gap_pct', 0)
                )
                return result
        
        return None
    
    def _convert_to_bars(self, df: pd.DataFrame) -> List[SimBar]:
        """Convert DataFrame to SimBar objects."""
        bars = []
        for _, row in df.iterrows():
            ts = row.get('timestamp')
            if ts is None:
                continue
            if isinstance(ts, str):
                ts = pd.to_datetime(ts)
            
            bars.append(SimBar(
                timestamp=ts,
                o=row.get('open', 0),
                h=row.get('high', 0),
                l=row.get('low', 0),
                c=row.get('close', 0),
                v=row.get('volume', 0)
            ))
        return sorted(bars, key=lambda x: x.timestamp)
    
    def _check_entry(
        self, 
        bar: SimBar, 
        rth_bars: List[SimBar], 
        pm_high: float
    ) -> Optional[Tuple[float, float, float]]:
        """Check if entry criteria are met. Returns (entry_price, stop_pct, atr) or None."""
        cfg = self.cfg
        
        # Get bars up to and including current bar
        bars_up_to_now = [b for b in rth_bars if b.timestamp <= bar.timestamp]
        
        min_len = max(cfg.atr_len + 1, cfg.volume_avg_window + 1)
        if len(bars_up_to_now) < min_len:
            return None
        
        # Calculate ATR
        atr = self._calculate_atr(bars_up_to_now, cfg.atr_len)
        if atr <= 0 or atr < cfg.min_atr_dollars:
            return None
        
        # Calculate VWAP
        vwap = self._calculate_vwap(bars_up_to_now)
        if vwap <= 0:
            return None
        
        # Price must be above VWAP
        if bar.c <= vwap:
            return None
        
        # Price must be above premarket high
        if pm_high > 0 and bar.c <= pm_high:
            return None
        
        # Price must not have extended too far past pm_high
        if pm_high > 0 and bar.c > pm_high * (1 + cfg.max_breakout_extension_pct):
            return None
        
        # Volume filter
        vol_window = cfg.volume_avg_window
        if vol_window > 0 and len(bars_up_to_now) >= vol_window + 1:
            recent = bars_up_to_now[-(vol_window + 1):-1]
            prev_vols = [b.v for b in recent]
            if len(prev_vols) >= vol_window:
                avg_vol = sum(prev_vols) / vol_window
                if avg_vol > 0 and bar.v < cfg.volume_spike_mult * avg_vol:
                    return None
        
        # Minimum volume check
        if bar.v < cfg.min_1m_volume:
            return None
        
        # Minimum dollar volume check
        if bar.v * bar.c < cfg.min_1m_dollar_volume:
            return None
        
        # Simulate spread (using typical spread estimation)
        # In real backtest, we'd use actual bid/ask if available
        spread = bar.c * 0.001  # Assume 0.1% spread as default
        max_spread = max(cfg.max_spread_dollars, cfg.max_spread_pct * bar.c)
        if spread > max_spread:
            return None
        
        # Calculate stop percentage
        stop_pct = self._calculate_stop_pct(cfg, atr, bar.c)
        
        return (bar.c, stop_pct, atr)
    
    def _calculate_atr(self, bars: List[SimBar], n: int) -> float:
        """Calculate Average True Range."""
        if len(bars) < n + 1:
            return 0.0
        
        trs = []
        for i in range(-n, 0):
            bar = bars[i]
            prev = bars[i - 1]
            tr = max(
                bar.h - bar.l,
                abs(bar.h - prev.c),
                abs(bar.l - prev.c)
            )
            trs.append(tr)
        
        return sum(trs) / n if trs else 0.0
    
    def _calculate_vwap(self, bars: List[SimBar]) -> float:
        """Calculate Volume Weighted Average Price."""
        pv = 0.0
        v = 0.0
        for bar in bars:
            tp = (bar.h + bar.l + bar.c) / 3.0
            pv += tp * bar.v
            v += bar.v
        return pv / v if v > 0 else 0.0
    
    def _calculate_stop_pct(self, cfg, atr: float, entry_price: float) -> float:
        """Calculate initial stop percentage."""
        stop_pct = (atr / entry_price) * cfg.stop_atr_mult
        return max(cfg.stop_min_pct, min(stop_pct, cfg.stop_max_pct))
    
    def _simulate_position(
        self,
        bars: List[SimBar],
        entry_bar: SimBar,
        entry_price: float,
        stop_pct: float,
        atr: float,
        trade_date: date,
        symbol: str,
        gap_pct: float
    ) -> SimTradeResult:
        """Simulate the position from entry to exit."""
        cfg = self.cfg
        
        entry_time = entry_bar.timestamp
        stop_price = entry_price * (1 - stop_pct)
        
        # Track state
        peak_price = entry_price
        breakeven_set = False
        trail_active = False
        trail_pct = cfg.trail_pct_1
        
        # Track MFE/MAE
        max_favorable = 0.0
        max_adverse = 0.0
        
        # Hard exit time
        hard_exit_time = datetime.combine(trade_date, time(11, 0))
        
        # Find bars after entry
        post_entry_bars = [b for b in bars if b.timestamp > entry_time]
        
        exit_price = None
        exit_time = None
        exit_reason = None
        
        for bar in post_entry_bars:
            # Update peak
            peak_price = max(peak_price, bar.h)
            
            # Calculate current return
            current_return = (bar.c - entry_price) / entry_price
            
            # Update MFE/MAE
            mfe = (bar.h - entry_price) / entry_price
            mae = (entry_price - bar.l) / entry_price
            max_favorable = max(max_favorable, mfe)
            max_adverse = max(max_adverse, mae)
            
            # Check breakeven
            if not breakeven_set and peak_price >= entry_price * (1 + cfg.breakeven_at_pct):
                stop_price = max(stop_price, entry_price)
                breakeven_set = True
            
            # Check trailing activation
            if not trail_active and peak_price >= entry_price * (1 + cfg.trail_activate_at_pct):
                trail_active = True
                trail_pct = cfg.trail_pct_1
            
            # Update trailing stop
            if trail_active:
                if peak_price >= entry_price * (1 + cfg.trail_widen_at_pct):
                    trail_pct = cfg.trail_pct_2
                trail_stop = peak_price * (1 - trail_pct)
                stop_price = max(stop_price, trail_stop)
            
            # Check dead momentum exit
            elapsed = (bar.timestamp - entry_time).total_seconds() / 60.0
            if elapsed >= cfg.dead_momo_minutes and current_return < cfg.dead_momo_min_gain:
                exit_price = bar.c
                exit_time = bar.timestamp
                exit_reason = "dead_momo"
                break
            
            # Check stop hit
            if bar.l <= stop_price:
                exit_price = stop_price  # Fill at stop price
                exit_time = bar.timestamp
                exit_reason = "stop"
                break
            
            # Check hard exit at 11:00
            if bar.timestamp >= hard_exit_time:
                exit_price = bar.c
                exit_time = bar.timestamp
                exit_reason = "hard_exit"
                break
        
        # If no exit found, use last bar
        if exit_price is None and post_entry_bars:
            last_bar = post_entry_bars[-1]
            exit_price = last_bar.c
            exit_time = last_bar.timestamp
            exit_reason = "hard_exit"
        
        # Calculate return
        return_pct = (exit_price - entry_price) / entry_price * 100
        r_multiples = return_pct / (stop_pct * 100) if stop_pct > 0 else 0.0
        
        return SimTradeResult(
            symbol=symbol,
            trade_date=trade_date,
            entry_time=entry_time,
            entry_price=entry_price,
            exit_time=exit_time,
            exit_price=exit_price,
            exit_reason=exit_reason,
            gap_pct=gap_pct,
            return_pct=return_pct,
            mfe=max_favorable * 100,
            mae=max_adverse * 100,
            r_multiples=r_multiples,
            stopped=exit_reason == "stop"
        )


class GapStrategyBacktester:
    """Backtests gap buckets with full strategy simulation."""
    
    def __init__(self, storage, cfg=None):
        self.storage = storage
        self.simulator = StrategySimulator(cfg)
        from collector.calendar import TradingCalendar
        self.calendar = TradingCalendar(storage)
    
    def run_backtest(
        self,
        start_date: date,
        end_date: date,
        min_dollar_volume: float = 10_000_000
    ) -> Dict[str, Dict]:
        """Run backtest for all gap buckets."""
        
        # Use existing bucket definitions
        buckets = [
            ("3-5%", (0.03, 0.05)),
            ("5-7%", (0.05, 0.07)),
            ("7-10%", (0.07, 0.10)),
            ("10-15%", (0.10, 0.15)),
            ("15%+", (0.15, float('inf')))
        ]
        
        results = {}
        
        for bucket_name, (min_gap, max_gap) in buckets:
            print(f"\nTesting bucket: {bucket_name}")
            bucket_results = self._test_bucket(
                start_date, end_date, min_gap, max_gap, 
                bucket_name, min_dollar_volume
            )
            results[bucket_name] = bucket_results
            
            # Print summary
            print(f"  Candidates: {bucket_results['total_candidates']}")
            print(f"  Trades: {bucket_results['actual_trades']}")
            print(f"  Win Rate: {bucket_results['win_rate']:.1%}")
            print(f"  Expectancy: {bucket_results['expectancy']:.2%}")
            print(f"  Avg R: {bucket_results['avg_r']:.2f}")
        
        return results
    
    def _test_bucket(
        self,
        start_date: date,
        end_date: date,
        min_gap: float,
        max_gap: float,
        bucket_name: str,
        min_dollar_volume: float
    ) -> Dict:
        """Test a specific gap bucket."""
        
        # Get candidates for this bucket
        candidates = self._get_bucket_candidates(
            start_date, end_date, min_gap, max_gap, min_dollar_volume
        )
        
        total_candidates = len(candidates)
        trades = []
        
        for _, candidate in candidates.iterrows():
            # Convert date to date object if string
            trade_date = candidate['date']
            if isinstance(trade_date, str):
                trade_date = date.fromisoformat(trade_date)
            
            symbol = candidate['symbol']
            
            # Get minute data
            minute_data = self.storage.read_minute_data(trade_date, symbol)
            
            if minute_data is None or minute_data.empty:
                continue
            
            # Simulate trade
            result = self.simulator.simulate_trade(
                minute_data, candidate, trade_date, symbol
            )
            
            if result:
                trades.append(result)
        
        return self._calculate_stats(trades, total_candidates, start_date, end_date)
    
    def _get_bucket_candidates(
        self,
        start_date: date,
        end_date: date,
        min_gap: float,
        max_gap: float,
        min_dollar_volume: float
    ) -> pd.DataFrame:
        """Get candidates for specific gap bucket."""
        
        daily_data = self.storage.read_meta("daily_bars_grouped.parquet")
        
        if daily_data is None:
            return pd.DataFrame()
        
        # Filter by date range
        start_str = start_date.isoformat()
        end_str = end_date.isoformat()
        mask = (daily_data['date'].astype(str) >= start_str) & (daily_data['date'].astype(str) <= end_str)
        filtered = daily_data[mask]
        
        # Filter by tradeability
        if 'is_tradeable' in filtered.columns:
            filtered = filtered[filtered['is_tradeable']]
        
        # Filter by gap range
        gap_mask = (filtered['gap_magnitude'] >= min_gap) & (filtered['gap_magnitude'] < max_gap)
        filtered = filtered[gap_mask]
        
        # Filter by dollar volume
        if 'avg_dollar_volume_20d' in filtered.columns:
            volume_mask = filtered['avg_dollar_volume_20d'] >= min_dollar_volume
            filtered = filtered[volume_mask]
        
        return filtered
    
    def _calculate_stats(
        self,
        trades: List[SimTradeResult],
        total_candidates: int,
        start_date: date,
        end_date: date
    ) -> Dict:
        """Calculate statistics for a bucket."""
        
        trading_days = len(list(self.calendar.get_trading_days(start_date, end_date)))
        
        if not trades:
            return {
                'total_candidates': total_candidates,
                'actual_trades': 0,
                'opportunity_rate': 0.0,
                'trading_days': trading_days,
                'trades_per_day': 0.0,
                'win_rate': 0.0,
                'avg_win': 0.0,
                'avg_loss': 0.0,
                'expectancy': 0.0,
                'avg_r': 0.0,
                'max_dd': 0.0,
                'avg_mfe': 0.0,
                'avg_mae': 0.0,
                'stop_rate': 0.0,
                'trades': []
            }
        
        returns = [t.return_pct for t in trades]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r < 0]
        
        win_rate = len(wins) / len(trades) if trades else 0.0
        avg_win = np.mean(wins) if wins else 0.0
        avg_loss = np.mean(losses) if losses else 0.0
        
        loss_rate = 1 - win_rate
        expectancy = (win_rate * avg_win) - (loss_rate * abs(avg_loss))
        
        r_values = [t.r_multiples for t in trades]
        avg_r = np.mean(r_values)
        
        # Calculate max drawdown
        cumulative = np.cumsum([0] + returns)
        running_max = np.maximum.accumulate(cumulative)
        drawdown = running_max - cumulative
        max_dd = np.max(drawdown) if len(drawdown) > 0 else 0.0
        
        # Exit reason breakdown
        exit_reasons = {}
        for t in trades:
            reason = t.exit_reason
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        
        stop_rate = sum(1 for t in trades if t.stopped) / len(trades)
        
        return {
            'total_candidates': total_candidates,
            'actual_trades': len(trades),
            'opportunity_rate': len(trades) / total_candidates if total_candidates > 0 else 0.0,
            'trading_days': trading_days,
            'trades_per_day': len(trades) / trading_days if trading_days > 0 else 0.0,
            'win_rate': win_rate,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'expectancy': expectancy,
            'avg_r': avg_r,
            'max_dd': max_dd,
            'avg_mfe': np.mean([t.mfe for t in trades]),
            'avg_mae': np.mean([t.mae for t in trades]),
            'stop_rate': stop_rate,
            'exit_reasons': exit_reasons,
            'trades': trades
        }
    
    def print_summary(self, results: Dict[str, Dict]):
        """Print comprehensive backtest summary."""
        
        print("\n" + "=" * 80)
        print("GAP STRATEGY BACKTEST RESULTS (Full Bot Simulation)")
        print("=" * 80)
        
        # Summary table
        print(f"\n{'Bucket':<8} {'Candidates':<10} {'Trades':<8} {'Win Rate':<10} {'Expectancy':<12} {'Avg R':<8} {'Max DD':<10}")
        print("-" * 80)
        
        for bucket_name, result in results.items():
            print(f"{bucket_name:<8} {result['total_candidates']:<10} {result['actual_trades']:<8} "
                  f"{result['win_rate']:<10.1%} {result['expectancy']:<12.2f} {result['avg_r']:<8.2f} {result['max_dd']:<10.2f}")
        
        print()
        
        # Detailed analysis
        print("DETAILED ANALYSIS:")
        print("-" * 50)
        
        for bucket_name, result in results.items():
            print(f"\n{bucket_name} Gap Bucket:")
            print(f"  Candidates: {result['total_candidates']}")
            print(f"  Trades Executed: {result['actual_trades']}")
            print(f"  Opportunity Rate: {result['opportunity_rate']:.1%}")
            print(f"  Trades/Day: {result['trades_per_day']:.1f}")
            print(f"  Win Rate: {result['win_rate']:.1%}")
            print(f"  Avg Win: {result['avg_win']:.2f}%")
            print(f"  Avg Loss: {result['avg_loss']:.2f}%")
            print(f"  Expectancy: {result['expectancy']:.2f}%")
            print(f"  Avg R: {result['avg_r']:.2f}")
            print(f"  Max Drawdown: {result['max_dd']:.2f}%")
            print(f"  Avg MFE: {result['avg_mfe']:.2f}%")
            print(f"  Avg MAE: {result['avg_mae']:.2f}%")
            print(f"  Stop Rate: {result['stop_rate']:.1%}")
            
            if result.get('exit_reasons'):
                print(f"  Exit Reasons:")
                for reason, count in result['exit_reasons'].items():
                    print(f"    {reason}: {count}")
        
        print()
        
        # Best bucket analysis
        if results:
            best_expectancy = max(results.items(), key=lambda x: x[1]['expectancy'])
            best_r = max(results.items(), key=lambda x: x[1]['avg_r'])
            best_win_rate = max(results.items(), key=lambda x: x[1]['win_rate'])
            
            print("BEST PERFORMERS:")
            print("-" * 20)
            print(f"Best Expectancy: {best_expectancy[0]} ({best_expectancy[1]['expectancy']:.2f}%)")
            print(f"Best Avg R: {best_r[0]} ({best_r[1]['avg_r']:.2f})")
            print(f"Best Win Rate: {best_win_rate[0]} ({best_win_rate[1]['win_rate']:.1%})")
