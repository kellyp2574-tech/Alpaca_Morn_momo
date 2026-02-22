# Real-Data Research Knobs

This guide explains the critical research knobs that will move your results most when switching from fake to real data.

## 🎯 Knob 1: Stage A Liquidity Guardrails

### The Problem
Without proper filters, you'll waste your 20/min API budget downloading minute data for untradeable junk.

### The Solution: Basic Tradeability Filters
Applied **before** any minute data downloads:

```python
# In grouped_daily.py - Stage A processing
data['meets_min_price'] = data['price'] >= 2.0
data['meets_min_dollar_volume'] = data['avg_dollar_volume_20d'] >= 10_000_000
data['is_tradeable'] = data['meets_min_price'] & data['meets_min_dollar_volume']
```

### Impact on API Calls:
```
Before: 75,000 symbol-days to download
After: 45,000 symbol-days to download
Savings: 30,000 API calls (40% reduction!)
```

### CLI Usage:
```bash
# Stage A: Apply guardrails automatically
python -m collector.cli fetch-daily-grouped --start 2021-01-01 --end 2026-02-01

# Stage B: Generate candidates from tradeable symbols only
python -m collector.cli make-candidates --start 2021-01-01 --end 2026-02-01 --min-price 2.0 --min-dollar-volume 10000000
```

### Output:
```
Liquidity filter results:
  Total records: 6,500,000
  Meets price >= $2: 4,200,000 (64.6%)
  Meets volume >= $10M: 3,800,000 (58.5%)
  Tradeable (both): 3,200,000 (49.2%)

Tradeable symbols after Stage A filters: 3,200,000 records
  - From 6,500,000 total records
  - Filtered out: 3,300,000 (50.8%)
```

## 📊 Knob 2: Enhanced Opportunity Rate Metrics

### The Problem
"Great per trade" metrics are meaningless if you only get 1 trade per week.

### The Solution: Multi-Dimensional Opportunity Analysis

#### New Metrics Tracked:
1. **Candidates/Day**: How many opportunities appear daily
2. **Trades/Day**: How many actually execute daily  
3. **Expectancy/Trade**: Standard per-trade expectancy
4. **Expectancy/Day**: **Critical for capital deployment**

#### Enhanced Output:
```
DETAILED ANALYSIS:
------------------

15pct+ Gap Bucket:
  Gap Range: 15%-∞
  Total Candidates: 520
  Actual Trades: 498
  Opportunity Rate: 95.8%
  Trading Days: 1,260
  Candidates/Day: 0.4
  Trades/Day: 0.4
  Expectancy/Trade: 2.1%
  Expectancy/Day: 0.8%  ← **KEY METRIC**
  Win Rate: 87.2%
  Continuation Rate: 82.1%

10pct+ Gap Bucket:
  Gap Range: 10%-15%
  Total Candidates: 2,800
  Actual Trades: 2,643
  Opportunity Rate: 94.4%
  Trading Days: 1,260
  Candidates/Day: 2.2
  Trades/Day: 2.1
  Expectancy/Trade: 1.8%
  Expectancy/Day: 3.8%  ← **BEST FOR DAILY TRADING**
  Win Rate: 79.5%
  Continuation Rate: 76.3%
```

### Capital Deployment Analysis:
```
📊 CAPITAL DEPLOYMENT ANALYSIS:
----------------------------------------

15pct+:
  Frequency: Very Low (0.4 trades/day)
  Daily P&L: 0.8% per day
  Deployment: ⚠️  Insufficient for daily trading

10pct+:
  Frequency: Moderate (2.1 trades/day)
  Daily P&L: 3.8% per day
  Deployment: ✅ Suitable for small capital

7pct+:
  Frequency: High (5.8 trades/day)
  Daily P&L: 4.2% per day
  Deployment: 🎯 Good for active trading
```

## 🎯 Knob 3: Priority-Based Research

### The Strategy: RVOL Inside Top Buckets

Your plan is solid:
1. **Gap buckets first** (broad filter)
2. **Apply RVOL inside top buckets** (refinement)

### Implementation Approach:

#### Phase 1: Quick Wins (Same Day)
```bash
# Start with highest-value buckets
python -m collector.cli fetch-minutes-optimized --threshold 15pct
python -m collector.cli fetch-minutes-optimized --threshold 10pct

# Immediate backtest results
python -m backtest.cli --start 2021-01-01 --end 2026-02-01
```

