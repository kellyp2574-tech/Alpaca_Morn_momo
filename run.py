"""Single entry point for the morning momentum bot.

Usage (paper trading):
    python run.py

Usage (live trading):
    python run.py --live

Usage (dry run / no orders):
    python run.py --dry-run

The script runs the full daily cycle unattended:

  04:00  Premarket scan begins (waits here if started earlier)
  09:33  Entry window opens
  10:30  Entry window closes; intraday monitor takes over
  11:00  All remaining positions force-exited; bot signs off

Start it any time before 11:00 ET and it will figure out which phase to
run based on the current time and what positions are on disk.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

# ---------------------------------------------------------------------------
# Bootstrap: ensure the project root is on sys.path so `bot.*` imports work
# when this file is run directly (python run.py) rather than as a module.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Load .env early to ensure API keys are available throughout
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)

# Debug check - remove after verifying
import os
print("DOTENV_CHECK", bool(os.getenv("APCA_API_KEY_ID")), bool(os.getenv("APCA_API_SECRET_KEY")))

from bot.clock import MARKET_TZ, market_now, parse_time_str
from bot.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("run")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dt_for(time_str: str, reference: datetime) -> datetime:
    """Return a MARKET_TZ-aware datetime for *time_str* on *reference*'s date.

    Using datetime.combine with the tz argument (not astimezone) ensures the
    result is always correct on DST transition days.
    """
    return datetime.combine(
        reference.astimezone(MARKET_TZ).date(),
        parse_time_str(time_str),
        tzinfo=MARKET_TZ,
    )


def _wait_until(target_dt: datetime) -> None:
    """Block until wall clock reaches *target_dt*, logging progress every 5 min."""
    while True:
        now = market_now()
        remaining = (target_dt - now).total_seconds()
        if remaining <= 0:
            return
        if remaining > 60:
            logger.info(
                "Waiting until %s ET — %.0f minutes remaining",
                target_dt.strftime("%H:%M"),
                remaining / 60,
            )
        time.sleep(min(remaining, 30))


def _check_market_open(args: argparse.Namespace) -> bool:
    """Return True if the market is scheduled to be open today.

    Uses Alpaca's /clock endpoint which accounts for holidays and early closes.
    Falls back to True on any API error so a network hiccup doesn't abort the run.
    """
    if args.dry_run:
        logger.info("MARKET_CHECK skipped (dry_run) — assuming market open")
        return True
    try:
        import os as _os
        from alpaca.trading.client import TradingClient
        from dotenv import load_dotenv
        load_dotenv()
        api_key = _os.getenv("APCA_API_KEY_ID") or _os.getenv("ALPACA_API_KEY")
        secret_key = _os.getenv("APCA_API_SECRET_KEY") or _os.getenv("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            logger.warning("MARKET_CHECK skipped — no API credentials found")
            return True
        client = TradingClient(api_key, secret_key, paper=not args.live)
        clock = client.get_clock()
        today = market_now().astimezone(MARKET_TZ).date()
        # next_close is today when market is open or will open today;
        # on holidays/weekends next_close is a future trading day.
        trades_today = clock.next_close.astimezone(MARKET_TZ).date() == today
        if trades_today:
            logger.info(
                "MARKET_CHECK market trades today=%s next_open=%s next_close=%s",
                today,
                clock.next_open.astimezone(MARKET_TZ).strftime("%H:%M ET"),
                clock.next_close.astimezone(MARKET_TZ).strftime("%H:%M ET"),
            )
            return True
        else:
            logger.warning(
                "MARKET_CHECK market CLOSED today=%s (holiday or weekend) "
                "next_open=%s — exiting",
                today,
                clock.next_open.astimezone(MARKET_TZ).strftime("%Y-%m-%d %H:%M ET"),
            )
            return False
    except Exception as exc:
        logger.warning("MARKET_CHECK failed (%s) — assuming market open", exc)
        return True


def _has_positions_on_disk(state_path: str) -> bool:
    """Return True if the positions file exists and contains at least one entry."""
    p = Path(state_path)
    if not p.exists():
        return False
    try:
        import json
        data = json.loads(p.read_text())
        return isinstance(data, list) and len(data) > 0
    except Exception:
        return False


def _decide_phases(
    args: argparse.Namespace,
    cfg: Config,
    now: datetime,
) -> tuple:
    """Return (run_entry: bool, run_intraday: bool) based on time + disk state.

    Decision table (evaluated in order):
      1. --skip-entry flag → skip entry, run intraday
      2. Past intraday_end → nothing to do
      3. Positions on disk + past entry_cutoff → skip entry, run intraday
      4. Positions on disk + within entry window → run both (entry loop
         will manage existing positions; intraday picks up the tail)
      5. Past entry_cutoff, no positions → nothing to do
      6. Default → run both phases normally
    """
    entry_cutoff_dt = _dt_for(cfg.entry_cutoff, now)
    intraday_end_dt = _dt_for(cfg.intraday_end, now)
    has_positions = _has_positions_on_disk(args.state_path)

    if args.skip_entry:
        logger.info("PHASE_DECISION --skip-entry set → entry=skip intraday=run")
        return False, True

    if now >= intraday_end_dt:
        logger.warning(
            "PHASE_DECISION current time %s is past intraday_end %s — nothing to do",
            now.strftime("%H:%M ET"), cfg.intraday_end,
        )
        return False, False

    if has_positions and now >= entry_cutoff_dt:
        logger.info(
            "PHASE_DECISION positions on disk + past entry_cutoff %s "
            "→ entry=skip intraday=run",
            cfg.entry_cutoff,
        )
        return False, True

    if not has_positions and now >= entry_cutoff_dt:
        logger.info(
            "PHASE_DECISION no positions + past entry_cutoff %s "
            "→ nothing to do",
            cfg.entry_cutoff,
        )
        return False, False

    logger.info(
        "PHASE_DECISION positions_on_disk=%s time=%s "
        "→ entry=run intraday=run",
        has_positions, now.strftime("%H:%M ET"),
    )
    return True, True


# ---------------------------------------------------------------------------
# PID lock (covers entire session)
# ---------------------------------------------------------------------------

def _acquire_pid_lock(lock_path: str) -> None:
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing_pid = int(path.read_text().strip())
        except (ValueError, OSError):
            existing_pid = None
        if existing_pid and existing_pid != os.getpid():
            try:
                os.kill(existing_pid, 0)
                logger.error(
                    "PID_LOCK another instance is running (pid=%d lock=%s) "
                    "— exiting to prevent split-brain trading",
                    existing_pid, lock_path,
                )
                raise SystemExit(1)
            except ProcessLookupError:
                pass  # stale lock; safe to overwrite
    path.write_text(str(os.getpid()))
    logger.info("PID_LOCK acquired pid=%d path=%s", os.getpid(), lock_path)


def _release_pid_lock(lock_path: str) -> None:
    try:
        path = Path(lock_path)
        if path.exists() and path.read_text().strip() == str(os.getpid()):
            path.unlink()
            logger.info("PID_LOCK released path=%s", lock_path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Morning momentum bot — full daily cycle",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--live", action="store_true",
                        help="Use live trading account (default: paper)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log orders without submitting to Alpaca")
    parser.add_argument("--equity", type=float, default=100_000.0,
                        help="Account equity for risk-based sizing (default: 100000)")
    parser.add_argument("--max-notional-pct", type=float, default=0.15,
                        help="Max notional per trade as fraction of equity (default: 0.15)")
    parser.add_argument("--state-path", type=str, default="state/positions.json",
                        help="Path for persisted positions (default: state/positions.json)")
    parser.add_argument("--skip-entry", action="store_true",
                        help="Skip entry loop and go straight to intraday monitor")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Phase runners
# ---------------------------------------------------------------------------

def _run_entry_phase(args: argparse.Namespace) -> None:
    """Run premarket scan + entry loop. Blocks until hard_exit time."""
    from bot.main import main as entry_main

    logger.info("=" * 60)
    logger.info("PHASE 1 — Premarket scan + entry loop")
    logger.info("=" * 60)

    argv: List[str] = [
        "--state-path", args.state_path,
        "--equity", str(args.equity),
        "--max-notional-pct", str(args.max_notional_pct),
    ]
    if args.live:
        argv.append("--live")
    if args.dry_run:
        argv.append("--dry-run")

    entry_main(argv)
    logger.info("PHASE 1 complete")


def _run_intraday_phase(args: argparse.Namespace) -> None:
    """Run intraday monitor. Blocks until intraday_end time."""
    from bot.intraday import main as intraday_main

    logger.info("=" * 60)
    logger.info("PHASE 2 — Intraday monitor")
    logger.info("=" * 60)

    argv: List[str] = ["--state-path", args.state_path]
    if args.live:
        argv.append("--live")
    if args.dry_run:
        argv.append("--dry-run")

    intraday_main(argv)
    logger.info("PHASE 2 complete")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> None:
    args = _parse_args(argv)
    cfg = Config()
    now = market_now()

    lock_path = str(Path(args.state_path).parent / "bot.pid")

    # Item 3: PID lock acquired at very top, released in finally around entire session
    _acquire_pid_lock(lock_path)
    try:
        _run_session(args, cfg, now, lock_path)
    finally:
        _release_pid_lock(lock_path)


def _run_session(
    args: argparse.Namespace,
    cfg: Config,
    startup_now: datetime,
    lock_path: str,
) -> None:
    now = startup_now

    # Item 7: RUN_PLAN — one structured line with every session parameter
    logger.info(
        "RUN_PLAN date=%s mode=%s dry_run=%s equity=%.0f "
        "scan=%s entry=%s-%s intraday=%s-%s "
        "state=%s pid_lock=%s",
        now.strftime("%Y-%m-%d"),
        "LIVE" if args.live else "PAPER",
        args.dry_run,
        args.equity,
        cfg.scan_start,
        cfg.entry_start, cfg.entry_cutoff,
        cfg.intraday_start, cfg.intraday_end,
        args.state_path,
        lock_path,
    )

    # Item 4: market calendar check (DST-safe via Alpaca /clock)
    if not _check_market_open(args):
        return

    # Item 1: scan window — always log current time vs configured window clearly
    scan_start_dt = _dt_for(cfg.scan_start, now)
    if now < scan_start_dt:
        logger.info(
            "SCAN_WINDOW current=%s scan_start=%s ET → before scan window, sleeping",
            now.strftime("%H:%M:%S"), cfg.scan_start,
        )
        _wait_until(scan_start_dt)
        now = market_now()
    else:
        logger.info(
            "SCAN_WINDOW current=%s scan_start=%s ET → scan window open, proceeding",
            now.strftime("%H:%M:%S"), cfg.scan_start,
        )

    # Item 2: state-aware phase auto-detection
    run_entry, run_intraday = _decide_phases(args, cfg, now)

    if not run_entry and not run_intraday:
        logger.info("Nothing to do for today's session. Signing off.")
        return

    if run_entry:
        _run_entry_phase(args)

    if run_intraday:
        _run_intraday_phase(args)

    logger.info("=" * 60)
    logger.info("Daily cycle complete. Bot signing off.")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
