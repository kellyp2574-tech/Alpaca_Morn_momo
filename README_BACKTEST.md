# Morning Momentum Backtesting Framework

A comprehensive data collection and backtesting framework for morning momentum strategies using Polygon API. Implements a 3-stage pipeline to efficiently collect and process market data.

## Architecture Overview

### Stage A: Candidate Generation
- Build tradable universe (2K-4K symbols vs 10K+)
- Apply gap + liquidity filters using daily bars
- Generate daily candidate lists (typically 10-30 symbols/day)

### Stage B: Minute Data Collection  
- Download 1-minute bars only for candidate symbol-days
- Focus on 04:00-11:00 ET window (morning session)
- Manifest-based tracking for restart-safe operation

### Stage C: Feature Engineering
- Compute MACD, VWAP, ORH/ORL locally
- Store derived features separately for iteration
- No API calls for indicators

## Data Lake Structure

```
data/
  raw/
    minute/
      date=YYYY-MM-DD/
        symbol=TSLA.parquet
        symbol=NVDA.parquet
  derived/
    features/
      date=YYYY-MM-DD/
        symbol=TSLA.parquet
  meta/
    trading_days.parquet
    daily_bars.parquet
    download_manifest.parquet
    candidate_days.parquet
    universe_YYYY-MM-DD.parquet
```

## Quick Start

### 1. Setup Environment
```bash
python -m venv .venv
.venv/Scripts/activate  # Windows
pip install -r requirements.txt
cp .env.example .env
# Add your Polygon API key to .env
```

### 2. Build Tradable Universe
```bash
python -m collector.cli build-universe --asof 2026-02-01
```

### 3. Fetch Daily Bars
```bash
python -m collector.cli fetch-daily --start 2021-01-01 --end 2026-02-01
```

### 4. Generate Candidates
```bash
python -m collector.cli make-candidates --start 2021-01-01 --end 2026-02-01 --gap 0.05 --min-dollar-volume 10000000
```

### 5. Download Minute Data
```bash
python -m collector.cli fetch-minutes --workers 10
```

### 6. Check Status
```bash
python -m collector.cli status
```

## CLI Commands

### Universe Management
```bash
# Build universe for specific date
python -m collector.cli build-universe --asof 2026-02-01 --min-price 1.0 --min-dollar-volume 10000000

# Universe stats are automatically saved to meta/universe_YYYY-MM-DD.parquet
```

### Daily Data Collection
```bash
# Fetch daily bars for date range
python -m collector.cli fetch-daily --start 2021-01-01 --end 2026-02-01

# Daily bars cached to meta/daily_bars.parquet
```

### Candidate Generation
```bash
# Generate candidates with custom filters
python -m collector.cli make-candidates \
  --start 2021-01-01 \
  --end 2026-02-01 \
  --gap 0.05 \           # 5% minimum gap
  --min-price 1.50 \     # $1.50 minimum price
  --min-dollar-volume 10000000  # $10M minimum daily volume

# Candidates saved to meta/candidate_days.parquet
```

### Minute Data Download
```bash
# Download with parallel workers
python -m collector.cli fetch-minutes --workers 10 --delay 0.1

# Retry failed downloads
python -m collector.cli retry-downloads --workers 5 --max-attempts 3

# Validate downloaded data
python -m collector.cli validate --start 2021-01-01 --end 2026-02-01
```

### Status and Monitoring
```bash
# Overall collection status
python -m collector.cli status

# Shows: trading days, daily bars, candidates, download progress, data size
```

## Default Filter Parameters

### Universe Filters (Stage A)
- **Price**: ≥ $1.00
- **Dollar Volume**: ≥ $10M (20-day average)  
- **Markets**: US stocks only (exclude OTC)
- **Result**: ~2,000-4,000 symbols

### Candidate Filters (Stage A→B)
- **Gap**: ≥ 5% absolute gap vs prior close
- **Price**: ≥ $1.50
- **Dollar Volume**: ≥ $10M average
- **ETFs**: Excluded by default
- **Result**: ~10-30 candidates/day

### Download Window (Stage B)
- **Session**: 04:00-11:00 ET
- **Granularity**: 1-minute bars
- **Format**: Compressed Parquet
- **Size**: ~7-10M rows total (~1-2GB)

## Data Size Estimates

For 5 years (~1,260 trading days):
- **Candidates**: ~15/day × 1,260 days = 18,900 symbol-days
- **Minutes**: 420 minutes/day × 18,900 = ~7.9M rows
- **Storage**: ~1-2GB compressed Parquet
- **Memory**: Easily fits in memory for analysis

## API Integration

### Polygon API Setup
```python
# Set in .env file
POLYGON_API_KEY=your_api_key_here
```

### API Endpoints Used
1. **Market Status** - Trading calendar
2. **Daily Aggregates** - Daily bars for universe
3. **Minute Aggregates** - 1-minute bars for candidates

### Rate Limiting
- **Parallel Workers**: 5-20 concurrent
- **Delay**: 0.1s between requests
- **Retry**: Exponential backoff on failures
- **Manifest**: Restart-safe operation

## Feature Engineering (Stage C)

Once minute data is collected, compute indicators locally:

```python
# Example: MACD on minute bars
def compute_macd(prices, fast=12, slow=26, signal=9):
    exp1 = prices.ewm(span=fast).mean()
    exp2 = prices.ewm(span=slow).mean()
    macd = exp1 - exp2
    signal_line = macd.ewm(span=signal).mean()
    histogram = macd - signal_line
    return macd, signal_line, histogram

# Example: VWAP
def compute_vwap(df):
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    vwap = (typical_price * df['volume']).cumsum() / df['volume'].cumsum()
    return vwap
```

## Backtesting Integration

The collected data provides:
- **Raw minute bars** for precise entry/exit timing
- **Derived features** for signal generation  
- **Candidate lists** for universe filtering
- **Daily context** for gap analysis

## Performance Optimization

### Parallel Processing
- Daily bars: Bulk fetch by symbol groups
- Minute data: 10-20 parallel workers
- Feature computation: Vectorized pandas operations

### Storage Efficiency  
- Parquet with Snappy compression
- Partitioned by date for selective loading
- Separate raw vs derived data

### Memory Management
- Process one date at a time for large analyses
- Use categorical dtypes for symbols
- Lazy loading with pandas chunking

## Troubleshooting

### Common Issues
1. **Missing candidates**: Check universe and daily bars
2. **Failed downloads**: Use `retry-downloads` command
3. **Memory errors**: Reduce workers or process date ranges
4. **API limits**: Adjust delay and worker count

### Validation
```bash
# Validate data integrity
python -m collector.cli validate --start 2021-01-01 --end 2021-01-31

# Check download progress
python -m collector.cli status
```

## Next Steps

1. **API Integration**: Replace placeholder data with Polygon API calls
2. **Feature Pipeline**: Build Stage C feature engineering
3. **Backtesting Engine**: Integrate with strategy logic
4. **Performance Tuning**: Optimize for your specific use case

## Data Flow Summary

```
Universe → Daily Bars → Candidates → Minute Data → Features → Backtest
   ↓           ↓           ↓            ↓           ↓          ↓
 2K-4K     5M rows     15/day     7.9M rows   Local     Strategy
symbols   (cached)   symbols    (1-2GB)   compute   testing
```
