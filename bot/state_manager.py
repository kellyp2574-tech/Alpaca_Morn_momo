"""JSON-backed persistence for bot runtime state."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from .storage import (
    PendingEntryState,
    PositionState,
    pending_entry_from_dict,
    pending_entry_to_dict,
    position_state_from_dict,
    position_state_to_dict,
)

logger = logging.getLogger(__name__)


class StateStore:
    def __init__(self, path: str = "state/positions.json", *, risk_path: Optional[str] = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if risk_path:
            self.risk_path = Path(risk_path)
        else:
            self.risk_path = self.path.with_name("risk_state.json")
        self.risk_path.parent.mkdir(parents=True, exist_ok=True)
        self.pending_entries_path = self.path.with_name("pending_entries.json")

    def load_positions(self) -> Dict[str, PositionState]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except json.JSONDecodeError:
            logger.exception("Failed to decode %s; starting fresh", self.path)
            return {}
        positions: Dict[str, PositionState] = {}
        for item in raw:
            state = position_state_from_dict(item)
            positions[state.symbol] = state
        logger.info("Loaded %d open positions from %s", len(positions), self.path)
        return positions

    def save_positions(self, positions: Dict[str, PositionState]) -> None:
        payload = [position_state_to_dict(state) for state in positions.values()]
        tmp_path = self.path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        tmp_path.replace(self.path)
        logger.debug("Persisted %d positions to %s", len(payload), self.path)

    # ------------------------------------------------------------------

    def load_pending_entries(self) -> Dict[str, PendingEntryState]:
        """Return {symbol: PendingEntryState} for all persisted pending entries."""
        if not self.pending_entries_path.exists():
            return {}
        try:
            with self.pending_entries_path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except json.JSONDecodeError:
            logger.exception("Failed to decode %s; ignoring", self.pending_entries_path)
            return {}
        result: Dict[str, PendingEntryState] = {}
        for item in raw:
            p = pending_entry_from_dict(item)
            result[p.symbol] = p
        logger.info("Loaded %d pending entries from %s", len(result), self.pending_entries_path)
        return result

    def save_pending_entries(self, pending: Dict[str, PendingEntryState]) -> None:
        payload = [pending_entry_to_dict(p) for p in pending.values()]
        tmp = self.pending_entries_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        tmp.replace(self.pending_entries_path)
        logger.debug("Persisted %d pending entries", len(payload))

    def clear_pending_entry(self, symbol: str) -> None:
        """Remove a single pending entry by symbol and re-save."""
        pending = self.load_pending_entries()
        if symbol in pending:
            del pending[symbol]
            self.save_pending_entries(pending)

    # ------------------------------------------------------------------

    def load_risk_state(self) -> Dict[str, Any]:
        if not self.risk_path.exists():
            return {}
        try:
            with self.risk_path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except json.JSONDecodeError:
            logger.exception("Failed to decode %s; starting fresh risk state", self.risk_path)
            return {}

    def save_risk_state(self, state: Dict[str, Any]) -> None:
        tmp_path = self.risk_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        tmp_path.replace(self.risk_path)
        logger.debug("Persisted risk state to %s", self.risk_path)
