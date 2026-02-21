"""
Pure Signal Gap Backtester (Test 1).

This is a minimal backtest to isolate the gap size as the independent variable.
No filters - just pure gap continuation signal.

Entry: Buy at 9:35
Exit: Sell at 11:00
Return: (Price at 11:00 - Price at 9:35) / Price at 9:35

Filters (minimal):
- Price > $2
- Dollar volume > $10M
- Long only (gap_up)
- No ETFs
"""

import pandas as pd
import numpy as np
from datetime import date, datetime, time
from typing import List, Dict, Optional
from dataclasses import dataclass


@dataclass
class TradeResult:
    """Result of a single trade."""
    symbol: str
    trade_date: date
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    gap_pct: float
    return_pct: float
    mfe: float  # Max favorable excursion
    mae: float  # Max adverse excursion
    continuation: bool


class PureSignalBacktester:
    """Minimal backtester - pure gap signal only."""
    
    def __init__(self, storage):
        self.storage = storage
        from collector.calendar import TradingCalendar
        self.calendar = TradingCalendar(storage)
    
    def run_backtest(
        self,
        start_date: date,
        end_date: date,
        min_dollar_volume: float = 10_000_000,
        min_price: float = 2.0
    ) -> Dict[str, Dict]:
        """
        Run pure signal backtest for all gap buckets.
        
        Only applies basic tradability filters - no strategy filters.
        """
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
                bucket_name, min_dollar_volume, min_price
            )
            results[bucket_name] = bucket_results
            
            print(f"  Candidates: {bucket_results['total_candidates']}")
            print(f"  Trades: {bucket_results['actual_trades']}")
            print(f"  Win Rate: {bucket_results['win_rate']:.1%}")
            print(f"  Continuation Rate: {bucket_results['continuation_rate']:.1%}")
            print(f"  Avg Return: {bucket_results['avg_return']:.2f}%")
            print(f"  Expectancy: {bucket_results['expectancy']:.2f}%")
        
        return results
    
    def _test_bucket(
        self,
        start_date: date,
        end_date: date,
        min_gap: float,
        max_gap: float,
        bucket_name: str,
        min_dollar_volume: float,
        min_price: float
    ) -> Dict:
        """Test a specific gap bucket with pure signal."""
        
        candidates = self._get_bucket_candidates(
            start_date, end_date, min_gap, max_gap, 
            min_dollar_volume, min_price
        )
        
        total_candidates = len(candidates)
        trades = []
        
        for _, candidate in candidates.iterrows():
            trade_date = candidate['date']
            if isinstance(trade_date, str):
                trade_date = date.fromisoformat(trade_date)
            
            symbol = candidate['symbol']
            
            # Get minute data
            minute_data = self.storage.read_minute_data(trade_date, symbol)
            
            if minute_data is None or minute_data.empty:
                continue
            
            # Execute pure signal trade
            result = self._execute_pure_trade(minute_data, trade_date, symbol, candidate)
            if result:
                trades.append(result)
        
        return self._calculate_stats(trades, total_candidates, start_date, end_date)
    
    def _get_bucket_candidates(
        self,
        start_date: date,
        end_date: date,
        min_gap: float,
        max_gap: float,
        min_dollar_volume: float,
        min_price: float
    ) -> pd.DataFrame:
        """Get candidates with only basic tradability filters."""
        
        daily_data = self.storage.read_meta("daily_bars_grouped.parquet")
        
        if daily_data is None:
            return pd.DataFrame()
        
        # Filter by date range
        start_str = start_date.isoformat()
        end_str = end_date.isoformat()
        mask = (daily_data['date'].astype(str) >= start_str) & (daily_data['date'].astype(str) <= end_str)
        filtered = daily_data[mask]
        
        # Basic price filter
        if 'close' in filtered.columns:
            filtered = filtered[filtered['close'] >= min_price]
        
        # Dollar volume filter
        if 'avg_dollar_volume_20d' in filtered.columns:
            filtered = filtered[filtered['avg_dollar_volume_20d'] >= min_dollar_volume]
        
        # Gap range filter
        gap_mask = (filtered['gap_magnitude'] >= min_gap) & (filtered['gap_magnitude'] < max_gap)
        filtered = filtered[gap_mask]
        
        # Long only (gap up)
        if 'gap_pct' in filtered.columns:
            filtered = filtered[filtered['gap_pct'] > 0]
        
        # Exclude ETFs (simple heuristic - symbols starting with common ETF prefixes)
        if 'symbol' in filtered.columns:
            etf_prefixes = ['SPY', 'QQQ', 'IWM', 'DIA', 'XLF', 'XLE', 'XLK', 'XLV', 'XLI', 'XLP', 
                           'XLY', 'XLC', 'XLB', 'XLRE', 'XLU', 'TLT', 'GLD', 'SLV', 'USO', 'UNG']
            filtered = filtered[~filtered['symbol'].str.startswith(tuple(etf_prefixes), na=False)]
        
        return filtered
    
    def _execute_pure_trade(
        self,
        minute_data: pd.DataFrame,
        trade_date: date,
        symbol: str,
        candidate: pd.Series
    ) -> Optional[TradeResult]:
        """
        Execute pure signal trade:
        - Entry at 9:35 (first bar after 9:35)
        - Exit at 11:00 (or last available)
        """
        
        # Find entry at 9:35
        entry_time = datetime.combine(trade_date, time(9, 35))
        
        # Find first bar at or after 9:35
        entry_bars = minute_data[minute_data['timestamp'] >= entry_time]
        
        if entry_bars.empty:
            # Try 9:36 if no 9:35 bar
            entry_time = datetime.combine(trade_date, time(9, 36))
            entry_bars = minute_data[minute_data['timestamp'] >= entry_time]
        
        if entry_bars.empty:
            return None
        
        # Use first available bar after 9:35
        entry_bar = entry_bars.iloc[0]
        entry_price = entry_bar['open']
        entry_time = entry_bar['timestamp']
        
        # Find exit at 11:00
        exit_time = datetime.combine(trade_date, time(11, 0))
        
        # Get bars up to and including 11:00
        exit_bars = minute_data[(minute_data['timestamp'] >= entry_time) & 
                                (minute_data['timestamp'] <= exit_time)]
        
        if exit_bars.empty:
            # Use last available bar if no 11:00 bar
            post_entry = minute_data[minute_data['timestamp'] > entry_time]
            if post_entry.empty:
                return None
            exit_bar = post_entry.iloc[-1]
        else:
            exit_bar = exit_bars.iloc[-1]
        
        exit_price = exit_bar['close']
        exit_time = exit_bar['timestamp']
        
        # Calculate return: (exit - entry) / entry
        return_pct = (exit_price - entry_price) / entry_price * 100
        
        # Calculate MFE/MAE for the trade window
        trade_window = minute_data[(minute_data['timestamp'] >= entry_time) & 
                                   (minute_data['timestamp'] <= exit_time)]
        
        if trade_window.empty:
            mfe = 0.0
            mae = 0.0
        else:
            mfe = (trade_window['high'].max() - entry_price) / entry_price * 100
            mae = (entry_price - trade_window['low'].min()) / entry_price * 100
        
        continuation = return_pct > 0
        
        return TradeResult(
            symbol=symbol,
            trade_date=trade_date,
            entry_time=entry_time,
            entry_price=entry_price,
            exit_time=exit_time,
            exit_price=exit_price,
            gap_pct=candidate.get('gap_pct', 0) * 100,
            return_pct=return_pct,
            mfe=mfe,
            mae=mae,
            continuation=continuation
        )
    
    def _calculate_stats(
        self,
        trades: List[TradeResult],
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
                'continuation_rate': 0.0,
                'avg_return': 0.0,
                'median_return': 0.0,
                'avg_win': 0.0,
                'avg_loss': 0.0,
                'expectancy': 0.0,
                'avg_mfe': 0.0,
                'avg_mae': 0.0,
                'std_dev': 0.0,
                'trades': []
            }
        
        returns = [t.return_pct for t in trades]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r < 0]
        
        win_rate = len(wins) / len(trades) if trades else 0.0
        continuation_rate = sum(1 for t in trades if t.continuation) / len(trades)
        avg_win = np.mean(wins) if wins else 0.0
        avg_loss = np.mean(losses) if losses else 0.0
        
        loss_rate = 1 - win_rate
        expectancy = (win_rate * avg_win) - (loss_rate * abs(avg_loss))
        
        return {
            'total_candidates': total_candidates,
            'actual_trades': len(trades),
            'opportunity_rate': len(trades) / total_candidates if total_candidates > 0 else 0.0,
            'trading_days': trading_days,
            'trades_per_day': len(trades) / trading_days if trading_days > 0 else 0.0,
            'win_rate': win_rate,
            'continuation_rate': continuation_rate,
            'avg_return': np.mean(returns),
            'median_return': np.median(returns),
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'expectancy': expectancy,
            'avg_mfe': np.mean([t.mfe for t in trades]),
            'avg_mae': np.mean([t.mae for t in trades]),
            'std_dev': np.std(returns),
            'max_return': np.max(returns),
            'p95_return': np.percentile(returns, 95),
            'p99_return': np.percentile(returns, 99),
            'trimmed_mean_5pct': np.mean(returns[(returns <= np.percentile(returns, 95)) & (returns >= np.percentile(returns, 5))]),
            'trades': trades
        }
    
    def print_summary(self, results: Dict[str, Dict]):
        """Print comprehensive backtest summary."""
        
        print("\n" + "=" * 80)
        print("PURE SIGNAL GAP BACKTEST (Test 1)")
        print("Entry: 9:35 | Exit: 11:00 | No Strategy Filters")
        print("=" * 80)
        
        # Summary table
        print(f"\n{'Bucket':<8} {'Cand.':<7} {'Trades':<7} {'Win%':<8} {'Cont.%':<8} {'Avg%':<10} {'Med%':<10} {'Exp%':<10}")
        print("-" * 80)
        
        for bucket_name, result in results.items():
            print(f"{bucket_name:<8} {result['total_candidates']:<7} {result['actual_trades']:<7} "
                  f"{result['win_rate']:<8.1%} {result['continuation_rate']:<8.1%} "
                  f"{result['avg_return']:<10.2f} {result['median_return']:<10.2f} {result['expectancy']:<10.2f}")
        
        print()
        
        # Monotonicity test
        print("CONTINUATION RATE BY GAP SIZE:")
        print("-" * 40)
        continuation_rates = []
        bucket_names = []
        
        for bucket_name, result in results.items():
            rate = result['continuation_rate']
            continuation_rates.append(rate)
            bucket_names.append(bucket_name)
            print(f"  {bucket_name}: {rate:.1%}")
        
        # Check if continuation increases with gap size
        is_increasing = all(continuation_rates[i] <= continuation_rates[i+1] 
                          for i in range(len(continuation_rates)-1))
        is_decreasing = all(continuation_rates[i] >= continuation_rates[i+1] 
                          for i in range(len(continuation_rates)-1))
        
        print()
        if is_increasing:
            print("✅ CONTINUATION INCREASES WITH GAP SIZE")
            print("   → Larger gaps = higher probability of continuation")
            print("   → Consider: Long larger gap-ups")
        elif is_decreasing:
            print("❌ CONTINUATION DECREASES WITH GAP SIZE")
            print("   → Larger gaps = LOWER probability of continuation")
            print("   → Consider: Short larger gap-ups OR wait for reclaim")
        else:
            print("⚠️  NO MONOTONIC RELATIONSHIP")
            print("   → Gap size alone doesn't predict continuation")
        
        # Detailed analysis
        print("\nDETAILED STATISTICS:")
        print("-" * 50)
        
        for bucket_name, result in results.items():
            print(f"\n{bucket_name}:")
            print(f"  Candidates: {result['total_candidates']}")
            print(f"  Trades: {result['actual_trades']}")
            print(f"  Trades/Day: {result['trades_per_day']:.1f}")
            print(f"  Win Rate: {result['win_rate']:.1%}")
            print(f"  Continuation Rate: {result['continuation_rate']:.1%}")
            print(f"  Avg Return: {result['avg_return']:.2f}%")
            print(f"  Median Return: {result['median_return']:.2f}%")
            print(f"  Std Dev: {result['std_dev']:.2f}%")
            print(f"  Max Return: {result['max_return']:.2f}%")
            print(f"  95th Percentile: {result['p95_return']:.2f}%")
            print(f"  99th Percentile: {result['p99_return']:.2f}%")
            print(f"  Trimmed Mean (5%): {result['trimmed_mean_5pct']:.2f}%")
            print(f"  Avg Win: {result['avg_win']:.2f}%")
            print(f"  Avg Loss: {result['avg_loss']:.2f}%")
            print(f"  Expectancy: {result['expectancy']:.2f}%")
            print(f"  Avg MFE: {result['avg_mfe']:.2f}%")
            print(f"  Avg MAE: {result['avg_mae']:.2f}%")
        
        # Key insight
        print("\n" + "=" * 80)
        print("KEY INSIGHT:")
        print("=" * 80)
        
        # Calculate edge for largest gap bucket
        largest_bucket = results.get("15%+")
        if largest_bucket and largest_bucket['actual_trades'] > 0:
            print(f"\n15%+ gap bucket:")
            print(f"  - {largest_bucket['actual_trades']} trades")
            print(f"  - {largest_bucket['continuation_rate']:.1%} continuation rate")
            print(f"  - {largest_bucket['avg_return']:.2f}% average return")
            
            if largest_bucket['continuation_rate'] > 0.55:
                print("\n→ STRONG SIGNAL: Large gap-ups tend to continue")
                print("  Consider: Trade 10%+ gaps with simple entry/exit")
            elif largest_bucket['continuation_rate'] < 0.45:
                print("\n→ REVERSE SIGNAL: Large gap-ups tend to fade")
                print("  Consider: Short 10%+ gaps OR wait for reclaim entries")
            else:
                print("\n→ WEAK SIGNAL: Large gap-ups are random")
                print("  Consider: Add filters or try fade strategy")
