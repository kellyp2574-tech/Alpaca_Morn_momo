"""JSON-backed persistence for bot runtime state."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from .storage import PositionState, position_state_from_dict, position_state_to_dict

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
