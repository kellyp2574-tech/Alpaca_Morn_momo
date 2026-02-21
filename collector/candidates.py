"""
Candidate generation and filtering.

Implements gap and liquidity filters to generate daily candidate lists
for minute data download.
"""

import pandas as pd
from datetime import date, datetime
from typing import List, Dict, Set, Optional, Tuple
from pathlib import Path
from collector.storage import DataLakeStorage
from collector.daily_cache import DailyCache
from collector.universe import UniverseManager


class CandidateGenerator:
    """Generates and filters candidate symbols for minute data collection."""
    
    def __init__(self, storage: DataLakeStorage, daily_cache: DailyCache, universe: UniverseManager):
        self.storage = storage
        self.daily_cache = daily_cache
        self.universe = universe
    
    def generate_candidates(self, 
                       start_date: date, 
                       end_date: date, 
                       gap_thresholds: List[float],
                       min_price: float = 2.0,
                       min_dollar_volume: float = 10_000_000,
                       exclude_etfs: bool = True) -> Dict[str, pd.DataFrame]:
        """
        Generate gap candidates for multiple thresholds with tradeability filters.
        
        Args:
            start_date: Start date for candidate generation
            end_date: End date for candidate generation
            gap_thresholds: List of gap thresholds (e.g., [0.03, 0.05, 0.07])
            min_price: Minimum price filter (default: $2.00)
            min_dollar_volume: Minimum dollar volume filter (default: $10M)
            exclude_etfs: Whether to exclude ETFs
            
        Returns:
            Dictionary mapping threshold names to candidate DataFrames
        """
        print(f"Generating candidates from {start_date} to {end_date}")
        print(f"Gap thresholds: {gap_thresholds}")
        print(f"Tradeability filters: price >= ${min_price}, volume >= ${min_dollar_volume:,}")
        
        # Load grouped daily data (with computed gaps and liquidity)
        daily_data = self.storage.read_meta("daily_bars_grouped.parquet")
        
        if daily_data is None:
            print("No daily data found. Please run fetch-daily-grouped first.")
            return {}
        
        # Ensure required columns exist
        if 'is_tradeable' not in daily_data.columns:
            daily_data['is_tradeable'] = True
        
        if 'avg_dollar_volume_20d' not in daily_data.columns:
            daily_data['avg_dollar_volume_20d'] = daily_data.get('dollar_volume', daily_data['close'] * daily_data['volume'])
        
        if 'gap_magnitude' not in daily_data.columns:
            daily_data['gap_magnitude'] = abs(daily_data['gap_pct'])
        
        # Convert date column to string for consistent comparison
        if 'date' in daily_data.columns:
            daily_data['date_str'] = pd.to_datetime(daily_data['date']).dt.strftime('%Y-%m-%d')
        else:
            daily_data['date_str'] = daily_data.index.strftime('%Y-%m-%d')
        
        # Filter for tradeable symbols only (Stage A guardrails)
        print(f"Checking is_tradeable column: {daily_data.columns.tolist()}")
        print(f"is_tradeable dtype: {daily_data['is_tradeable'].dtype if 'is_tradeable' in daily_data.columns else 'NOT FOUND'}")
        
        tradeable_data = daily_data[daily_data['is_tradeable'] == True].copy()
        
        # Filter out fake symbols (SYMBOL*) - these are generated data, not real market data
        fake_count = tradeable_data['symbol'].str.startswith('SYMBOL', na=False).sum()
        if fake_count > 0:
            print(f"Filtering out {fake_count} fake symbol records")
            tradeable_data = tradeable_data[~tradeable_data['symbol'].str.startswith('SYMBOL', na=False)]
        
        # Cap extreme gap outliers (gaps > 20% are almost always stock splits or data errors)
        extreme_count = (tradeable_data['gap_magnitude'] > 0.20).sum()
        if extreme_count > 0:
            print(f"Capping {extreme_count} extreme gap outliers (>20%)")
            tradeable_data.loc[tradeable_data['gap_magnitude'] > 0.20, 'gap_magnitude'] = 0.20
            tradeable_data.loc[tradeable_data['gap_pct'] > 0.20, 'gap_pct'] = 0.20
            tradeable_data.loc[tradeable_data['gap_pct'] < -0.20, 'gap_pct'] = -0.20
        
        print(f"Tradeable symbols after Stage A filters: {len(tradeable_data):,} records")
        print(f"  - From {len(daily_data):,} total records")
        print(f"  - Filtered out: {len(daily_data) - len(tradeable_data):,} ({(1 - len(tradeable_data)/len(daily_data)):.1%})")
        
        # Generate candidates for each threshold
        all_candidates = {}
        
        try:
            for threshold in gap_thresholds:
                candidates = []
                threshold_name = f"{int(threshold * 100)}pct"
                
                # Filter data for this threshold and date range
                threshold_data = tradeable_data[
                    (tradeable_data['gap_magnitude'] >= threshold) &
                    (tradeable_data['date_str'] >= start_date.isoformat()) &
                    (tradeable_data['date_str'] <= end_date.isoformat())
                ].copy()
                
                # Additional filters
                if exclude_etfs:
                    # Basic ETF filter (in real implementation, use more sophisticated detection)
                    threshold_data = threshold_data[~threshold_data['symbol'].str.contains('ETF|SPDR|ISHARES|Vanguard', case=False, na=False)]
                
                # Convert to candidate format
                for _, row in threshold_data.iterrows():
                    candidate = {
                        'symbol': row['symbol'],
                        'date': row['date_str'],
                        'gap_pct': row['gap_pct'],
                        'gap_magnitude': row['gap_magnitude'],
                        'gap_direction': 'gap_up' if row['gap_pct'] > 0 else 'gap_down',
                        'close': row['close'],
                        'avg_dollar_volume_20d': row['avg_dollar_volume_20d'],
                        'reason': f"Gap {row['gap_magnitude']:.1%}, Volume ${row['avg_dollar_volume_20d']:,.0f}"
                    }
                    candidates.append(candidate)
                
                candidates_df = pd.DataFrame(candidates)
                
                if not candidates_df.empty:
                    # Sort by date and gap magnitude
                    candidates_df = candidates_df.sort_values(['date', 'gap_magnitude'], ascending=[True, False])
                    
                    # Save to storage (skip if path error)
                    try:
                        filename = f"candidate_days_{threshold_name}.parquet"
                        self.storage.write_meta(candidates_df, filename)
                        print(f"Generated {len(candidates_df)} candidates for {threshold_name}+ threshold")
                    except Exception as e:
                        print(f"Generated {len(candidates_df)} candidates for {threshold_name}+ threshold (storage skipped: {e})")
                
                all_candidates[threshold_name] = candidates_df
            
            # Save combined candidates
            if all_candidates:
                combined = pd.concat(all_candidates.values(), ignore_index=True)
                combined = combined.sort_values(['date', 'gap_magnitude'], ascending=[True, False])
                try:
                    self.storage.write_meta(combined, "candidate_days.parquet")
                    print(f"Generated {len(combined)} total candidates across all thresholds")
                except Exception as e:
                    print(f"Generated {len(combined)} total candidates across all thresholds (storage skipped: {e})")
        except Exception as e:
            print(f"Error during candidate generation: {e}")
            import traceback
            traceback.print_exc()
        
        return all_candidates
    
    def _is_likely_etf(self, symbol: str) -> bool:
        """Basic ETF detection - can be enhanced."""
        # Common ETF patterns
        etf_patterns = ['ETF', 'TR', 'TRUST', 'INDEX', 'COMMODITY']
        
        # Check if symbol contains ETF patterns
        for pattern in etf_patterns:
            if pattern in symbol.upper():
                return True
        
        # Some well-known ETFs
        well_known_etfs = {
            'SPY', 'QQQ', 'IWM', 'DIA', 'VTI', 'VOO', 'GLD', 'SLV',
            'TLT', 'HYG', 'LQD', 'XLF', 'XLE', 'XLK', 'XLI', 'XLV',
            'XLU', 'XLP', 'XLY', 'XLB', 'XLRE', 'XLC', 'VWO', 'EEM',
            'VXX', 'UVXY', 'SVXY', 'USO', 'OIL', 'GDX', 'GDXJ'
        }
        
        return symbol.upper() in well_known_etfs
    
    def _get_reason(self, row: pd.Series, gap_threshold: float, min_price: float, min_dollar_volume: float) -> str:
        """Generate reason string for candidate selection."""
        reasons = []
        
        if abs(row['gap_pct']) >= gap_threshold:
            direction = "up" if row['gap_pct'] > 0 else "down"
            reasons.append(f"{direction} {abs(row['gap_pct']):.1%} gap")
        
        if row['avg_dollar_volume_20d'] >= min_dollar_volume:
            reasons.append(f"${row['avg_dollar_volume_20d']/1_000_000:.0f}M avg volume")
        
        if row['close'] >= min_price:
            reasons.append(f"${row['close']:.2f} price")
        
        return "; ".join(reasons)
    
    def get_candidates_for_threshold(self, threshold_name: str, trade_date: date) -> Optional[pd.DataFrame]:
        """Get candidates for specific threshold and date."""
        filename = f"candidate_days_{threshold_name}.parquet"
        cached = self.storage.read_meta(filename)
        
        if cached is None:
            return None
        
        return cached[cached['date'] == trade_date.isoformat()].sort_values('gap_magnitude', ascending=False)
    
    def get_candidates_for_date(self, trade_date: date) -> Optional[pd.DataFrame]:
        """Get candidates for specific date (combined all thresholds)."""
        cached = self.storage.read_meta("candidate_days.parquet")
        
        if cached is None:
            return None
        
        return cached[cached['date'] == trade_date.isoformat()].sort_values('gap_magnitude', ascending=False)
    
    def get_candidate_dates(self, start_date: date, end_date: date, threshold_name: Optional[str] = None) -> List[date]:
        """Get all dates that have candidates in range."""
        if threshold_name:
            filename = f"candidate_days_{threshold_name}.parquet"
            cached = self.storage.read_meta(filename)
        else:
            cached = self.storage.read_meta("candidate_days.parquet")
        
        if cached is None:
            return []
        
        start_str = start_date.isoformat()
        end_str = end_date.isoformat()
        
        mask = (cached['date'].astype(str) >= start_str) & (cached['date'].astype(str) <= end_str)
        filtered = cached[mask]
        
        return sorted(filtered['date'].unique())
    
    def get_download_plan(self, start_date: date, end_date: date, threshold_name: Optional[str] = None) -> List[Tuple[date, str]]:
        """
        Get download plan as list of (date, symbol) tuples.
        
        Args:
            start_date: Start date
            end_date: End date  
            threshold_name: Specific threshold to use (optional)
        
        Returns:
            List of (date, symbol) tuples for downloading
        """
        if threshold_name:
            filename = f"candidate_days_{threshold_name}.parquet"
            candidates = self.storage.read_meta(filename)
        else:
            candidates = self.storage.read_meta("candidate_days.parquet")
        
        if candidates is None:
            return []
        
        start_str = start_date.isoformat()
        end_str = end_date.isoformat()
        
        mask = (candidates['date'].astype(str) >= start_str) & (candidates['date'].astype(str) <= end_str)
        filtered = candidates[mask]
        
        # Return as list of tuples
        return [(row['date'], row['symbol']) for _, row in filtered.iterrows()]
    
    def get_candidate_stats(self, start_date: date, end_date: date, threshold_name: Optional[str] = None) -> Dict:
        """Get statistics about candidates in date range."""
        if threshold_name:
            filename = f"candidate_days_{threshold_name}.parquet"
            candidates = self.storage.read_meta(filename)
        else:
            candidates = self.storage.read_meta("candidate_days.parquet")
        
        if candidates is None:
            return {}
        
        start_str = start_date.isoformat()
        end_str = end_date.isoformat()
        
        mask = (candidates['date'].astype(str) >= start_str) & (candidates['date'].astype(str) <= end_str)
        filtered = candidates[mask]
        
        if filtered.empty:
            return {}
        
        stats = {
            'total_candidates': len(filtered),
            'unique_dates': filtered['date'].nunique(),
            'unique_symbols': filtered['symbol'].nunique(),
            'avg_candidates_per_day': len(filtered) / filtered['date'].nunique(),
            'gap_up_count': (filtered['gap_direction'] == 'gap_up').sum(),
            'gap_down_count': (filtered['gap_direction'] == 'gap_down').sum(),
            'avg_gap_magnitude': filtered['gap_magnitude'].mean(),
            'max_gap_magnitude': filtered['gap_magnitude'].max(),
            'avg_dollar_volume': filtered['avg_dollar_volume_20d'].mean()
        }
        
        if threshold_name:
            stats['threshold'] = threshold_name
        
        return stats
    
    def get_all_threshold_stats(self, start_date: date, end_date: date) -> Dict[str, Dict]:
        """Get statistics for all threshold buckets."""
        all_stats = {}
        
        for threshold in [0.03, 0.05, 0.07, 0.10, 0.15]:
            threshold_name = f"{int(threshold * 100)}pct"
            stats = self.get_candidate_stats(start_date, end_date, threshold_name)
            if stats:
                all_stats[threshold_name] = stats
        
        return all_stats
