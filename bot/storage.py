"""Dataclasses representing core runtime entities."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class Candidate:
    symbol: str
    price: float
    prev_close: float
    pm_last: float
    pm_high: float
    pm_volume: float
    avg_vol_30d: float
    float_shares: float

    gap_pct: float
    pm_vol_float: float
    relvol: float
    score: float


@dataclass
class PositionState:
    symbol: str
    entry_time: datetime
    entry_price: float
    qty: float

    stop_price: float
    peak_price: float

    r_stop_pct: float  # initial stop% used for sizing
    trail_pct: Optional[float] = None
    breakeven_set: bool = False
    trail_active: bool = False

    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    realized_r: Optional[float] = None


def position_state_to_dict(state: PositionState) -> Dict[str, Any]:
    return {
        "symbol": state.symbol,
        "entry_time": state.entry_time.isoformat(),
        "entry_price": state.entry_price,
        "qty": state.qty,
        "stop_price": state.stop_price,
        "peak_price": state.peak_price,
        "r_stop_pct": state.r_stop_pct,
        "trail_pct": state.trail_pct,
        "breakeven_set": state.breakeven_set,
        "trail_active": state.trail_active,
        "exit_time": state.exit_time.isoformat() if state.exit_time else None,
        "exit_price": state.exit_price,
        "realized_r": state.realized_r,
    }


def position_state_from_dict(payload: Dict[str, Any]) -> PositionState:
    entry_time = datetime.fromisoformat(payload["entry_time"])
    exit_time = datetime.fromisoformat(payload["exit_time"]) if payload.get("exit_time") else None
    return PositionState(
        symbol=payload["symbol"],
        entry_time=entry_time,
        entry_price=float(payload["entry_price"]),
        qty=float(payload["qty"]),
        stop_price=float(payload["stop_price"]),
        peak_price=float(payload["peak_price"]),
        r_stop_pct=float(payload["r_stop_pct"]),
        trail_pct=float(payload["trail_pct"]) if payload.get("trail_pct") is not None else None,
        breakeven_set=bool(payload.get("breakeven_set", False)),
        trail_active=bool(payload.get("trail_active", False)),
        exit_time=exit_time,
        exit_price=float(payload["exit_price"]) if payload.get("exit_price") is not None else None,
        realized_r=float(payload["realized_r"]) if payload.get("realized_r") is not None else None,
    )
