# Optimized Data Collection Workflow

This guide shows how to efficiently collect backtesting data while respecting API rate limits (20 calls/minute).

## 🎯 The Strategy

**Key Insight**: Never download minute data for symbols you won't trade.

**Pipeline**:
1. **Stage A**: Grouped Daily (1,260 calls total) → Candidate filtering
2. **Stage B**: Minute downloads only for candidates → Backtesting

## 📊 Stage A: Grouped Daily Aggregates

### The Magic: 1 Call Per Day
Instead of calling per symbol, use Polygon's Grouped Daily endpoint:
- **1 call = ALL symbols for 1 day**
- **5 years = ~1,260 trading days = 1,260 calls**
- **At 20/min = ~63 minutes total**

### Command:
```bash
# Fetch all daily data (rate limited to 20/min)
python -m collector.cli fetch-daily-grouped --start 2021-01-01 --end 2026-02-01
```

### Output:
```
Fetching 1,260 trading days...
Estimated time: 63 minutes at 20 calls/min
Progress: 10/1260 days, 1250 remaining
...
Successfully cached 6,500,000 daily bars
Date range: 2021-01-04 to 2026-02-01
Symbols: 2,847
```

## 🎯 Stage B: Generate Candidates

### Filter Locally (No API Calls!)
```bash
# Generate candidates for all gap buckets
python -m collector.cli make-candidates --start 2021-01-01 --end 2026-02-01 --min-dollar-volume 10000000
```

### Output:
```
=== Candidate Statistics ===

3pct+:
  Total candidates: 45,200
  Avg per day: 18.2
  Gap up/down: 23,100/22,100

5pct+:
  Total candidates: 18,900
  Avg per day: 7.6
  Gap up/down: 9,800/9,100

7pct+:
  Total candidates: 8,400
  Avg per day: 3.4
  Gap up/down: 4,400/4,000

10pct+:
  Total candidates: 2,800
  Avg per day: 1.1
  Gap up/down: 1,500/1,300

15pct+:
  Total candidates: 520
  Avg per day: 0.2
  Gap up/down: 280/240
```

## ⚡ Stage C: Optimized Minute Downloads

### Priority-Based Download (Recommended)
Download high-value buckets first for early results:

```bash
# Download by priority (15% → 10% → 7% → 5% → 3%)
python -m collector.cli fetch-minutes-optimized
```

### Output:
```
Downloading minute data by priority: ['15pct', '10pct', '7pct', '5pct', '3pct']

=== Downloading 15pct bucket ===
Found 520 candidates in 15pct bucket
Downloading minute data for 520 candidate symbol-days
Rate limit: 20 calls/minute
Workers: 3, Delay: 3.0s
Estimated time: 26.0 minutes
Progress: 100/520 (3.3/min, ETA: 127s)
Bucket 15pct complete: 498 success, 22 failed

=== Downloading 10pct bucket ===
Found 2,800 candidates in 10pct bucket
...
```

### Specific Bucket Download
```bash
# Download only 10%+ gaps (fastest results)
python -m collector.cli fetch-minutes-optimized --threshold 10pct
```

## 📈 Time Estimates

### Complete Dataset (All Buckets):
- **Total candidates**: ~75,000 symbol-days
- **At 20/min**: ~62 hours
- **With 3 workers**: ~20-21 hours
- **Run overnight**: Perfect for weekend execution

### Quick Start (10%+ only):
- **Total candidates**: ~3,300 symbol-days
- **At 20/min**: ~2.75 hours
- **Perfect for**: Same-day backtesting

## 🎯 Backtesting with Opportunity Rates

### Run Backtest:
```bash
python -m backtest.cli --start 2021-01-01 --end 2026-02-01 --min-dollar-volume 10000000
```

### Enhanced Output Shows:
```
DETAILED ANALYSIS:
------------------

15pct+ Gap Bucket:
  Gap Range: 15%-∞
  Total Candidates: 520
  Actual Trades: 498
  Opportunity Rate: 95.8%
  Win Rate: 87.2%
  Expectancy: 2.1%
  Continuation Rate: 82.1%

10pct+ Gap Bucket:
  Gap Range: 10%-15%
  Total Candidates: 2,800
  Actual Trades: 2,643
  Opportunity Rate: 94.4%
  Win Rate: 79.5%
  Expectancy: 1.8%
  Continuation Rate: 76.3%

📊 OPPORTUNITY ANALYSIS:
----------------------
✅ 15pct+: Good opportunity rate (95.8%)
   → 520 candidates → 498 trades
✅ 10pct+: Good opportunity rate (94.4%)
   → 2,800 candidates → 2,643 trades
⚠️  7pct+: Low opportunity rate (89.2%)
   → 8,400 candidates → 7,493 trades
```

## 🚀 Recommended Workflow

### 1. Quick Start (Same Day):
```bash
# 1. Get daily data (63 minutes)
python -m collector.cli fetch-daily-grouped --start 2021-01-01 --end 2026-02-01

# 2. Generate candidates (2 minutes)
python -m collector.cli make-candidates --start 2021-01-01 --end 2026-02-01

# 3. Download 10%+ gaps only (3 hours)
python -m collector.cli fetch-minutes-optimized --threshold 10pct

# 4. Backtest (5 minutes)
python -m backtest.cli --start 2021-01-01 --end 2026-02-01
```

### 2. Complete Dataset (Weekend):
```bash
# 1. Daily data (1 hour)
python -m collector.cli fetch-daily-grouped --start 2021-01-01 --end 2026-02-01

# 2. Generate candidates (2 minutes)
python -m collector.cli make-candidates --start 2021-01-01 --end 2026-02-01

# 3. Download all buckets (run Friday evening)
python -m collector.cli fetch-minutes-optimized

# 4. Monday morning: Full backtest ready
python -m backtest.cli --start 2021-01-01 --end 2026-02-01
```

## 🔧 Rate Limiting Details

### Token Bucket Algorithm:
- **Capacity**: 20 tokens
- **Refill**: 20 tokens per minute
- **Backoff**: Exponential with jitter on 429/5xx

### Worker Configuration:
- **Recommended**: 3 workers, 3.0s delay
- **Why**: Prevents thundering herd, respects limits
- **Result**: Smooth 20 calls/minute, minimal retries

### Cost with Massive.com:
- **Free tier**: 5 calls/min → 3 days
- **Starter tier ($29/month)**: Unlimited calls → 4 hours
- **Your choice**: Respect limits either way!

## 📊 Storage Requirements

### Daily Data (Stage A):
- **Size**: ~2GB for 5 years
- **Format**: Parquet, compressed
- **Query**: Instant filtering by symbol/date

### Minute Data (Stage B):
- **10%+ only**: ~500MB
- **All buckets**: ~8GB
- **Format**: Parquet, partitioned by date

## 🎯 Key Benefits

1. **Respectful**: Never exceeds 20 calls/minute
2. **Efficient**: Only downloads needed data
3. **Incremental**: Start with 10%+, add more later
4. **Reliable**: Built-in retries and manifest tracking
5. **Cost-effective**: Works on free or paid tiers

## 🚨 What NOT to Do

❌ **"Minute data for 2,000 symbols for 5 years"**
- That's millions of calls
- Impossible under rate limits

❌ **"Pull minutes, then decide candidates"**
- Backwards approach
- Wastes API calls on non-candidates

❌ **"High concurrency (50+ workers)"**
- Won't help with rate limits
- Just amplifies retries

✅ **"Grouped daily → candidates → targeted minutes"**
- 1,260 calls vs millions
- Respectful and efficient
- Gets you trading faster!
