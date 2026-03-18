# BankNifty Forecasting Tools Reference

## 🎯 Forecasting Tools Available

### 1. **LSTM Model** (Time Series Forecaster)
**Location:** [src/lstm_model.py](src/lstm_model.py)

**Class:** `LSTMModel`

**Features:**
- Bidirectional LSTM with 100+ timestep lookback
- Multivariate input: Close, Volume, Returns, Volatility, RSI, MACD
- Predicts next 5 candles with sanity checks
- ±3 ATR bounds enforcement

**Usage:**
```python
from src.lstm_model import LSTMModel

model = LSTMModel(lookback=100, forecast_steps=5)
predictions = model.predict_sequence(df)
# Returns: [price1, price2, price3, price4, price5]
```

---

### 2. **Options Forecaster** (Black-Scholes + IV Prediction)
**Location:** [src/options_forecaster.py](src/options_forecaster.py)

**Classes:**
- `BlackScholesOption` - Calculate option prices
- `OptionsForecastingModel` - Forecast IV and option prices

**Features:**
- Black-Scholes option pricing
- Implied volatility calculation (Newton-Raphson)
- Greeks calculation (Delta, Gamma, Theta, Vega)
- Multi-strike forecasting (ATM ± 200 points by default)

**Usage:**
```python
from src.options_forecaster import OptionsForecastingModel, BlackScholesOption

# Calculate call price
call_price = BlackScholesOption.call_price(
    S=54000,      # Current price
    K=54000,      # Strike
    T=0.05,       # Time to expiry (in years)
    r=0.06,       # Risk-free rate
    sigma=0.20    # Volatility
)

# Forecast option prices
forecaster = OptionsForecastingModel()
forecast = forecaster.forecast_options(
    underlying_price=54000,
    strikes_range=[53000, 53500, 54000, 54500, 55000],
    option_type="call",
    days_to_expiry=5
)
```

---

### 3. **Ensemble Forecaster** (Hybrid Model)
**Location:** [src/ensemble_forecaster.py](src/ensemble_forecaster.py)

**Features:**
- Combines LSTM + Options + Technical Analysis
- Weighted voting from multiple models
- Produces probability-weighted predictions
- Handles missing data gracefully

**Usage:**
```python
from src.ensemble_forecaster import EnsembleForecaster

ensemble = EnsembleForecaster()
prediction = ensemble.forecast(df, look_ahead=5)
# Returns: {
#   'direction': 'UP' or 'DOWN',
#   'confidence': 0.75,
#   'targets': [price1, price2, price3, ...]
# }
```

---

### 4. **CatBoost Forecaster** (Gradient Boosting)
**Location:** [src/catboost_model.py](src/catboost_model.py)

**Features:**
- CatBoost gradient boosting
- Handles categorical variables natively
- Fast training and inference
- Feature importance analysis

**Usage:**
```python
from src.catboost_model import CatBoostForecaster

forecaster = CatBoostForecaster()
forecaster.train(X_train, y_train)
predictions = forecaster.predict(X_test)
```

---

### 5. **Options Bid-Ask Collector** (Live Data + Mock Fallback)
**Location:** [src/options_bidask_collector.py](src/options_bidask_collector.py)

**Features:**
- Fetches live bid-ask from Upstox API
- 892 BankNifty options mapped (30+ expirations)
- Graceful fallback to realistic mock data
- Instrument key auto-lookup by strike

**Usage:**
```python
from src.options_bidask_collector import OptionsBidAskCollector
from src.upstox_auth import UpstoxAuth

auth = UpstoxAuth(api_key, api_secret)
collector = OptionsBidAskCollector(upstox_auth=auth)

bid_ask = collector.fetch_option_bidask(
    symbol="BANKNIFTY",
    strike=54000,
    expiry="2026-03-30",
    option_type="CE"
)
# Returns: {'bid': 53999.50, 'ask': 54000.50, 'ltp': 54000.00, 'source': 'live'}
```