#### Phase 2: Complete Dataset (Weekend)
```bash
# All buckets by priority
python -m collector.cli fetch-minutes-optimized

# Full analysis
python -m backtest.cli --start 2021-01-01 --end 2026-02-01
```

#### Phase 3: RVOL Refinement
```python
# Inside top buckets, add RVOL filter
def add_rvol_filter(candidates, rvol_threshold=2.0):
    """Add RVOL filter to existing candidates."""
    candidates['rvol'] = candidates['volume'] / candidates['avg_volume_20d']
    return candidates[candidates['rvol'] >= rvol_threshold]
```

## 📈 Expected Impact on Results

### Before vs After:

#### Fake Data (Baseline):
```
15pct+: 90% win rate, 2.5% expectancy, 0.2 trades/day
10pct+: 85% win rate, 2.1% expectancy, 1.1 trades/day
7pct+: 80% win rate, 1.8% expectancy, 3.4 trades/day
```

#### Real Data (Expected):
```
15pct+: 70-80% win rate, 1.5-2.0% expectancy, 0.3-0.5 trades/day
10pct+: 65-75% win rate, 1.2-1.8% expectancy, 1.5-2.5 trades/day
7pct+: 60-70% win rate, 1.0-1.5% expectancy, 4.0-6.0 trades/day
```

### Why Real Data Will Be Different:

1. **Slippage**: Real fills worse than theoretical
2. **Partial Fills**: Large gaps may not fill completely
3. **Market Impact**: Your orders affect prices
4. **Liquidity Crunch**: High RVOL = crowded trades

## 🔧 Research Knob Tuning Guide

### Knob 1: Liquidity Thresholds
```bash
# Conservative (fewer, higher quality)
--min-price 5.0 --min-dollar-volume 20000000

# Moderate (balanced)
--min-price 2.0 --min-dollar-volume 10000000  # Default

# Aggressive (more opportunities)
--min-price 1.0 --min-dollar-volume 5000000
```

### Knob 2: Gap Bucket Selection
```bash
# High conviction, low frequency
--thresholds 0.15 0.10

# Balanced approach
--thresholds 0.10 0.07 0.05

# High frequency, lower conviction
--thresholds 0.07 0.05 0.03
```

### Knob 3: RVOL Threshold (Future)
```python
# Low RVOL (less crowded)
rvol_threshold = 1.5

# Moderate RVOL
rvol_threshold = 2.0  # Default expected

# High RVOL (momentum plays)
rvol_threshold = 3.0
```

## 🎯 Decision Framework

### When to Choose Each Bucket:

#### 15pct+ Bucket:
- **Use if**: Expectancy/Day > 1.0% AND you have patience
- **Skip if**: Trades/Day < 0.2 OR win rate < 60%
- **Capital**: Small, speculative allocation

#### 10pct+ Bucket:
- **Use if**: Expectancy/Day > 2.0% AND Trades/Day > 1.0
- **Skip if**: Opportunity rate < 80% OR continuation < 60%
- **Capital**: Primary trading bucket

#### 7pct+ Bucket:
- **Use if**: Expectancy/Day > 3.0% AND high frequency needed
- **Skip if**: Win rate < 55% OR high variance
- **Capital**: Larger, more active allocation

## 📊 Success Metrics

### Minimum Viable Thresholds:
```
Opportunity Rate: > 80%
Trades/Day: > 0.5
Win Rate: > 60%
Continuation Rate: > 65%
Expectancy/Day: > 1.0%
```

### Excellent Thresholds:
```
Opportunity Rate: > 90%
Trades/Day: > 1.0
Win Rate: > 70%
Continuation Rate: > 75%
Expectancy/Day: > 2.0%
```

## 🚀 Next Steps

1. **Run Stage A** with liquidity guardrails
2. **Generate candidates** from tradeable symbols only
3. **Download 10%+ bucket** first (quick wins)
4. **Analyze opportunity rates** and capital deployment
5. **Add RVOL filter** to best performing bucket
6. **Scale to full dataset** based on results

The key is **iterative refinement** - start broad, then apply increasingly sophisticated filters to the best performing buckets.
