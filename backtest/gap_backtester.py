"""
Gap continuation backtester.

Tests the hypothesis that larger gaps have higher continuation probability
using simple entry/exit rules without additional filters.
"""

import pandas as pd
import numpy as np
from datetime import date, time, datetime, timedelta
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from collector.storage import DataLakeStorage


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
    gap_direction: str
    return_pct: float
    mfe: float  # Maximum Favorable Excursion
    mae: float  # Maximum Adverse Excursion
    continuation: bool  # True if exit > entry


@dataclass
class BucketResults:
    """Results for a gap bucket."""
    bucket_name: str
    gap_range: Tuple[float, float]
    total_candidates: int  # Total candidates in this bucket
    actual_trades: int  # Trades that actually executed
    opportunity_rate: float  # actual_trades / total_candidates
    trading_days: int  # Number of trading days in period
    candidates_per_day: float  # total_candidates / trading_days
    trades_per_day: float  # actual_trades / trading_days
    expectancy_per_trade: float  # Standard expectancy
    expectancy_per_day: float  # trades_per_day * expectancy_per_trade
    win_rate: float
    avg_win: float
    avg_loss: float
    expectancy: float
    median_return: float
    std_dev: float
    avg_mfe: float
    avg_mae: float
    continuation_rate: float
    trades: List[TradeResult]


