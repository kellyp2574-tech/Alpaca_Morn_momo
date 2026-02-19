"""Core configuration for the morning momentum bot."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """Strategy parameters and guardrails."""

    # Strategy window
    scan_start: str = "04:00"
    scan_end: str = "09:25"
    entry_start: str = "09:33"
    entry_cutoff: str = "10:30"
    hard_exit: str = "11:00"
    intraday_start: str = "10:30"
    intraday_end: str = "11:00"
    market_open: str = "09:30"

    # Universe filters
    min_price: float = 2.0
    max_price: float = 20.0
    max_float: float = 30_000_000
    min_gap_pct: float = 0.08
    min_pm_vol_float: float = 0.03
    min_relvol: float = 2.0

    # Risk / slots
    risk_per_trade: float = 0.01  # 1% equity risk
    max_concurrent: int = 2
    max_trades_per_day: int = 4
    daily_kill_r: float = -2.0  # stop if realized <= -2R

    # Stops / management
    atr_len: int = 10  # 1m ATR length
    stop_atr_mult: float = 2.0
    stop_min_pct: float = 0.06
    stop_max_pct: float = 0.12

    breakeven_at_pct: float = 0.06  # +6% move stop to entry
    trail_activate_at_pct: float = 0.10  # +10% start trailing
    trail_pct_1: float = 0.03  # 3%
    trail_widen_at_pct: float = 0.20  # +20% widen trail
    trail_pct_2: float = 0.05  # 5%

    dead_momo_minutes: int = 20
    dead_momo_min_gain: float = 0.03  # must be +3% after 20 mins

    volume_avg_window: int = 5
    volume_spike_mult: float = 1.5
    entry_slip_pct: float = 0.002
    max_bar_delay_seconds: int = 30
    min_atr_dollars: float = 0.05
    min_1m_volume: int = 20_000
    min_1m_dollar_volume: float = 100_000.0
    max_breakout_extension_pct: float = 0.02
    exec_slippage_buy_pct: float = 0.002
    exec_slippage_sell_pct: float = 0.005
    max_spread_dollars: float = 0.03
    max_spread_pct: float = 0.005
    exit_spread_dollars: float = 0.10  # exit if spread exceeds this...
    exit_spread_pct: float = 0.02      # ...or this % of ask (whichever is larger)
    spread_exit_grace_seconds: int = 60  # ignore spread exits this many seconds after entry
    spread_exit_consecutive: int = 2     # require N consecutive bad-spread checks before exiting
    exit_ack_timeout_seconds: int = 15   # seconds before reconciling an unresolved exit order
    quote_refresh_seconds: int = 5
    candidate_retry_minutes: int = 5     # retry premarket scan every N minutes if no candidates