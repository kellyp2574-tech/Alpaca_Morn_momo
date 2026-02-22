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
    volume_5min: float = 0.0  # First 5-min dollar volume


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
        min_price: float = 2.0,
        slippage: float = 0.0,
        opening_strength: bool = False,
        exit_time: str = '11:00',
        entry_time: str = '9:35',
        year: int = None,
        exclude_top_pct: float = 0.0,
        portfolio: bool = False,
        daily_deploy_pct: float = 0.40,
        min_volume_5min: float = 0.0,
        bucket_range: str = None,
        entry_randomize: bool = False,
        fill_haircut: float = 0.0,
        capacity_test: bool = False,
        max_daily_deploy: float = 0.0,
        max_return_pct: float = 80.0,
        take_profit: float = 0.0,
        stop_loss: float = 0.0,
        hard_exit: str = None,
        pessimistic_tp_sl: bool = False,
        partial_tp_pct: float = 0.0,
        trail_pct: float = 0.0,
        monte_carlo: int = 0,
        block_size: int = 10,
        stress_slippage: float = 0.0,
        stress_participation: float = 0.0
    ) -> Dict[str, Dict]:
        """
        Run pure signal backtest for all gap buckets.
        
        Only applies basic tradability filters - no strategy filters.
        
        Args:
            slippage: Round-trip slippage as decimal (e.g., 0.005 = 0.5%)
            opening_strength: If True, only enter if first 5-min candle is green (9:35 close > 9:30 open)
            exit_time: Exit time in HH:MM format (default: 11:00)
            year: If set, only test that year (2021-2025)
            exclude_top_pct: Exclude top X% of trades by return (e.g., 1 for 1%)
            portfolio: If True, simulate equal-weight portfolio growth (1% per trade)
            min_volume_5min: Minimum volume in first 5 minutes (in dollars)
            bucket_range: Single bucket range string (e.g., "7-15" for 7-15%). Overrides default buckets
            entry_randomize: If True, randomize entry by ±1 minute (9:34-9:36) for robustness
            fill_haircut: Partial fill haircut (e.g., 0.0025 = 0.25% worse fill)
        """
        self.slippage = slippage
        self.opening_strength = opening_strength
        self.exit_time = exit_time
        self.entry_time = entry_time
        self.year = year
        self.exclude_top_pct = exclude_top_pct
        self.portfolio = portfolio
        self.daily_deploy_pct = daily_deploy_pct
        self.min_volume_5min = min_volume_5min
        self.bucket_range = bucket_range
        self.entry_randomize = entry_randomize
        self.fill_haircut = fill_haircut
        self.capacity_test = capacity_test
        self.max_daily_deploy = max_daily_deploy
        self.max_return_pct = max_return_pct
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.hard_exit = hard_exit if hard_exit else exit_time
        self.pessimistic_tp_sl = pessimistic_tp_sl
        self.partial_tp_pct = partial_tp_pct
        self.trail_pct = trail_pct
        self.monte_carlo = monte_carlo
        self.block_size = block_size
        self.stress_slippage = stress_slippage
        self.stress_participation = stress_participation
        
        # Apply stress overrides if set
        if stress_slippage > 0:
            self.slippage = stress_slippage
        if stress_participation > 0:
            self.volume_cap = stress_participation
        
        # Run capacity test if requested
        if capacity_test:
            return self._run_capacity_test(start_date, end_date, min_dollar_volume, min_price)
        
        # Determine buckets to test
        if bucket_range:
            # Parse custom bucket range (e.g., "7-15" -> 7% to 15%)
            try:
                min_gap, max_gap = map(float, bucket_range.split('-'))
                buckets = [(f"{bucket_range}%", (min_gap / 100, max_gap / 100))]
            except:
                print(f"Invalid bucket-range format: {bucket_range}. Using default.")
                buckets = [
                    ("7-10%", (0.07, 0.10)),
                    ("10-15%", (0.10, 0.15)),
                ]
        else:
            # Default: only test 7-10% and 10-15% buckets
            buckets = [
                ("7-10%", (0.07, 0.10)),
                ("10-15%", (0.10, 0.15)),
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
            
            # Year filter: skip if not the selected year
            if self.year is not None and trade_date.year != self.year:
                continue
            
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
        
        # Parse entry time
        entry_hour, entry_minute = map(int, self.entry_time.split(':'))
        entry_time = datetime.combine(trade_date, time(entry_hour, entry_minute))
        
        # Randomize entry by ±1 minute if enabled
        if getattr(self, 'entry_randomize', False):
            import random
            minute_offset = random.choice([-1, 0, 1])
            entry_time = datetime.combine(trade_date, time(entry_hour, entry_minute + minute_offset))
        
        # Find first bar at or after entry_time
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
        
        # Apply fill haircut (worse fill for partial fills)
        if getattr(self, 'fill_haircut', 0) > 0:
            entry_price = entry_price * (1 + self.fill_haircut)
        
        # Opening Strength Filter: Check if first 5-min candle is green (9:35 close > 9:30 open)
        # This removes weak opens that immediately sell off
        if getattr(self, 'opening_strength', False):
            # Get the 9:30 bar (first bar of the day)
            bar_930 = minute_data[minute_data['timestamp'] < entry_time]
            if not bar_930.empty:
                first_bar = bar_930.iloc[0]
                if first_bar['close'] <= first_bar['open']:
                    # First candle is red - skip this trade
                    return None
        
        # Liquidity Filter: Check first 5 min volume (only if min_vol_5min > 0)
        min_vol_5min = getattr(self, 'min_volume_5min', 0.0)
        
        if min_vol_5min > 0:
            vol_start = datetime.combine(trade_date, time(9, 30))
            vol_end = datetime.combine(trade_date, time(9, 35))
            first_5min = minute_data[(minute_data['timestamp'] >= vol_start) & (minute_data['timestamp'] < vol_end)]
            if not first_5min.empty:
                first_5min_vol = first_5min['volume'].sum()
                avg_price = first_5min[['open', 'close', 'high', 'low']].mean().mean()
                dollar_vol = first_5min_vol * avg_price
                if dollar_vol < min_vol_5min:
                    return None
        
        # Always calculate 5-min volume for portfolio cap (separate from liquidity filter)
        vol_start = datetime.combine(trade_date, time(9, 30))
        vol_end = datetime.combine(trade_date, time(9, 35))
        first_5min = minute_data[(minute_data['timestamp'] >= vol_start) & (minute_data['timestamp'] < vol_end)]
        dollar_vol = 0.0
        if not first_5min.empty:
            first_5min_vol = first_5min['volume'].sum()
            avg_price = first_5min[['open', 'close', 'high', 'low']].mean().mean()
            dollar_vol = first_5min_vol * avg_price
        
        # Use hard_exit for final exit
        hard_exit_time = getattr(self, 'hard_exit', self.exit_time)
        exit_hour, exit_minute = map(int, hard_exit_time.split(':'))
        hard_exit_dt = datetime.combine(trade_date, time(exit_hour, exit_minute))
        
        # Get trade window up to hard exit
        trade_window = minute_data[(minute_data['timestamp'] >= entry_time) & 
                                   (minute_data['timestamp'] <= hard_exit_dt)]
        
        # Check for TP/SL exit
        exit_price = None
        exit_time_actual = hard_exit_dt
        exit_reason = 'hard_exit'
        
        take_profit = getattr(self, 'take_profit', 0.0)
        stop_loss = getattr(self, 'stop_loss', 0.0)
        partial_tp_pct = getattr(self, 'partial_tp_pct', 0.0)
        trail_pct = getattr(self, 'trail_pct', 0.0)
        
        # Track partial TP state
        partial_tp_hit = False
        partial_tp_price = None
        partial_tp_time = None
        max_price_after_partial = None
        
        # Track trailing stop state
        trailing_active = False
        trail_peak = 0
        
        # Debug: print SL value
        if not hasattr(self, '_sl_debug_done'):
            print(f"  DEBUG SL: stop_loss={stop_loss} take_profit={take_profit} trail_pct={trail_pct}")
            self._sl_debug_done = True
        
        if take_profit > 0 or stop_loss > 0:
            # Skip entry bar - start from next bar
            trade_window_after_entry = trade_window[trade_window['timestamp'] > entry_time]
            
            pessimistic = getattr(self, 'pessimistic_tp_sl', False)
            
            # Check each bar for TP/SL
            for idx, bar in trade_window_after_entry.iterrows():
                current_price = bar['close']
                high = bar.get('high', current_price)
                low = bar.get('low', current_price)
                
                if entry_price > 0:
                    sl_price = entry_price * (1 - stop_loss) if stop_loss > 0 else 0
                    
                    # Same-bar TP/SL: if both hit in same bar, assume worse outcome (stop)
                    if stop_loss > 0 and take_profit > 0 and not pessimistic:
                        if sl_price > 0 and low <= sl_price and high >= (entry_price * (1 + take_profit)):
                            exit_price = sl_price
                            exit_time_actual = bar['timestamp']
                            exit_reason = 'stop_loss'
                            break
                    
                    # Check SL first
                    if stop_loss > 0 and sl_price > 0:
                        if pessimistic:
                            sl_trigger = current_price <= sl_price
                        else:
                            sl_trigger = low <= sl_price
                        if sl_trigger:
                            exit_price = current_price
                            exit_time_actual = bar['timestamp']
                            exit_reason = 'stop_loss'
                            break
                    
                    # Trailing stop: activate at take_profit, trail at trail_pct
                    if take_profit > 0 and trail_pct > 0:
                        activation_price = entry_price * (1 + take_profit)
                        
                        # Check if we've hit activation threshold
                        if not trailing_active:
                            if pessimistic:
                                activate = current_price >= activation_price
                            else:
                                activate = high >= activation_price
                            
                            if activate:
                                trailing_active = True
                                trail_peak = high if not pessimistic else current_price
                        
                        # If trailing is active, track peak and check trail
                        if trailing_active:
                            # Update peak
                            if not pessimistic:
                                trail_peak = max(trail_peak, high)
                            else:
                                trail_peak = max(trail_peak, current_price)
                            
                            # Check trailing stop
                            trail_trigger_price = trail_peak * (1 - trail_pct)
                            if pessimistic:
                                trail_trigger = current_price <= trail_trigger_price
                            else:
                                trail_trigger = low <= trail_trigger_price
                            
                            if trail_trigger:
                                exit_price = current_price
                                exit_time_actual = bar['timestamp']
                                exit_reason = 'trailing_stop'
                                break
                    
                    # Track max price after partial TP for trailing
                    if partial_tp_hit and max_price_after_partial is not None:
                        max_price_after_partial = max(max_price_after_partial, high)
                        
                        # Check trailing stop
                        if trail_pct > 0:
                            trail_trigger_price = max_price_after_partial * (1 - trail_pct)
                            if pessimistic:
                                trail_trigger = current_price <= trail_trigger_price
                            else:
                                trail_trigger = low <= trail_trigger_price
                            
                            if trail_trigger:
                                exit_price = current_price
                                exit_time_actual = bar['timestamp']
                                exit_reason = 'trailing_stop'
                                break
                    
                    # Check SL - use close in pessimistic mode, low otherwise
                    if pessimistic:
                        sl_trigger = current_price <= sl_price
                    else:
                        sl_trigger = low <= sl_price
                    
                    if stop_loss > 0 and sl_trigger:
                        exit_price = current_price
                        exit_time_actual = bar['timestamp']
                        exit_reason = 'stop_loss'
                        break
        
        # If no TP/SL hit, use hard exit price
        if exit_price is None:
            exit_bars = minute_data[(minute_data['timestamp'] >= entry_time) & 
                                    (minute_data['timestamp'] <= hard_exit_dt)]
            
            if exit_bars.empty:
                post_entry = minute_data[minute_data['timestamp'] > entry_time]
                if post_entry.empty:
                    return None
                exit_bar = post_entry.iloc[-1]
            else:
                exit_bar = exit_bars.iloc[-1]
            
            exit_price = exit_bar['close']
            exit_time_actual = exit_bar['timestamp']
        
        # Apply slippage with multiplier based on exit type
        if entry_price > 0:
            base_slippage = getattr(self, 'slippage', 0.0)
            
            # Slippage multiplier: stop/trail = 1.5x, time exit = 1.1x
            if exit_reason in ['stop_loss', 'trailing_stop']:
                slippage_multiplier = 1.5
            elif exit_reason == 'hard_exit':
                slippage_multiplier = 1.1
            else:
                slippage_multiplier = 1.0
            
            slippage = base_slippage * slippage_multiplier
            slippage_half = slippage / 2
            entry_with_slippage = entry_price * (1 + slippage_half)
            exit_with_slippage = exit_price * (1 - slippage_half)
            return_pct = (exit_with_slippage - entry_with_slippage) / entry_with_slippage * 100
            
            # Debug slippage for first trade
            if not hasattr(self, '_slippage_debug_done'):
                entry_slip = entry_with_slippage - entry_price
                exit_slip = entry_price - exit_with_slippage
                total_slip = entry_slip + exit_slip
                total_slip_pct = (total_slip / entry_price) * 100
                print(f"  DEBUG SLIPPAGE: entry_raw=${entry_price:.2f} entry_filled=${entry_with_slippage:.2f} exit_raw=${exit_price:.2f} exit_filled=${exit_with_slippage:.2f} slip_$$={total_slip:.2f} slip_%={total_slip_pct:.2f}% exit={exit_reason}")
                self._slippage_debug_done = True
        else:
            return_pct = 0.0
        
        # Calculate MFE/MAE for the trade window
        trade_window = minute_data[(minute_data['timestamp'] >= entry_time) & 
                                   (minute_data['timestamp'] <= exit_time_actual)]
        
        if trade_window.empty or entry_price <= 0:
            mfe = 0.0
            mae = 0.0
        else:
            mfe = (trade_window['high'].max() - entry_price) / entry_price * 100
            mae = (entry_price - trade_window['low'].min()) / entry_price * 100
        
        continuation = return_pct > 0
        
        # Sanity filter: exclude extreme returns
        max_return = getattr(self, 'max_return_pct', 80.0)
        if abs(return_pct) > max_return:
            return None
        
        return TradeResult(
            symbol=symbol,
            trade_date=trade_date,
            entry_time=entry_time,
            entry_price=entry_price,
            exit_time=exit_time_actual,
            exit_price=exit_price,
            gap_pct=candidate.get('gap_pct', 0) * 100,
            return_pct=return_pct,
            mfe=mfe,
            mae=mae,
            continuation=continuation,
            volume_5min=dollar_vol
        )
    
    def _run_capacity_test(self, start_date, end_date, min_dollar_volume, min_price):
        """Run capacity sensitivity analysis with multiple starting capital levels."""
        
        # Define starting capital levels to test
        starting_capitals = [25000, 100000, 250000, 1000000, 5000000, 10000000, 25000000, 50000000, 100000000]
        
        print("\n" + "=" * 100)
        print("CAPACITY SENSITIVITY ANALYSIS")
        print("=" * 100)
        slippage = getattr(self, 'slippage', 0.01) * 100
        haircut = getattr(self, 'fill_haircut', 0.0) * 100
        print(f"Strategy: {bucket_name} gap, Opening Strength, 9:45 exit, {slippage:.1f}% slippage, {haircut:.2f}% fill haircut, 5% volume cap")
        print(f"Period: {start_date} to {end_date}")
        print("=" * 100)
        
        # Get bucket config from instance variable
        bucket_range = getattr(self, 'bucket_range', '7-15')
        try:
            min_gap, max_gap = map(float, bucket_range.split('-'))
            min_gap = min_gap / 100
            max_gap = max_gap / 100
            bucket_name = f"{bucket_range}%"
        except:
            min_gap, max_gap = 0.07, 0.15
            bucket_name = "7-15%"
        
        print(f"Testing bucket: {bucket_name}")
        
        # Get candidates using existing method
        candidates = self._get_bucket_candidates(start_date, end_date, min_gap, max_gap, min_dollar_volume, min_price)
        if candidates.empty:
            print("No candidates found")
            return {}
        
        # Execute all trades
        all_trades = []
        for idx, row in candidates.iterrows():
            trade_date = row['date']
            if isinstance(trade_date, str):
                trade_date = date.fromisoformat(trade_date)
            symbol = row['symbol']
            
            # Get minute data
            minute_data = self.storage.read_minute_data(trade_date, symbol)
            if minute_data is None or minute_data.empty:
                continue
            
            # Execute trade
            trade = self._execute_pure_trade(minute_data, trade_date, symbol, row)
            if trade:
                all_trades.append(trade)
        
        print(f"Loaded {len(all_trades)} trades for capacity analysis")
        
        # Group by date for portfolio simulation
        trades_by_date = {}
        for t in all_trades:
            trade_date = t.entry_time.date() if hasattr(t.entry_time, 'date') else t.entry_time
            if trade_date not in trades_by_date:
                trades_by_date[trade_date] = []
            trades_by_date[trade_date].append(t)
        
        results = []
        
        for initial_capital in starting_capitals:
            # Run portfolio simulation
            sim_result = self._run_portfolio_simulation(all_trades, trades_by_date, initial_capital)
            results.append({
                'starting_capital': initial_capital,
                **sim_result
            })
        
        # Print summary table
        print(f"\n{'Start Cap':>12} {'Final Equity':>15} {'Return %':>12} {'Max DD %':>10} {'Max DD $':>15} {'Trades':>8} {'% Capped':>10} {'Avg Part':>10}")
        print("-" * 100)
        
        for r in results:
            print(f"${r['starting_capital']:>11,.0f} ${r['final_capital']:>14,.0f} {r['total_return']:>11.1f}% {r['max_drawdown']:>9.1f}% ${r['max_drawdown_dollars']:>14,.0f} {r['total_trades']:>8} {r['pct_capped']:>9.1f}% {r['avg_participation']:>9.2f}%")
        
        print("\n" + "=" * 100)
        print("DETAILED RESULTS BY CAPITAL LEVEL")
        print("=" * 100)
        
        for r in results:
            print(f"\n=== Starting Capital: ${r['starting_capital']:,} ===")
            print(f"  Final Equity: ${r['final_capital']:,.0f}")
            print(f"  Total Return: {r['total_return']:.1f}%")
            print(f"  Max Drawdown: {r['max_drawdown']:.1f}% (${r['max_drawdown_dollars']:,.0f})")
            print(f"  Longest Flat Period: {r['max_flat_days']} days")
            print(f"  Total Trades: {r['total_trades']}")
            print(f"  Trades Capped: {r['capped_trades']} ({r['pct_capped']:.1f}%)")
            print(f"  Avg Participation: {r['avg_participation']:.2f}%")
            print(f"  Max Participation: {r['max_participation']:.2f}%")
            print(f"  Avg Deployed/Day: ${r['avg_deployed']:,.0f}")
            print(f"  Total Requested: ${r['total_requested']:,.0f}")
            print(f"  Total Executed: ${r['total_executed']:,.0f}")
            print(f"  Utilization: {r['utilization']:.1f}%")
            # First day sanity checks
            print(f"  --- First Day Sanity ---")
            print(f"  First Day Equity: ${r.get('first_day_equity', 0):,.0f}")
            print(f"  First Day Budget: ${r.get('first_day_budget', 0):,.0f}")
            print(f"  First Day Requested: ${r.get('first_day_requested', 0):,.0f}")
            print(f"  First Day Executed: ${r.get('first_day_executed', 0):,.0f}")
            print(f"  First Day Max Part: {r.get('first_day_max_part', 0):.2f}%")
        
        return {'capacity_test': results}
    
    def _run_portfolio_simulation(self, trades, trades_by_date, initial_capital, max_daily_deployment=0.40):
        """Run portfolio simulation with given starting capital."""
        
        capital = initial_capital
        peak_capital = initial_capital
        max_drawdown = 0
        flat_days = 0
        max_flat_days = 0
        
        total_capped_trades = 0
        total_trades = 0
        participation_rates = []
        total_deployed = 0
        total_unused = 0
        total_requested = 0
        
        # Get max daily deploy - use CLI arg or default to percentage
        max_daily_deploy = getattr(self, 'max_daily_deploy', 0)
        
        # Track first day metrics for sanity check
        first_day_metrics = None
        
        # Track daily PnL for Monte Carlo
        daily_pnls = []
        
        for i, trade_date in enumerate(sorted(trades_by_date.keys())):
            date_trades = trades_by_date[trade_date]
            if not date_trades:
                continue
            
            # Get trades for this date to access their 5-min volumes
            date_trade_objs = [t for t in trades if (t.entry_time.date() if hasattr(t.entry_time, 'date') else t.entry_time) == trade_date]
            
            # Calculate daily capital
            daily_capital = capital * max_daily_deployment
            
            # Apply hard daily deploy cap if set
            if max_daily_deploy > 0:
                daily_capital = min(max_daily_deploy, daily_capital)
            
            position_per_trade = daily_capital / len(date_trades)
            
            # Calculate P&L using executed positions (after cap)
            daily_pnl = 0
            day_requested = 0
            day_executed = 0
            day_participations = []
            executed_positions = []
            
            # Debug: print first 10 trades
            debug_count = 0
            
            for j, t in enumerate(date_trades):
                total_trades += 1
                # Get corresponding trade object for volume info
                trade_obj = date_trade_objs[j] if j < len(date_trade_objs) else None
                
                if trade_obj and trade_obj.volume_5min > 0:
                    max_position = trade_obj.volume_5min * 0.05
                    day_requested += position_per_trade
                    
                    # Calculate executed position for this trade
                    executed_position = position_per_trade
                    if position_per_trade > max_position:
                        executed_position = max_position
                        total_capped_trades += 1
                    
                    day_executed += executed_position
                    participation = executed_position / trade_obj.volume_5min * 100
                    day_participations.append(participation)
                    participation_rates.append(participation)
                    
                    # Debug output for first 10 trades
                    if i == 0 and debug_count < 10:
                        slippage = getattr(self, 'slippage', 0.0)
                        haircut = getattr(self, 'fill_haircut', 0.0)
                        # Approximate gross return (without slippage/haircut)
                        gross_ret = t / (1 - slippage) if slippage > 0 else t
                        slip_cost = executed_position * (slippage / 2) * 2  # round-trip
                        haircut_cost = executed_position * haircut
                        pnl_dollar = executed_position * (t / 100)
                        print(f"  DEBUG TRADE {debug_count+1}: {trade_obj.symbol} ret={t:.2f}% gross={gross_ret:.2f}% slip=${slip_cost:.2f} hair=${haircut_cost:.2f} pnl=${pnl_dollar:.2f}")
                        debug_count += 1
                    
                    # PnL uses EXECUTED position, not requested
                    daily_pnl += executed_position * (t / 100)
                    executed_positions.append(executed_position)
                else:
                    # No volume info - use requested position
                    day_requested += position_per_trade
                    day_executed += position_per_trade
                    daily_pnl += position_per_trade * (t / 100)
                    executed_positions.append(position_per_trade)
            
            # Capture first day metrics
            if i == 0:
                first_day_metrics = {
                    'first_day_equity': capital,
                    'first_day_budget': daily_capital,
                    'first_day_requested': day_requested,
                    'first_day_executed': day_executed,
                    'first_day_max_part': max(day_participations) if day_participations else 0
                }
            
            # Accumulate totals
            total_requested += day_requested
            total_deployed += day_executed
            total_unused += max(0, day_requested - day_executed)
            
            if capital <= peak_capital:
                flat_days += 1
                max_flat_days = max(max_flat_days, flat_days)
            else:
                flat_days = 0
            
            if capital > peak_capital:
                peak_capital = capital
            
            drawdown = (peak_capital - capital) / peak_capital * 100 if peak_capital > 0 else 0
            max_drawdown = max(max_drawdown, drawdown)
            
            capital += daily_pnl
        
        return {
            'final_capital': capital,
            'total_return': (capital - initial_capital) / initial_capital * 100,
            'max_drawdown': max_drawdown,
            'max_drawdown_dollars': peak_capital * max_drawdown / 100,
            'max_flat_days': max_flat_days,
            'total_trades': total_trades,
            'capped_trades': total_capped_trades,
            'pct_capped': total_capped_trades / total_trades * 100 if total_trades > 0 else 0,
            'avg_participation': np.mean(participation_rates) if participation_rates else 0,
            'max_participation': np.max(participation_rates) if participation_rates else 0,
            'avg_deployed': total_deployed / len(trades_by_date),
            'total_requested': total_requested,
            'total_executed': total_deployed,
            'utilization': total_deployed / total_requested * 100 if total_requested > 0 else 100,
            **first_day_metrics
        }
    
    def _run_monte_carlo(self, daily_pnls: List[float], initial_capital: float, iterations: int, block_size: int = 10):
        """Run block bootstrap Monte Carlo simulation on daily PnLs.
        
        Block bootstrap preserves:
        - Winning/losing streaks
        - Volatility regimes
        - Autocorrelation
        """
        import random
        
        print("\n" + "=" * 60)
        print(f"BLOCK BOOTSTRAP MONTE CARLO ({iterations} iterations, block={block_size})")
        print("=" * 60)
        
        n_days = len(daily_pnls)
        
        # Create overlapping blocks
        blocks = []
        for i in range(n_days - block_size + 1):
            block = daily_pnls[i:i + block_size]
            blocks.append(block)
        
        print(f"\nDaily PnL: mean=${np.mean(daily_pnls):.2f} std=${np.std(daily_pnls):.2f} days={n_days}")
        print(f"Blocks: {len(blocks)} overlapping blocks of {block_size} days")
        
        final_equities = []
        max_drawdowns = []
        longest_flats = []
        
        for _ in range(iterations):
            # Sample blocks with replacement until we reach n_days
            sampled_pnls = []
            while len(sampled_pnls) < n_days:
                block = random.choice(blocks)
                sampled_pnls.extend(block)
            
            # Trim to exact length
            sampled_pnls = sampled_pnls[:n_days]
            
            # Simulate equity curve
            capital = initial_capital
            peak = initial_capital
            max_dd = 0
            flat_days = 0
            max_flat = 0
            
            for pnl in sampled_pnls:
                capital += pnl
                if capital > peak:
                    peak = capital
                    flat_days = 0
                else:
                    flat_days += 1
                    max_flat = max(max_flat, flat_days)
                
                dd = (peak - capital) / peak * 100 if peak > 0 else 0
                max_dd = max(max_dd, dd)
            
            final_equities.append(capital)
            max_drawdowns.append(max_dd)
            longest_flats.append(max_flat)
        
        # Calculate percentiles (ascending order: worst -> best)
        final_equities.sort()
        max_drawdowns.sort()
        longest_flats.sort()
        
        # Percentile index: 1st percentile = worst (low index), 95th = best (high index)
        p1 = int(0.01 * iterations)
        p5 = int(0.05 * iterations)
        p50 = int(0.50 * iterations)
        p95 = int(0.95 * iterations)
        
        print(f"\nFinal Equity:")
        print(f"  1st percentile:  ${final_equities[p1]:>12,.0f}  ({final_equities[p1]/initial_capital*100:.1f}%)")
        print(f"  5th percentile:  ${final_equities[p5]:>12,.0f}  ({final_equities[p5]/initial_capital*100:.1f}%)")
        print(f"  Median:          ${final_equities[p50]:>12,.0f}  ({final_equities[p50]/initial_capital*100:.1f}%)")
        print(f"  95th percentile: ${final_equities[p95]:>12,.0f}  ({final_equities[p95]/initial_capital*100:.1f}%)")
        
        print(f"\nMax Drawdown:")
        print(f"  1st percentile:  {max_drawdowns[p1]:>6.1f}%")
        print(f"  5th percentile:  {max_drawdowns[p5]:>6.1f}%")
        print(f"  Median:          {max_drawdowns[p50]:>6.1f}%")
        print(f"  95th percentile: {max_drawdowns[p95]:>6.1f}%")
        
        # Probability of max DD > X%
        for threshold in [10, 15, 20, 25]:
            prob = sum(1 for dd in max_drawdowns if dd > threshold) / iterations * 100
            print(f"  P(DD > {threshold}%): {prob:.1f}%")
        
        print(f"\nLongest Flat Period:")
        print(f"  Median:          {longest_flats[p50]:>6.0f} days")
        print(f"  95th percentile: {longest_flats[p95]:>6.0f} days")
        
        print("=" * 60)
    
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
        
        # Exclude top X% of trades by return (if specified)
        exclude_pct = getattr(self, 'exclude_top_pct', 0.0)
        if exclude_pct > 0 and len(returns) > 10:
            threshold = np.percentile(returns, 100 - exclude_pct)
            returns = [r for r in returns if r <= threshold]
            trades = [t for t in trades if t.return_pct <= threshold]
        
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
            'trimmed_mean_5pct': np.mean(np.array(returns)[(np.array(returns) <= np.percentile(returns, 95)) & (np.array(returns) >= np.percentile(returns, 5))]) if len(returns) > 0 else 0.0,
            'trades': trades
        }
    
    def print_summary(self, results: Dict[str, Dict]):
        """Print comprehensive backtest summary."""
        
        print("\n" + "=" * 80)
        print("PURE SIGNAL GAP BACKTEST (Test 1)")
        filters = []
        if getattr(self, 'opening_strength', False):
            filters.append("Opening Strength")
        if getattr(self, 'portfolio', False):
            filters.append("Portfolio")
        exit_time = getattr(self, 'exit_time', '11:00')
        filter_str = f"Entry: 9:35 | Exit: {exit_time}" + (f" | Filters: {', '.join(filters)}" if filters else " | No Strategy Filters")
        print(filter_str)
        print("=" * 80)
        
        # Portfolio simulation: Option B - Daily capital cap with daily compounding
        if getattr(self, 'portfolio', False):
            print("\n--- PORTFOLIO SIMULATION (Option B: Daily Cap, 25% max deployed) ---")
            for bucket_name, result in results.items():
                trades = result.get('trades', [])
                if not trades:
                    continue
                
                # Track daily PnL for Monte Carlo
                daily_pnls = []
                
                # Group trades by date
                trades_by_date = {}
                for t in trades:
                    trade_date = t.entry_time.date() if hasattr(t.entry_time, 'date') else t.entry_time
                    if trade_date not in trades_by_date:
                        trades_by_date[trade_date] = []
                    trades_by_date[trade_date].append(t.return_pct)
                
                # Simulate: start with $5k, deploy per day, compound daily
                initial_capital = 5000
                max_daily_pct = getattr(self, 'daily_deploy_pct', 0.40)  # Configurable %
                max_daily_deploy = getattr(self, 'max_daily_deploy', 0)  # Hard cap if set
                
                capital = initial_capital
                peak_capital = initial_capital
                max_drawdown = 0
                flat_days = 0
                max_flat_days = 0
                
                # Cap statistics
                total_capped_trades = 0
                total_trades = 0
                participation_rates = []
                days_with_cap = 0
                
                # Day-level accumulators for proper reporting
                sum_daily_budget = 0.0
                sum_deployed = 0.0
                sum_unused = 0.0
                days_counted = 0
                
                for trade_date in sorted(trades_by_date.keys()):
                    daily_returns = trades_by_date[trade_date]
                    if not daily_returns:
                        continue
                    
                    # Get trades for this date to access their 5-min volumes
                    date_trades = [t for t in trades if (t.entry_time.date() if hasattr(t.entry_time, 'date') else t.entry_time) == trade_date]
                    
                    # Allocate daily capital - use 40% of current equity, capped by hard limit if set
                    daily_capital = capital * max_daily_pct
                    if max_daily_deploy > 0:
                        daily_capital = min(daily_capital, max_daily_deploy)
                    
                    # Debug: print first day capital details
                    if trade_date == sorted(trades_by_date.keys())[0]:
                        print(f"  DEBUG DAY 1: equity=${capital:.0f} 40pct=${capital*max_daily_pct:.0f} hard_cap=${max_daily_deploy} daily_cap=${daily_capital:.0f} trades={len(daily_returns)} pos_per_trade=${daily_capital/len(daily_returns):.2f}")
                    
                    # Calculate position per trade - apply cap ONLY if position exceeds 5% of volume
                    position_per_trade = daily_capital / len(daily_returns)
                    capped_trades = 0
                    debug_count = 0
                    
                    # Track executed positions per trade for PnL calculation
                    executed_positions = []
                    
                    # Debug: print first 10 trades
                    first_day_debug = (trade_date == sorted(trades_by_date.keys())[0])
                    
                    for t in date_trades:
                        total_trades += 1
                        if t.volume_5min > 0:
                            max_position = t.volume_5min * 0.05  # 5% of 5-min volume
                            # Calculate executed position for this specific trade
                            executed_position = position_per_trade
                            if position_per_trade > max_position:
                                executed_position = max_position  # Apply cap
                                capped_trades += 1
                                total_capped_trades += 1
                                # Debug output for capped trades
                                if debug_count < 3:
                                    print(f"  DEBUG CAPPED: date={trade_date} symbol={t.symbol} dv_5m=${t.volume_5min:,.0f} cap=${max_position:,.0f} req=${position_per_trade:,.0f} exec=${executed_position:,.0f} part={executed_position/t.volume_5min*100:.2f}%")
                                    debug_count += 1
                            
                            # Debug output for first 10 trades
                            if first_day_debug and debug_count < 15:
                                slippage = getattr(self, 'slippage', 0.0)
                                haircut = getattr(self, 'fill_haircut', 0.0)
                                slip_cost = executed_position * (slippage / 2) * 2
                                haircut_cost = executed_position * haircut
                                pnl_dollar = executed_position * (t.return_pct / 100)
                                print(f"  DEBUG TRADE: {t.symbol} ret={t.return_pct:.2f}% slip=${slip_cost:.2f} hair=${haircut_cost:.2f} pnl=${pnl_dollar:.2f}")
                                debug_count += 1
                            
                            # Use EXECUTED position for participation
                            participation_rates.append(executed_position / t.volume_5min if t.volume_5min > 0 else 0)
                            executed_positions.append(executed_position)
                        else:
                            executed_positions.append(position_per_trade)
                    
                    # Calculate daily P&L using EXECUTED positions
                    daily_pnl = 0
                    for i, ret in enumerate(daily_returns):
                        exec_pos = executed_positions[i] if i < len(executed_positions) else position_per_trade
                        daily_pnl += exec_pos * (ret / 100)
                    
                    # Track daily PnL for Monte Carlo
                    daily_pnls.append(daily_pnl)
                    
                    # Track deployed vs unused capital - use actual executed today
                    deployed_today = sum(executed_positions) if executed_positions else 0
                    unused_today = max(0, daily_capital - deployed_today)
                    
                    # Debug: print day-end deployed
                    if trade_date == sorted(trades_by_date.keys())[0]:
                        print(f"  DEBUG DAY 1 END: deployed_today=${deployed_today:.2f} unused=${unused_today:.2f}")
                    
                    # Accumulate day-level metrics
                    sum_daily_budget += daily_capital
                    sum_deployed += deployed_today
                    sum_unused += unused_today
                    days_counted += 1
                    
                    if capped_trades > 0:
                        days_with_cap += 1
                    
                    # Track flat days (no profit from previous peak)
                    if capital <= peak_capital:
                        flat_days += 1
                        max_flat_days = max(max_flat_days, flat_days)
                    else:
                        flat_days = 0
                    
                    # Track drawdown
                    if capital > peak_capital:
                        peak_capital = capital
                    drawdown = (peak_capital - capital) / peak_capital * 100 if peak_capital > 0 else 0
                    max_drawdown = max(max_drawdown, drawdown)
                    
                    # Compound daily
                    capital += daily_pnl
                
                total_return = (capital - initial_capital) / initial_capital * 100
                avg_participation = np.mean(participation_rates) * 100 if participation_rates else 0
                max_participation = np.max(participation_rates) * 100 if participation_rates else 0
                avg_deployed = sum_deployed / days_counted if days_counted > 0 else 0
                avg_unused = sum_unused / days_counted if days_counted > 0 else 0
                
                print(f"  DEBUG SUMMARY: days={days_counted} sum_deployed=${sum_deployed:,.2f} sum_unused=${sum_unused:,.2f}")
                print(f"{bucket_name}: {len(trades)} trades across {len(trades_by_date)} days -> Portfolio Return: {total_return:.2f}% | Final: ${capital:,.0f}")
                print(f"  Cap Stats: {total_capped_trades}/{total_trades} ({total_capped_trades/total_trades*100:.1f}%) trades capped | {days_with_cap} days affected")
                print(f"  Avg Participation: {avg_participation:.2f}% of 5-min vol | Max: {max_participation:.2f}%")
                print(f"  Avg Deployed: ${avg_deployed:,.0f}/day | Avg Unused: ${avg_unused:,.0f}/day")
                print(f"  Max Drawdown: {max_drawdown:.2f}% | Longest Flat Period: {max_flat_days} days")
                print(f"SUMMARY start={initial_capital:.0f} final={capital:.0f} ret_pct={total_return:.1f} max_dd_pct={max_drawdown:.1f} trades={len(trades)} win_pct={total_capped_trades/total_trades*100 if total_trades > 0 else 0:.1f} avg_ret={sum(t.return_pct for t in trades)/len(trades) if trades else 0:.2f} avg_deployed={avg_deployed:.0f}")
                
                # Run Monte Carlo if requested
                monte_carlo_iters = getattr(self, 'monte_carlo', 0)
                block_size = getattr(self, 'block_size', 10)
                if monte_carlo_iters > 0 and daily_pnls:
                    self._run_monte_carlo(daily_pnls, initial_capital, monte_carlo_iters, block_size)
            print("-" * 50)
        
        # Summary table
        print(f"\n{'Bucket':<8} {'Cand.':<7} {'Trades':<7} {'Trades/Day':<11} {'Win%':<8} {'Cont.%':<8} {'Avg%':<10} {'Med%':<10} {'Exp%':<10}")
        print("-" * 95)
        
        for bucket_name, result in results.items():
            trades_per_day = result.get('trades_per_day', 0)
            print(f"{bucket_name:<8} {result['total_candidates']:<7} {result['actual_trades']:<7} {trades_per_day:<11.1f} "
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
            if result['actual_trades'] > 0:
                print(f"  Std Dev: {result['std_dev']:.2f}%")
                print(f"  Max Return: {result['max_return']:.2f}%")
                print(f"  95th Percentile: {result['p95_return']:.2f}%")
                print(f"  99th Percentile: {result['p99_return']:.2f}%")
                print(f"  Trimmed Mean (5%): {result['trimmed_mean_5pct']:.2f}%")
                print(f"  Avg Win: {result['avg_win']:.2f}%")
                print(f"  Avg Loss: {result['avg_loss']:.2f}%")
                print(f"  Avg MFE: {result['avg_mfe']:.2f}%")
                print(f"  Avg MAE: {result['avg_mae']:.2f}%")
            else:
                print("  (No trades to calculate statistics)")
            print(f"  Expectancy: {result['expectancy']:.2f}%")
        
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
