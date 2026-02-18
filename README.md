# Morning Momentum Bot

Automated premarket momentum strategy built on Alpaca. Scans top actives premarket, ranks by float churn / gap / relvol, and trades the top setups with VWAP + ATR confirmations. Includes live entry loop, intraday stop monitor, synthetic stops, and JSON state persistence.

## Features

- **Premarket scan** via Alpaca + FMP float cache (SQLite)
- **Signal logic** using VWAP confirmation and ATR-based stops
- **Risk controls**: slot caps, max trades/day, daily kill switch, dead-momo exits
- **Execution**: marketable limit orders, dry-run mode, paper/live toggle via env
- **Persistence**: open positions serialized to `state/positions.json`
- **Intraday loop** to manage stops after the entry window

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate  # Windows
pip install -r requirements.txt
cp .env.example .env
# Fill in Alpaca + FMP keys, set ALPACA_PAPER=true for paper trading
```

## Usage

Premarket entry loop (scans + trades):

```bash
python -m bot.main --most-active 60 --watchlist 15 --equity 150000
```

Intraday monitor (stop management after entries):

```bash
python -m bot.intraday
```

Use `--live` to flip to the live Alpaca account and `--dry-run` to log without submitting orders.
