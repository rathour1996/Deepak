# Signal Mismatch Analysis & Root Causes

## Issues Identified:

### 1. **Forecast-to-Signal Mapping Issue**
**Problem**: Using only `forecast_path[0]` (first 1 candle ahead) to generate signal
- Forecast_path contains 5 candles ahead
- First point might be nearly equal to current price (0.1% threshold = too tight)
- **Result**: Signal stays "WAIT" even though overall trend is bullish

**Location**: `app.py` line 695-697
```python
next_price = forecast_path[0] if forecast_path else None  # ❌ Only looks 1 step ahead
signal = generate_signals(df, next_price, option_chain_df)
```

### 2. **Trend Calculation Flaw**
**Problem**: Using fixed slopes (50/-50 points) for trend strength
- For BankNifty (trading in 40,000+ range), 50 points is only 0.12% move
- This is too sensitive/arbitrary
- **Result**: "Strong Up" vs "Strong Down" depends on 5-candle slope, not actual forecast direction

**Location**: `app.py` line 762-768
```python
slope = forecast_path[-1] - forecast_path[0]
if slope > 50:
    trend_strength = "Strong Up"  # ❌ Arbitrary threshold
elif slope < -50:
    trend_strength = "Strong Down"
```

### 3. **Signal Logic Flaw**
**Problem**: Threshold 1.001 (0.1%) is too tight
- If forecast_path[0] = 44,100 and current = 44,098, no signal triggered
- PCR and Supertrend might be bullish, but LSTM override prevents combined signal

**Location**: `src/strategy.py` line 81-88
```python
if lstm_price > current_price * 1.001:  # ❌ Only 0.1% threshold
    if signal['action'] == 'WAIT': signal['action'] = 'BULLISH_EARLY'
```

### 4. **Predict-First-Point Logic**
**Problem**: Passing only `forecast_path[0]` to signal generator
-Better to use DIRECTION of entire forecast path (slope)
- Or average of 2-3 first points to smooth noise

### 5. **Data Staleness**
**Problem**: Model trained on historical data, but market is live
- Training data captured at different market conditions
- Model features might not capture current volatility

### 6. **Feature Engineering Issues in CatBoost**
**Problem**: Lag features might be using stale prices
- `close_lag_1` through `close_lag_40` might be artificially dampening predictions
- RSI, SMA/EMA might be whipping in volatile markets

---

## Impact:
- ✗ Supertrend says BUY (trend reversed, technical analysis confirmed)
- ✗ AI Forecast says DOWN (but price moving UP - model is wrong)
- ✗ Execution Panel says WAIT (conflicting signals)
- ✗ CatBoost/LSTM not aligned with reality

---

## Root Cause Priority:
1. **HIGH**: Forecast path direction vs actual market movement (Model accuracy issue)
2. **HIGH**: Signal generation logic using only 1st point vs full forecast direction
3. **HIGH**: Trend strength threshold is arbitrary (50 points)
4. **MEDIUM**: LSTM threshold too tight (0.1%)
5. **MEDIUM**: Feature engineering needs recalibration