class GapBacktester:
    """Backtests gap continuation strategies with simple rules."""
    
    def __init__(self, storage: DataLakeStorage):
        self.storage = storage
        # Add calendar for trading days calculation
        from collector.calendar import TradingCalendar
        self.calendar = TradingCalendar(storage)
    
    def run_backtest(self, 
                    start_date: date,
                    end_date: date,
                    min_dollar_volume: float = 10_000_000) -> Dict[str, BucketResults]:
        """
        Run backtest for all gap buckets.
        
        Args:
            start_date: Backtest start date
            end_date: Backtest end date
            min_dollar_volume: Minimum dollar volume filter
        
        Returns:
            Dictionary mapping bucket names to results
        """
        # Define gap buckets
        buckets = [
            ("3-5%", (0.03, 0.05)),
            ("5-7%", (0.05, 0.07)),
            ("7-10%", (0.07, 0.10)),
            ("10-15%", (0.10, 0.15)),
            ("15%+", (0.15, float('inf')))
        ]
        
        results = {}
        
        for bucket_name, (min_gap, max_gap) in buckets:
            print(f"Testing bucket: {bucket_name}")
            bucket_results = self._test_bucket(
                start_date, end_date, min_gap, max_gap, 
                bucket_name, min_dollar_volume
            )
            results[bucket_name] = bucket_results
            
            # Print summary
            print(f"  Trades: {bucket_results.actual_trades}")
            print(f"  Win Rate: {bucket_results.win_rate:.1%}")
            print(f"  Continuation Rate: {bucket_results.continuation_rate:.1%}")
            print(f"  Expectancy: {bucket_results.expectancy:.2%}")
            print()
        
        return results
    
    def _test_bucket(self, 
                    start_date: date,
                    end_date: date,
                    min_gap: float,
                    max_gap: float,
                    bucket_name: str,
                    min_dollar_volume: float) -> BucketResults:
        """Test a specific gap bucket."""
        
        # Get candidates for this bucket
        candidates = self._get_bucket_candidates(
            start_date, end_date, min_gap, max_gap, min_dollar_volume
        )
        
        total_candidates = len(candidates)
        trades = []
        
        for _, candidate in candidates.iterrows():
            trade_result = self._execute_trade(candidate)
            if trade_result:
                trades.append(trade_result)
        
        actual_trades = len(trades)
        opportunity_rate = actual_trades / total_candidates if total_candidates > 0 else 0.0
        
        # Calculate trading days
        trading_days = self.calendar.get_trading_days(start_date, end_date).__len__()
        candidates_per_day = total_candidates / trading_days if trading_days > 0 else 0.0
        trades_per_day = actual_trades / trading_days if trading_days > 0 else 0.0
        
        # Calculate bucket statistics
        return self._calculate_bucket_stats(
            bucket_name, (min_gap, max_gap), trades, total_candidates, actual_trades, opportunity_rate,
            trading_days, candidates_per_day, trades_per_day
        )
    
    def _get_bucket_candidates(self, 
                             start_date: date,
                             end_date: date,
                             min_gap: float,
                             max_gap: float,
                             min_dollar_volume: float) -> pd.DataFrame:
        """Get candidates for specific gap bucket."""
        
        # Load real daily data instead of cached candidates
        daily_data = self.storage.read_meta("daily_bars_grouped.parquet")
        
        if daily_data is None:
            return pd.DataFrame()
        
        # Filter by date range
        start_str = start_date.isoformat()
        end_str = end_date.isoformat()
        mask = (daily_data['date'].astype(str) >= start_str) & (daily_data['date'].astype(str) <= end_str)
        filtered = daily_data[mask]
        
        # Filter by tradeability
        filtered = filtered[filtered['is_tradeable']]
        
        # Filter by gap range
        gap_mask = (filtered['gap_magnitude'] >= min_gap) & (filtered['gap_magnitude'] < max_gap)
        filtered = filtered[gap_mask]
        
        # Add gap_direction if missing
        if 'gap_direction' not in filtered.columns:
            filtered['gap_direction'] = filtered.apply(lambda row: 'gap_up' if row['gap_pct'] > 0 else 'gap_down', axis=1)
        
        # Filter by dollar volume
        volume_mask = filtered['avg_dollar_volume_20d'] >= min_dollar_volume
        filtered = filtered[volume_mask]
        
        return filtered
    
    def _execute_trade(self, candidate: pd.Series) -> Optional[TradeResult]:
        """Execute a single trade according to strategy rules."""
        
        # Convert date to date object if it's a string
        trade_date = candidate['date']
        if isinstance(trade_date, str):
            trade_date = date.fromisoformat(trade_date)
        
        symbol = candidate['symbol']
        
        print(f" Executing trade for {symbol} on {trade_date}")
        
        # Get minute data
        minute_data = self.storage.read_minute_data(trade_date, symbol)
        
        if minute_data is None or minute_data.empty:
            print(f" No minute data for {symbol} on {trade_date}")
            return None
        
        print(f" Found {len(minute_data)} minute bars for {symbol}")
        
        # Find entry time: first minute after 9:33
        entry_time = self._find_entry_time(minute_data)
        
        if entry_time is None:
            print(f" No entry time found for {symbol} on {trade_date}")
            return None
        
        print(f" Entry time found: {entry_time}")
        
        # Get entry price (open of entry minute)
        entry_row = minute_data[minute_data['timestamp'] == entry_time]
        if entry_row.empty:
            print(f" No entry row found for {symbol} at {entry_time}")
            return None
        
        entry_price = entry_row.iloc[0]['open']
        print(f" Entry price: ${entry_price:.2f}")
        
        # Find exit time: 11:00 or last available minute
        exit_time = self._find_exit_time(minute_data, entry_time)
        
        if exit_time is None:
            print(f" No exit time found for {symbol} on {trade_date}")
            return None
        
        print(f" Exit time found: {exit_time}")
        
        # Get exit price (close of exit minute)
        exit_row = minute_data[minute_data['timestamp'] == exit_time]
        if exit_row.empty:
            print(f" No exit row found for {symbol} at {exit_time}")
            return None
        
        exit_price = exit_row.iloc[0]['close']
        print(f" Exit price: ${exit_price:.2f}")
        
        # Calculate return
        return_pct = (exit_price - entry_price) / entry_price
        return_pct = return_pct * 100
        
        # Calculate MFE/MAE
        high = minute_data[(minute_data['timestamp'] >= entry_time) & (minute_data['timestamp'] <= exit_time)]
        low = minute_data[(minute_data['timestamp'] >= entry_time) & (minute_data['timestamp'] <= exit_time)]
        
        if high.empty or low.empty:
            print(f" No intraday data for {symbol} between {entry_time} and {exit_time}")
            return None
        
        mfe = (high['high'].max() - entry_price) / entry_price * 100
        mae = (entry_price - low['low'].min()) / entry_price * 100
        
        # Determine continuation
        continuation = 1 if return_pct > 0 else 0
        
        print(f' Return: {return_pct:.2f}%')
        print(f' MFE: {mfe:.2f}%')
        print(f' MAE: {mae:.2f}%')
        print(f' Continuation: {continuation}')
        
        return TradeResult(
            symbol=symbol,
            trade_date=trade_date,
            entry_price=entry_price,
            exit_price=exit_price,
            return_pct=return_pct,
            mfe=mfe,
            mae=mae,
            continuation=continuation
        )
    
    def _find_entry_time(self, minute_data: pd.DataFrame) -> Optional[datetime]:
        """Find first minute after 9:33 or 9:40 if no 9:33 data."""
        
        # Create 9:33 time for the trade date
        trade_date_str = minute_data['trade_date'].iloc[0]
        if isinstance(trade_date_str, str):
            trade_date = date.fromisoformat(trade_date_str)
        else:
            trade_date = trade_date_str
        
        # Try 9:33 first, then 9:40
        entry_time = datetime.combine(trade_date, time(9, 33))
        after_933 = minute_data[minute_data['timestamp'] > entry_time]
        
        if after_933.empty:
            # Try 9:40 instead
            entry_time = datetime.combine(trade_date, time(9, 40))
            after_940 = minute_data[minute_data['timestamp'] > entry_time]
            
            if after_940.empty:
                return None
            
            return after_940.iloc[0]['timestamp']
        
        return after_933.iloc[0]['timestamp']
    
    def _find_exit_time(self, minute_data: pd.DataFrame, entry_time: datetime) -> Optional[datetime]:
        """Find 11:00 exit time or last available minute."""
        
        # Create 11:00 time for the trade date
        trade_date_str = minute_data['trade_date'].iloc[0]
        if isinstance(trade_date_str, str):
            trade_date = date.fromisoformat(trade_date_str)
        else:
            trade_date = trade_date_str
        exit_target = datetime.combine(trade_date, time(11, 0))
        
        # Find minutes at or after 11:00, but after entry time
        after_entry = minute_data[minute_data['timestamp'] > entry_time]
        
        if after_entry.empty:
            return None
        
        # Try to find 11:00 exactly
        exact_exit = after_entry[after_entry['timestamp'] == exit_target]
        if not exact_exit.empty:
            return exact_exit.iloc[0]['timestamp']
        
        # If 11:00 not found, use the last available minute
        return after_entry.iloc[-1]['timestamp']
    
    def _calculate_mfe_mae(self, 
                          minute_data: pd.DataFrame,
                          entry_time: datetime,
                          exit_time: datetime,
                          entry_price: float) -> Tuple[float, float]:
        """Calculate Maximum Favorable Excursion and Maximum Adverse Excursion."""
        
        # Get data between entry and exit
        trade_window = minute_data[
            (minute_data['timestamp'] >= entry_time) & 
            (minute_data['timestamp'] <= exit_time)
        ]
        
        if trade_window.empty:
            return 0.0, 0.0
        
        # Calculate high/low excursions from entry price
        max_high = trade_window['high'].max()
        min_low = trade_window['low'].min()
        
        mfe = (max_high - entry_price) / entry_price
        mae = (entry_price - min_low) / entry_price
        
        return mfe, mae
    
    def _calculate_bucket_stats(self, 
                              bucket_name: str,
                              gap_range: Tuple[float, float],
                              trades: List[TradeResult],
                              total_candidates: int,
                              actual_trades: int,
                              opportunity_rate: float,
                              trading_days: int,
                              candidates_per_day: float,
                              trades_per_day: float) -> BucketResults:
        """Calculate statistics for a bucket."""
        
        if not trades:
            return BucketResults(
                bucket_name=bucket_name,
                gap_range=gap_range,
                total_candidates=total_candidates,
                actual_trades=actual_trades,
                opportunity_rate=opportunity_rate,
                trading_days=trading_days,
                candidates_per_day=candidates_per_day,
                trades_per_day=trades_per_day,
                expectancy_per_trade=0.0,
                expectancy_per_day=0.0,
                win_rate=0.0,
                avg_win=0.0,
                avg_loss=0.0,
                expectancy=0.0,
                median_return=0.0,
                std_dev=0.0,
                avg_mfe=0.0,
                avg_mae=0.0,
                continuation_rate=0.0,
                trades=[]
            )
        
        returns = [trade.return_pct for trade in trades]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r < 0]
        
        win_rate = len(wins) / len(trades) if trades else 0.0
        avg_win = np.mean(wins) if wins else 0.0
        avg_loss = np.mean(losses) if losses else 0.0
        
        # Expectancy = (Win Rate * Avg Win) - (Loss Rate * Avg Loss)
        loss_rate = 1 - win_rate
        expectancy = (win_rate * avg_win) - (loss_rate * abs(avg_loss))
        
        continuation_rate = sum(1 for trade in trades if trade.continuation) / len(trades)
        
        # Calculate expectancy metrics
        expectancy_per_trade = (win_rate * avg_win) - ((1 - win_rate) * abs(avg_loss))
        expectancy_per_day = trades_per_day * expectancy_per_trade
        
        return BucketResults(
            bucket_name=bucket_name,
            gap_range=gap_range,
            total_candidates=total_candidates,
            actual_trades=actual_trades,
            opportunity_rate=opportunity_rate,
            trading_days=trading_days,
            candidates_per_day=candidates_per_day,
            trades_per_day=trades_per_day,
            expectancy_per_trade=expectancy_per_trade,
            expectancy_per_day=expectancy_per_day,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            expectancy=expectancy_per_trade,
            median_return=np.median(returns),
            std_dev=np.std(returns),
            avg_mfe=np.mean([trade.mfe for trade in trades]),
            avg_mae=np.mean([trade.mae for trade in trades]),
            continuation_rate=continuation_rate,
            trades=trades
        )
    
    def print_summary(self, results: Dict[str, BucketResults]):
        """Print comprehensive backtest summary."""
        
        print("=" * 80)
        print("GAP CONTINUATION BACKTEST RESULTS")
        print("=" * 80)
        print()
        
        # Summary table
        print(f"{'Bucket':<8} {'Trades':<8} {'Win Rate':<10} {'Continuation':<12} {'Expectancy':<11} {'Avg MFE':<9} {'Avg MAE':<9}")
        print("-" * 80)
        
        for bucket_name, result in results.items():
            print(f"{bucket_name:<8} {result.actual_trades:<8} "
                  f"{result.win_rate:<10.1%} {result.continuation_rate:<12.1%} "
                  f"{result.expectancy_per_trade:<11.2%} {result.avg_mfe:<9.2%} {result.avg_mae:<9.2%}")
        
        print()
        
        # Detailed analysis
        print("DETAILED ANALYSIS:")
        print("-" * 50)
        
        for bucket_name, result in results.items():
            print(f"\n{bucket_name} Gap Bucket:")
            max_range = f"{result.gap_range[1]:.0%}" if result.gap_range[1] != float('inf') else "∞"
            print(f"  Gap Range: {result.gap_range[0]:.0%}-{max_range}")
            print(f"  Total Candidates: {result.total_candidates}")
            print(f"  Actual Trades: {result.actual_trades}")
            print(f"  Opportunity Rate: {result.opportunity_rate:.1%}")
            print(f"  Trading Days: {result.trading_days}")
            print(f"  Candidates/Day: {result.candidates_per_day:.1f}")
            print(f"  Trades/Day: {result.trades_per_day:.1f}")
            print(f"  Expectancy/Trade: {result.expectancy_per_trade:.2%}")
            print(f"  Expectancy/Day: {result.expectancy_per_day:.2%}")
            print(f"  Win Rate: {result.win_rate:.1%}")
            print(f"  Avg Win: {result.avg_win:.2%}")
            print(f"  Avg Loss: {result.avg_loss:.2%}")
            print(f"  Median Return: {result.median_return:.2%}")
            print(f"  Std Dev: {result.std_dev:.2%}")
            print(f"  Avg MFE: {result.avg_mfe:.2%}")
            print(f"  Avg MAE: {result.avg_mae:.2%}")
            print(f"  Continuation Rate: {result.continuation_rate:.1%}")
        
        print()
        
        # Test for monotonic improvement in continuation
        continuation_rates = [result.continuation_rate for result in results.values()]
        bucket_names = list(results.keys())
        
        print("MONOTONIC CONTINUATION TEST:")
        print("-" * 40)
        
        is_monotonic = all(continuation_rates[i] <= continuation_rates[i+1] 
                          for i in range(len(continuation_rates)-1))
        
        if is_monotonic:
            print("✅ CONTINUATION PROBABILITY INCREASES WITH GAP SIZE")
            print("   Strategy appears to be continuation-biased")
        else:
            print("❌ CONTINUATION PROBABILITY DOES NOT INCREASE MONOTONICALLY")
            print("   Strategy may be fade-biased or more complex")
        
        print(f"\nContinuation Rates by Bucket:")
        for i, (bucket, rate) in enumerate(zip(bucket_names, continuation_rates)):
            trend = "↗" if i > 0 and rate > continuation_rates[i-1] else "↘" if i > 0 and rate < continuation_rates[i-1] else "→"
            print(f"  {bucket}: {rate:.1%} {trend}")
        
        print()
        
        # Recommendation
        best_expectancy = max(results.items(), key=lambda x: x[1].expectancy_per_trade)
        best_continuation = max(results.items(), key=lambda x: x[1].continuation_rate)
        best_opportunity = max(results.items(), key=lambda x: x[1].opportunity_rate)
        best_daily_expectancy = max(results.items(), key=lambda x: x[1].expectancy_per_day)
        
        print("RECOMMENDATIONS:")
        print("-" * 20)
        print(f"Best Expectancy/Trade: {best_expectancy[0]} ({best_expectancy[1].expectancy_per_trade:.2%})")
        print(f"Best Continuation: {best_continuation[0]} ({best_continuation[1].continuation_rate:.1%})")
        print(f"Best Opportunity Rate: {best_opportunity[0]} ({best_opportunity[1].opportunity_rate:.1%})")
        print(f"Best Expectancy/Day: {best_daily_expectancy[0]} ({best_daily_expectancy[1].expectancy_per_day:.2%})")
        
        print("\n📊 CAPITAL DEPLOYMENT ANALYSIS:")
        print("-" * 40)
        for bucket_name, result in results.items():
            # Capital deployment guidance
            if result.trades_per_day < 0.5:  # Less than 1 trade every 2 days
                frequency = "Very Low"
                deployment = "⚠️  Insufficient for daily trading"
            elif result.trades_per_day < 1.0:  # Less than 1 trade per day
                frequency = "Low"
                deployment = "📉 Consider pooling multiple buckets"
            elif result.trades_per_day < 2.0:  # 1-2 trades per day
                frequency = "Moderate"
                deployment = "✅ Suitable for small capital"
            else:  # 2+ trades per day
                frequency = "High"
                deployment = "🎯 Good for active trading"
            
            print(f"\n{bucket_name}:")
            print(f"  Frequency: {frequency} ({result.trades_per_day:.1f} trades/day)")
            print(f"  Daily P&L: {result.expectancy_per_day:.2%} per day")
            print(f"  Deployment: {deployment}")
        
        print("\n📊 OPPORTUNITY ANALYSIS:")
        print("-" * 30)
        for bucket_name, result in results.items():
            if result.opportunity_rate < 0.1:  # Less than 10% opportunity rate
                print(f"⚠️  {bucket_name}: Very low opportunity rate ({result.opportunity_rate:.1%})")
                print(f"   → {result.total_candidates} candidates → {result.actual_trades} trades")
            elif result.opportunity_rate < 0.3:  # Less than 30% opportunity rate
                print(f"📉 {bucket_name}: Low opportunity rate ({result.opportunity_rate:.1%})")
                print(f"   → {result.total_candidates} candidates → {result.actual_trades} trades")
            else:
                print(f"✅ {bucket_name}: Good opportunity rate ({result.opportunity_rate:.1%})")
                print(f"   → {result.total_candidates} candidates → {result.actual_trades} trades")
        
        if is_monotonic:
            print("\n🎯 Consider focusing on larger gaps (10%+) for pure continuation plays")
        else:
            print("\n⚠️  Consider adding filters or exploring fade strategies")
