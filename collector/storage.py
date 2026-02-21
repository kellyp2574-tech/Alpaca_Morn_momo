"""
Storage utilities for Parquet-based data lake operations.

Handles reading/writing Parquet files with consistent partitioning
and directory structure for the backtesting framework.
"""

import os
import pandas as pd
from pathlib import Path
from typing import Optional, List, Dict, Any
import pyarrow as pa
import pyarrow.parquet as pq
from datetime import datetime, date


class DataLakeStorage:
    """Manages Parquet storage operations for the data lake."""
    
    def __init__(self, base_path: str = None):
        if base_path is None:
            # Use absolute path to project root/data
            project_root = Path(__file__).parent.parent
            base_path = project_root / "data"
        else:
            base_path = Path(base_path)
        
        self.base_path = base_path
        self.raw_minute_path = self.base_path / "raw" / "minute"
        self.derived_features_path = self.base_path / "derived" / "features"
        self.meta_path = self.base_path / "meta"
        
        # Ensure directories exist
        for path in [self.raw_minute_path, self.derived_features_path, self.meta_path]:
            path.mkdir(parents=True, exist_ok=True)
    
    def get_minute_file_path(self, trade_date: date, symbol: str) -> Path:
        """Get path for raw minute data file."""
        date_str = trade_date.isoformat()
        symbol_dir = self.raw_minute_path / f"date={date_str}"
        symbol_dir.mkdir(parents=True, exist_ok=True)
        return symbol_dir / f"symbol={symbol}.parquet"
    
    def get_features_file_path(self, trade_date: date, symbol: str) -> Path:
        """Get path for derived features file."""
        date_str = trade_date.isoformat()
        symbol_dir = self.derived_features_path / f"date={date_str}"
        symbol_dir.mkdir(parents=True, exist_ok=True)
        return symbol_dir / f"symbol={symbol}.parquet"
    
    def get_meta_file_path(self, filename: str) -> Path:
        """Get path for metadata file."""
        return self.meta_path / filename
    
    def write_minute_data(self, trade_date, symbol, df=None) -> None:
        """Write minute data to Parquet with compression."""
        # Handle both (df, date, symbol) and (date, symbol, df) calling conventions
        if isinstance(trade_date, pd.DataFrame):
            # Called as write_minute_data(df, date, symbol)
            actual_df = trade_date
            actual_date = symbol
            actual_symbol = df
            df = actual_df
            trade_date = actual_date
            symbol = actual_symbol
        elif df is None:
            raise ValueError("write_minute_data requires DataFrame")
        
        if df.empty:
            return
            
        file_path = self.get_minute_file_path(trade_date, symbol)
        
        # Add metadata columns
        df = df.copy()
        df['trade_date'] = trade_date.isoformat()
        df['symbol'] = symbol
        
        # Write with compression
        table = pa.Table.from_pandas(df)
        pq.write_table(
            table, 
            file_path,
            compression='snappy'
        )
    
    def read_minute_data(self, trade_date: date, symbol: str) -> Optional[pd.DataFrame]:
        """Read minute data from Parquet."""
        file_path = self.get_minute_file_path(trade_date, symbol)
        
        if not file_path.exists():
            return None
            
        return pd.read_parquet(file_path)
    
    def write_features(self, df: pd.DataFrame, trade_date: date, symbol: str) -> None:
        """Write derived features to Parquet."""
        if df.empty:
            return
            
        file_path = self.get_features_file_path(trade_date, symbol)
        
        # Add metadata columns
        df = df.copy()
        df['trade_date'] = trade_date.isoformat()
        df['symbol'] = symbol
        
        # Write with compression
        table = pa.Table.from_pandas(df)
        pq.write_table(
            table,
            file_path,
            compression='snappy'
        )
    
    def read_features(self, trade_date: date, symbol: str) -> Optional[pd.DataFrame]:
        """Read derived features from Parquet."""
        file_path = self.get_features_file_path(trade_date, symbol)
        
        if not file_path.exists():
            return None
            
        return pd.read_parquet(file_path)
    
    def write_meta(self, df: pd.DataFrame, filename: str) -> None:
        """Write metadata to Parquet."""
        # Ensure filename is a string
        filename = str(filename)
        file_path = self.get_meta_file_path(filename)
        
        if df.empty:
            print("DataFrame is empty, skipping save")
            return
        
        # Debug: check all columns for Path objects
        for col in df.columns:
            if df[col].dtype == 'object':
                sample = df[col].dropna().head(1)
                if len(sample) > 0:
                    val = sample.iloc[0]
                    if hasattr(val, '__fspath__'):
                        print(f"Found Path in column {col}, converting to string")
                        df[col] = df[col].astype(str)
        
        print(f"Writing {len(df)} rows to {filename}")
        
        try:
            df.to_parquet(file_path, compression='snappy', index=False)
            print(f"Successfully saved to {file_path}")
        except Exception as e:
            print(f"Error saving: {e}")
            # Print column dtypes
            print("Column dtypes:")
            print(df.dtypes)
            raise
    
    def read_meta(self, filename: str) -> Optional[pd.DataFrame]:
        """Read metadata from Parquet."""
        file_path = self.get_meta_file_path(filename)
        
        if not file_path.exists():
            return None
            
        return pd.read_parquet(file_path)
    
    def list_dates_with_data(self, data_type: str = "minute") -> List[date]:
        """List all dates that have data."""
        if data_type == "minute":
            base_path = self.raw_minute_path
        elif data_type == "features":
            base_path = self.derived_features_path
        else:
            raise ValueError(f"Unknown data type: {data_type}")
        
        dates = []
        for date_dir in base_path.glob("date=*"):
            date_str = date_dir.name.split("=")[1]
            dates.append(date.fromisoformat(date_str))
        
        return sorted(dates)
    
    def list_symbols_for_date(self, trade_date: date, data_type: str = "minute") -> List[str]:
        """List all symbols that have data for a given date."""
        if data_type == "minute":
            base_path = self.raw_minute_path
        elif data_type == "features":
            base_path = self.derived_features_path
        else:
            raise ValueError(f"Unknown data type: {data_type}")
        
        date_str = trade_date.isoformat()
        date_dir = base_path / f"date={date_str}"
        
        if not date_dir.exists():
            return []
        
        symbols = []
        for file_path in date_dir.glob("symbol=*.parquet"):
            symbol = file_path.name.split("=")[1].replace(".parquet", "")
            symbols.append(symbol)
        
        return sorted(symbols)
    
    def delete_date_data(self, trade_date: date, data_type: str = "minute") -> None:
        """Delete all data for a specific date."""
        if data_type == "minute":
            base_path = self.raw_minute_path
        elif data_type == "features":
            base_path = self.derived_features_path
        else:
            raise ValueError(f"Unknown data type: {data_type}")
        
        date_str = trade_date.isoformat()
        date_dir = base_path / f"date={date_str}"
        
        if date_dir.exists():
            import shutil
            shutil.rmtree(date_dir)
