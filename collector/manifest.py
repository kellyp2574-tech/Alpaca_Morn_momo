"""
Download manifest management.

Tracks download status, attempts, and metadata for restart-safe
data collection operations.
"""

import pandas as pd
from datetime import date, datetime
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import hashlib
from collector.storage import DataLakeStorage


class DownloadManifest:
    """Manages download manifest for tracking data collection status."""
    
    def __init__(self, storage: DataLakeStorage):
        self.storage = storage
        self._manifest: Optional[pd.DataFrame] = None
    
    def load_manifest(self) -> pd.DataFrame:
        """Load manifest from storage or create new one."""
        cached = self.storage.read_meta("download_manifest.parquet")
        
        if cached is not None:
            self._manifest = cached
            return cached
        
        # Create empty manifest
        columns = [
            'date', 'symbol', 'stage', 'status', 'attempts',
            'last_error', 'row_count', 'file_path', 'downloaded_at',
            'file_size', 'checksum', 'retry_after'
        ]
        
        empty_df = pd.DataFrame(columns=columns)
        self.storage.write_meta(empty_df, "download_manifest.parquet")
        self._manifest = empty_df
        
        return empty_df
    
    def add_entry(self, 
                  trade_date: date,
                  symbol: str,
                  stage: str,
                  status: str = 'pending',
                  attempts: int = 0,
                  last_error: str = '',
                  row_count: int = 0,
                  file_path: str = '',
                  downloaded_at: Optional[datetime] = None,
                  file_size: int = 0,
                  checksum: str = '',
                  retry_after: Optional[datetime] = None) -> None:
        """Add or update entry in manifest."""
        if self._manifest is None:
            self.load_manifest()
        
        # Check if entry exists
        mask = (self._manifest['date'].astype(str) == trade_date.isoformat()) & \
               (self._manifest['symbol'] == symbol) & \
               (self._manifest['stage'] == stage)
        
        existing = self._manifest[mask]
        
        if downloaded_at is None:
            downloaded_at = datetime.now()
        
        entry_data = {
            'date': trade_date.isoformat(),
            'symbol': symbol,
            'stage': stage,
            'status': status,
            'attempts': attempts,
            'last_error': last_error,
            'row_count': row_count,
            'file_path': file_path,
            'downloaded_at': downloaded_at,
            'file_size': file_size,
            'checksum': checksum,
            'retry_after': retry_after
        }
        
        if existing.empty:
            # Add new entry
            new_df = pd.DataFrame([entry_data])
            self._manifest = pd.concat([self._manifest, new_df], ignore_index=True)
        else:
            # Update existing entry
            for col, value in entry_data.items():
                self._manifest.loc[mask, col] = value
        
        # Save to storage
        self.storage.write_meta(self._manifest, "download_manifest.parquet")
    
    def mark_success(self, 
                    trade_date: date,
                    symbol: str,
                    stage: str,
                    row_count: int,
                    file_path: str,
                    file_size: int = 0) -> None:
        """Mark download as successful."""
        # Compute checksum if file exists
        checksum = ''
        if Path(file_path).exists():
            checksum = self._compute_file_checksum(file_path)
        
        self.add_entry(
            trade_date=trade_date,
            symbol=symbol,
            stage=stage,
            status='ok',
            attempts=1,
            row_count=row_count,
            file_path=file_path,
            file_size=file_size,
            checksum=checksum
        )
    
    def mark_failure(self,
                    trade_date: date,
                    symbol: str,
                    stage: str,
                    error: str,
                    retry_after: Optional[datetime] = None) -> None:
        """Mark download as failed."""
        # Get current attempts
        current_attempts = self.get_attempts(trade_date, symbol, stage)
        
        self.add_entry(
            trade_date=trade_date,
            symbol=symbol,
            stage=stage,
            status='failed',
            attempts=current_attempts + 1,
            last_error=error,
            retry_after=retry_after
        )
    
    def get_status(self, trade_date: date, symbol: str, stage: str) -> str:
        """Get download status for specific entry."""
        if self._manifest is None:
            self.load_manifest()
        
        mask = (self._manifest['date'].astype(str) == trade_date.isoformat()) & \
               (self._manifest['symbol'] == symbol) & \
               (self._manifest['stage'] == stage)
        
        entries = self._manifest[mask]
        
        if entries.empty:
            return 'pending'
        
        return entries.iloc[0]['status']
    
    def get_attempts(self, trade_date: date, symbol: str, stage: str) -> int:
        """Get number of download attempts for entry."""
        if self._manifest is None:
            self.load_manifest()
        
        mask = (self._manifest['date'].astype(str) == trade_date.isoformat()) & \
               (self._manifest['symbol'] == symbol) & \
               (self._manifest['stage'] == stage)
        
        entries = self._manifest[mask]
        
        if entries.empty:
            return 0
        
        return entries.iloc[0]['attempts']
    
    def get_pending_downloads(self, stage: str) -> List[Tuple[date, str]]:
        """Get list of pending downloads for stage."""
        if self._manifest is None:
            self.load_manifest()
        
        pending = self._manifest[
            (self._manifest['stage'] == stage) & 
            (self._manifest['status'] == 'pending')
        ]
        
        return [(date.fromisoformat(row['date']), row['symbol']) for _, row in pending.iterrows()]
    
    def get_failed_downloads(self, stage: str, max_attempts: int = 3) -> List[Tuple[date, str]]:
        """Get list of failed downloads that can be retried."""
        if self._manifest is None:
            self.load_manifest()
        
        now = datetime.now()
        
        failed = self._manifest[
            (self._manifest['stage'] == stage) & 
            (self._manifest['status'] == 'failed') &
            (self._manifest['attempts'] < max_attempts) &
            ((self._manifest['retry_after'].isna()) | (self._manifest['retry_after'] <= now))
        ]
        
        return [(date.fromisoformat(row['date']), row['symbol']) for _, row in failed.iterrows()]
    
    def is_downloaded(self, trade_date: date, symbol: str, stage: str) -> bool:
        """Check if data has been successfully downloaded."""
        return self.get_status(trade_date, symbol, stage) == 'ok'
    
    def get_download_stats(self, stage: str) -> Dict:
        """Get statistics about downloads for stage."""
        if self._manifest is None:
            self.load_manifest()
        
        stage_data = self._manifest[self._manifest['stage'] == stage]
        
        if stage_data.empty:
            return {}
        
        return {
            'total_entries': len(stage_data),
            'successful': (stage_data['status'] == 'ok').sum(),
            'failed': (stage_data['status'] == 'failed').sum(),
            'pending': (stage_data['status'] == 'pending').sum(),
            'total_rows': stage_data['row_count'].sum(),
            'avg_attempts': stage_data['attempts'].mean(),
            'max_attempts': stage_data['attempts'].max(),
            'total_file_size': stage_data['file_size'].sum()
        }
    
    def _compute_file_checksum(self, file_path: str) -> str:
        """Compute MD5 checksum of file."""
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
                return hashlib.md5(content).hexdigest()
        except Exception:
            return ''
    
    def cleanup_old_entries(self, days_to_keep: int = 30) -> None:
        """Remove old entries from manifest."""
        if self._manifest is None:
            self.load_manifest()
        
        cutoff_date = datetime.now() - pd.Timedelta(days=days_to_keep)
        
        # Keep successful entries older than cutoff, but remove failed/pending
        old_successful = self._manifest[
            (self._manifest['downloaded_at'] < cutoff_date) &
            (self._manifest['status'] == 'ok')
        ]
        
        old_other = self._manifest[
            (self._manifest['downloaded_at'] < cutoff_date) &
            (self._manifest['status'] != 'ok')
        ]
        
        # Keep only successful old entries
        self._manifest = pd.concat([
            self._manifest[self._manifest['downloaded_at'] >= cutoff_date],
            old_successful
        ], ignore_index=True)
        
        self.storage.write_meta(self._manifest, "download_manifest.parquet")