---

## 🚀 Quick Start Commands

### 1. Launch Live Trading Dashboard
```bash
streamlit run app_with_live_data.py
```
- Real-time bid-ask monitor
- Live predictions
- Order book analysis
- Dynamic strike range (automatically adjusted to current spot ±500)

### 2. Test LSTM Predictions
```bash
python3 -c "
from src.lstm_model import LSTMModel
import pandas as pd

model = LSTMModel()
df = pd.read_csv('data/banknifty_historical.csv')  # Your data
predictions = model.predict_sequence(df.tail(100))
print(f'Next 5 prices: {predictions}')
"
```

### 3. Test Options Pricing
```bash
python3 -c "
from src.options_forecaster import BlackScholesOption

price = BlackScholesOption.call_price(S=54000, K=54000, T=0.05, r=0.06, sigma=0.20)
print(f'Call Price: {price}')
"
```

### 4. Collect Live Bid-Ask Data
```bash
python3 update_and_test_token.py
```
- Updates token cache
- Tests live API connection
- Verifies system operational

---

## 📊 Strike Range Update (NEW!)

**Previous:** Hardcoded 40,000 - 60,000

**Now:** Dynamically calculated based on current spot price
- **Min Strike:** Current Price - 2,000 (with floor at 40,000)
- **Max Strike:** Current Price + 2,000 (with ceil at 70,000)
- **Default Range:** ATM ± 500
- **Display:** Current spot price shown in dashboard

Example with spot = 54,000:
- Min: 52,000
- Max: 56,000
- Default: 53,500 - 54,500

---

## 🔧 Architecture Overview

```
┌─────────────────────────────────────────┐
│   Live Trading Dashboard (Streamlit)    │
│  ├─ Real-time Bid-Ask Monitor           │
│  ├─ Live Predictions                    │
│  └─ Order Book Analysis                 │
└──────────────┬──────────────────────────┘
               │
    ┌──────────┼──────────┐
    │          │          │
    ▼          ▼          ▼
┌────────┐ ┌────────┐ ┌────────┐
│ Options│ │ LSTM   │ │Ensemble│
│Forecst │ │ Model  │ │Model   │
└────────┘ └────────┘ └────────┘
    ▲          ▲          ▲
    └──────────┼──────────┘
               │
    ┌──────────┴──────────┐
    │                     │
    ▼                     ▼
┌─────────────┐  ┌──────────────────┐
│ Upstox API  │  │ Historical Data   │
│ (Live Data) │  │ (Training)        │
└─────────────┘  └──────────────────┘
```

---

## 📈 Current System Status

| Component | Status | Path |
|-----------|--------|------|
| **Auth** | ✅ Token Valid (13.6 hours) | `data/.upstox_token_cache.json` |
| **Collector** | ✅ Live API Ready | `src/options_bidask_collector.py` |
| **LSTM** | ✅ Trained Model Loaded | `data/lstm_model.keras` |
| **Options Forecaster** | ✅ Ready | `src/options_forecaster.py` |
| **Ensemble** | ✅ Ready | `src/ensemble_forecaster.py` |
| **CatBoost** | ✅ Ready | `src/catboost_model.py` |
| **Dashboard** | ✅ Live Updated | `app_with_live_data.py` |

---

## 🎯 Next Steps

1. **Launch Dashboard** (when markets are open):
   ```bash
   streamlit run app_with_live_data.py
   ```

2. **Monitor Dynamic Strikes:**
   - Dashboard shows current spot price
   - Range auto-adjusts to ATM ± 500
   - Slider shows full available range (ATM ± 2000)

3. **Test Forecasting:**
   - Review LSTM predictions in "Predictions" tab
   - Compare with actual market movement
   - Measure accuracy improvement

---

**Updated:** 2026-03-02 02:50 UTC  
**Status:** 🚀 Production Ready
