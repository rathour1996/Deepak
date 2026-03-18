"""
BankNifty Forecast Bot with Live Upstox Bid-Ask Integration
Real-time option pricing with ensemble forecasting
"""

import os
os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo
from pathlib import Path
import json
import time as pytime
import threading
from uuid import uuid4

# Import Upstox and bidask collector
from src.upstox_auth import UpstoxAuth
from src.options_bidask_collector import OptionsBidAskCollector, build_bidask_features
from src.nse_data import fetch_nse_index_data
from src.upstox_data import fetch_upstox_index_ohlc, fetch_upstox_option_chain_ltp_timeseries
from src.strategy import calculate_supertrend
from src.data import load_option_ltp_data, save_option_ltp_data
from src.banknifty_forecaster import forecast_banknifty_direct
from src.options_ltp_integration import _clean_option_series_5m, _align_series_to_index
from src.options_lstm_forecaster import OptionsLSTMForecaster
from src.options_microstructure_forecaster import OptionsMicrostructureForecaster
from src.nhits_model import NHITSForecaster
from src.ltp_forecaster_improved import forecast_option_ltp_improved
from src.dlinear_forecaster import DLinearForecaster

IST = ZoneInfo("Asia/Kolkata")
FORECAST_STEPS = 24
FORECAST_HORIZON_LABEL = "2h"

def _to_ist_timestamp(value):
    """Normalize timestamp-like input to tz-aware IST timestamp."""
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return pd.NaT
    try:
        if getattr(ts, "tzinfo", None) is None:
            return ts.tz_localize(IST)
        return ts.tz_convert(IST)
    except Exception:
        return pd.NaT


MARKET_OPEN_TIME = dt_time(9, 15)
MARKET_CLOSE_TIME = dt_time(15, 30)


def _is_market_open(ts: datetime | None = None) -> bool:
    ts = _to_ist_timestamp(ts or datetime.now(IST))
    if pd.isna(ts):
        return False
    if ts.weekday() >= 5:
        return False
    current_time = ts.time()
    return (current_time >= MARKET_OPEN_TIME) and (current_time <= MARKET_CLOSE_TIME)


# ============================================================================
# DATA STORAGE - Local caching for historical prices
# ============================================================================
# Use absolute path to ensure it works from any working directory
SCRIPT_DIR = Path(__file__).parent.absolute()
DATA_DIR = SCRIPT_DIR / "data" / "historical_prices"
DATA_DIR.mkdir(parents=True, exist_ok=True)
_CACHE_WRITE_LOCK = threading.Lock()
FORECAST_HISTORY_PATH = SCRIPT_DIR / "data" / "ltp_forecast_history.json"


def _load_forecast_history():
    if "ltp_forecast_history" in st.session_state and st.session_state["ltp_forecast_history"]:
        return
    if not FORECAST_HISTORY_PATH.exists():
        st.session_state["ltp_forecast_history"] = {}
        return
    try:
        payload = json.loads(FORECAST_HISTORY_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            cleaned = {}
            for key, entries in payload.items():
                if not isinstance(entries, list):
                    continue
                cleaned_list = [
                    {"anchor": e.get("anchor"), "values": e.get("values")}
                    for e in entries
                    if isinstance(e, dict)
                ]
                cleaned[key] = cleaned_list
            st.session_state["ltp_forecast_history"] = cleaned
            return
    except Exception:
        pass
    st.session_state["ltp_forecast_history"] = {}


def _save_forecast_history(history: dict):
    try:
        FORECAST_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        FORECAST_HISTORY_PATH.write_text(
            json.dumps(history, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"⚠️ Unable to persist forecast history: {e}")


def _forecast_history_key(strike, option_type: str, model: str, expiry: str | None = None) -> str:
    expiry_tag = str(expiry) if expiry else "unknown"
    return f"{int(round(float(strike)))}|{str(option_type).upper()}|{str(model).upper()}|{expiry_tag}"


def _legacy_forecast_key(strike, option_type: str, model: str) -> str:
    return f"{int(round(float(strike)))}|{str(option_type).upper()}|{str(model).upper()}"


def _get_model_cache(key: str, ttl_sec: int = 900):
    cache = st.session_state.setdefault("ltp_model_cache", {})
    entry = cache.get(key)
    if not entry:
        return None
    try:
        ts = float(entry.get("ts", 0.0))
        if (pytime.time() - ts) <= float(ttl_sec):
            return entry
    except Exception:
        pass
    return None


def _set_model_cache(key: str, model, metrics: dict, length: int):
    cache = st.session_state.setdefault("ltp_model_cache", {})
    cache[key] = {
        "ts": float(pytime.time()),
        "model": model,
        "metrics": metrics or {},
        "length": int(length),
    }


def _get_selector_cache(key: str, ttl_sec: int = 1200):
    cache = st.session_state.setdefault("ltp_selector_cache", {})
    entry = cache.get(key)
    if not entry:
        return None
    try:
        if (pytime.time() - float(entry.get("ts", 0.0))) <= float(ttl_sec):
            return entry.get("payload")
    except Exception:
        pass
    return None


def _set_selector_cache(key: str, payload: dict):
    cache = st.session_state.setdefault("ltp_selector_cache", {})
    cache[key] = {
        "ts": float(pytime.time()),
        "payload": payload or {},
    }


def _seconds_until_next_ist_tick(interval_seconds: int):
    """Return seconds until next exact IST-aligned tick."""
    interval = max(int(interval_seconds), 1)
    now_ist = datetime.now(IST)
    rem = now_ist.timestamp() % interval
    sleep_for = interval - rem
    if sleep_for < 0.05:
        sleep_for = interval
    return float(sleep_for), now_ist


def _interval_to_timedelta(interval_value: str) -> timedelta:
    """Convert app interval token to timedelta."""
    try:
        token = str(interval_value).strip().lower()
        if token.endswith("m"):
            return timedelta(minutes=max(int(float(token[:-1])), 1))
        if token.endswith("h"):
            return timedelta(hours=max(int(float(token[:-1])), 1))
        if token.endswith("d"):
            return timedelta(days=max(int(float(token[:-1])), 1))
    except Exception:
        pass
    return timedelta(minutes=5)


def _build_ai_forecast(series_like, steps: int = 12, anchor_price: float = np.nan):
    """
    Adaptive AI-style deterministic forecast using momentum + mean reversion.
    Returns median/lower/upper forecast arrays.
    """
    steps = max(int(steps), 1)
    series = pd.to_numeric(pd.Series(series_like), errors="coerce").dropna()
    if series.empty:
        return None

    # Primary independent model: direct multi-horizon GBRT with residual calibration.
    try:
        direct_out = forecast_banknifty_direct(series, steps=steps)
    except Exception as _bnf_err:
        print(f"⚠️ BankNifty direct forecast failed: {_bnf_err}")
        direct_out = None
    if direct_out:
        if np.isfinite(anchor_price):
            # Light anchor blend keeps path synced with latest live quote when present.
            med = np.asarray(direct_out["median"], dtype=float)
            lo = np.asarray(direct_out["lower"], dtype=float)
            hi = np.asarray(direct_out["upper"], dtype=float)
            t = np.arange(1, len(med) + 1, dtype=float)
            w = 0.16 * (t / max(len(med), 1))
            med = (1.0 - w) * med + w * float(anchor_price)
            lo = (1.0 - w) * lo + w * (float(anchor_price) * 0.998)
            hi = (1.0 - w) * hi + w * (float(anchor_price) * 1.002)
            direct_out["median"] = med.tolist()
            direct_out["lower"] = lo.tolist()
            direct_out["upper"] = hi.tolist()
        return {
            "median": direct_out["median"],
            "lower": direct_out["lower"],
            "upper": direct_out["upper"],
            "direction": direct_out.get("direction", "FLAT"),
            "confidence": float(direct_out.get("confidence", 45.0)),
            "model": str(direct_out.get("model", "banknifty_direct_gbr_conformal")),
            "metrics": direct_out.get("metrics", {}),
        }

    last_price = float(series.iloc[-1])
    if len(series) < 3:
        median = [last_price] * steps
        lower = [last_price * 0.997] * steps
        upper = [last_price * 1.003] * steps
        return {
            "median": median,
            "lower": lower,
            "upper": upper,
            "direction": "FLAT",
            "confidence": 35.0,
        }

    returns = series.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    r1 = float(returns.tail(1).mean()) if not returns.empty else 0.0
    r5 = float(returns.tail(min(5, len(returns))).mean()) if not returns.empty else 0.0
    r20 = float(returns.tail(min(20, len(returns))).mean()) if not returns.empty else 0.0
    vol = float(returns.tail(min(30, len(returns))).std()) if not returns.empty else 0.0
    if not np.isfinite(vol):
        vol = 0.0
    vol = max(vol, 0.0002)

    drift = (0.50 * r1) + (0.30 * r5) + (0.20 * r20)
    max_step = max(3.0 * vol, 0.0008)
    ema_anchor = float(series.ewm(span=min(20, len(series)), adjust=False).mean().iloc[-1])

    median = []
    lower = []
    upper = []
    prev = last_price

    for i in range(steps):
        decay = float(np.exp(-0.08 * i))
        reversion = ((ema_anchor - prev) / max(prev, 1e-9)) * 0.08
        step_ret = np.clip((drift * decay) + reversion, -max_step, max_step)

        next_price = float(max(prev * (1.0 + step_ret), 0.01))
        if np.isfinite(anchor_price):
            blend = 0.22 * ((i + 1) / steps)
            next_price = float((1.0 - blend) * next_price + blend * float(anchor_price))

        median.append(next_price)
        band_pct = float(np.clip(max(vol * 1.8 * np.sqrt(i + 1), 0.002), 0.002, 0.03))
        lower.append(next_price * (1.0 - band_pct))
        upper.append(next_price * (1.0 + band_pct))
        prev = next_price

    final_pct = (median[-1] - last_price) / max(last_price, 1e-9)
    if final_pct > 0.002:
        direction = "UP"
    elif final_pct < -0.002:
        direction = "DOWN"
    else:
        direction = "FLAT"
    confidence = float(np.clip(100.0 / (1.0 + (vol * 250.0)), 35.0, 90.0))

    return {
        "median": median,
        "lower": lower,
        "upper": upper,
        "direction": direction,
        "confidence": confidence,
    }


def _update_ltp_history_snapshot(
    strike: int,
    ce_ltp: float,
    pe_ltp: float,
    ce_source: str = "",
    pe_source: str = "",
):
    """Append latest CE/PE LTP snapshot per strike into session history."""
    if "ltp_history" not in st.session_state:
        st.session_state.ltp_history = {}

    stamp = datetime.now(IST).replace(second=0, microsecond=0).isoformat()
    strike_key = int(round(float(strike)))
    max_reasonable_ltp = max(50.0, float(strike_key) * 0.5)
    strike_pack = st.session_state.ltp_history.setdefault(strike_key, {"CE": [], "PE": []})

    def _upsert_leg(leg: str, ltp_value, source=""):
        val = pd.to_numeric(ltp_value, errors="coerce")
        src = str(source or "").lower()
        # Do not learn from synthetic/mock ticks; they poison intraday path.
        if src.startswith("mock"):
            return
        if (not np.isfinite(val)) or float(val) <= 0 or float(val) > max_reasonable_ltp:
            return
        points = strike_pack.setdefault(leg, [])
        if points and points[-1][0] == stamp:
            points[-1] = [stamp, float(val)]
        else:
            points.append([stamp, float(val)])
        if len(points) > 720:
            del points[:-720]

    _upsert_leg("CE", ce_ltp, ce_source)
    _upsert_leg("PE", pe_ltp, pe_source)


def _get_ltp_history_series(strike: int, leg: str) -> pd.Series:
    """Return historical LTP series for strike/leg from session state."""
    hist_map = st.session_state.get("ltp_history", {})
    strike_pack = hist_map.get(int(round(float(strike))), {})
    points = strike_pack.get(str(leg).upper(), [])
    if not points:
        return pd.Series(dtype=float)

    frame = pd.DataFrame(points, columns=["Time", "LTP"])
    frame["Time"] = frame["Time"].apply(_to_ist_timestamp)
    frame["LTP"] = pd.to_numeric(frame["LTP"], errors="coerce")
    strike_key = int(round(float(strike)))
    max_reasonable_ltp = max(50.0, float(strike_key) * 0.5)
    frame = frame[(frame["LTP"] > 0) & (frame["LTP"] <= max_reasonable_ltp)]
    frame = frame.dropna(subset=["Time", "LTP"]).drop_duplicates(subset=["Time"], keep="last").sort_values("Time")
    if frame.empty:
        return pd.Series(dtype=float)

    series = pd.Series(frame["LTP"].astype(float).values, index=pd.DatetimeIndex(frame["Time"]))
    series = series[~series.index.duplicated(keep="last")].sort_index()
    return series


def _coerce_series_to_ist(series_like) -> pd.Series:
    """Coerce arbitrary series/index into numeric values with tz-aware IST datetime index."""
    series = pd.to_numeric(pd.Series(series_like), errors="coerce").dropna()
    if series.empty:
        return pd.Series(dtype=float)

    idx = [_to_ist_timestamp(x) for x in series.index]
    out = pd.Series(series.values, index=pd.DatetimeIndex(idx))
    out = out[~out.index.isna()].dropna()
    if out.empty:
        return pd.Series(dtype=float)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out.astype(float)


def _ensure_min_history(series_like, current_value: float, min_points: int = 25) -> pd.Series:
    """Ensure option LTP history has enough points for forecast validation."""
    series = _coerce_series_to_ist(series_like)
    if not np.isfinite(current_value):
        return series
    if len(series) >= min_points:
        return series
    needed = int(max(min_points - len(series), 0))
    if needed <= 0:
        return series
    end_time = datetime.now(IST)
    if isinstance(series.index, pd.DatetimeIndex) and len(series.index):
        last_time = _to_ist_timestamp(series.index[-1])
        if pd.notna(last_time):
            end_time = last_time + timedelta(minutes=1)
    filler_index = pd.date_range(end=end_time, periods=needed, freq="1min")
    filler = pd.Series([float(current_value)] * needed, index=filler_index)
    return _coerce_series_to_ist(pd.concat([series, filler], axis=0))


def _merge_series_ist(*series_list) -> pd.Series:
    """Merge multiple series into one IST-indexed numeric series."""
    merged = pd.Series(dtype=float)
    for s in series_list:
        part = _coerce_series_to_ist(s)
        if part.empty:
            continue
        if merged.empty:
            merged = part
        else:
            merged = pd.concat([merged, part], axis=0)
            merged = _coerce_series_to_ist(merged)
    return merged


def _coerce_option_frame_ist(frame_like) -> pd.DataFrame:
    frame = pd.DataFrame(frame_like).copy()
    if frame.empty:
        return pd.DataFrame()
    if "Time" in frame.columns:
        idx = pd.to_datetime(frame["Time"], errors="coerce")
        frame = frame.drop(columns=["Time"])
    else:
        idx = pd.to_datetime(frame.index, errors="coerce")
    valid = ~pd.isna(idx)
    frame = frame.loc[valid].copy()
    idx = idx[valid]
    if len(idx) == 0:
        return pd.DataFrame()
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize(IST)
    else:
        idx = idx.tz_convert(IST)
    frame.index = idx
    for col in frame.columns:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def _series_to_option_frame(series_like) -> pd.DataFrame:
    series = _coerce_series_to_ist(series_like)
    if series.empty:
        return pd.DataFrame()
    return pd.DataFrame(
        {
            "Open": series.values,
            "High": series.values,
            "Low": series.values,
            "Close": series.values,
            "Volume": np.nan,
            "OI": np.nan,
        },
        index=series.index,
    )


def _merge_option_frames_ist(*frames) -> pd.DataFrame:
    merged = pd.DataFrame()
    for frame in frames:
        part = _coerce_option_frame_ist(frame)
        if part.empty:
            continue
        if merged.empty:
            merged = part
        else:
            merged = pd.concat([merged, part], axis=0)
            merged = _coerce_option_frame_ist(merged)
    return merged


def _append_current_ltp_to_frame(frame_like, current_value: float) -> pd.DataFrame:
    frame = _coerce_option_frame_ist(frame_like)
    if not np.isfinite(current_value):
        return frame
    now = _to_ist_timestamp(datetime.now(IST)).floor("1min")
    row = pd.DataFrame(
        {
            "Open": [current_value],
            "High": [current_value],
            "Low": [current_value],
            "Close": [current_value],
        },
        index=pd.DatetimeIndex([now]),
    )
    return _merge_option_frames_ist(frame, row)


def _slice_hist_for_plot(series: pd.Series, cutoff_ts: pd.Timestamp, fallback_points: int = 600) -> pd.Series:
    """Safely slice history by datetime cutoff; handles empty/non-datetime indexes."""
    s = pd.to_numeric(pd.Series(series), errors="coerce").dropna()
    if s.empty:
        return pd.Series(dtype=float)
    if isinstance(s.index, pd.DatetimeIndex):
        try:
            return s[s.index >= cutoff_ts]
        except Exception:
            pass
    return s.tail(int(max(1, fallback_points)))


def _resolve_chain_ltp(auth_instance, strike: float, option_type: str, expiry: str) -> float:
    """Fetch latest LTP from option chain snapshot for a strike/leg."""
    if auth_instance is None or not getattr(auth_instance, "access_token", None):
        return np.nan
    try:
        from src.upstox_data import fetch_upstox_option_chain
        chain_df, _ = fetch_upstox_option_chain(
            auth_instance=auth_instance,
            symbol="BANKNIFTY",
            expiry=expiry,
        )
        if chain_df is None or chain_df.empty:
            return np.nan
        strike_df = chain_df[
            (chain_df["Strike"].astype(float) == float(strike))
            & (chain_df["Type"].astype(str).str.upper() == str(option_type).upper())
        ]
        if strike_df.empty:
            strike_df = chain_df[
                (chain_df["Strike"].astype(float) - float(strike)).abs() < 0.5
            ]
            strike_df = strike_df[
                strike_df["Type"].astype(str).str.upper() == str(option_type).upper()
            ]
        if strike_df.empty:
            return np.nan
        return float(pd.to_numeric(strike_df.iloc[0].get("Price", np.nan), errors="coerce"))
    except Exception:
        return np.nan


def _fetch_chain_snapshot(auth_instance, strike: float, expiry: str) -> dict:
    """Fetch CE/PE snapshot from option chain for a strike."""
    if auth_instance is None or not getattr(auth_instance, "access_token", None):
        return {}
    try:
        from src.upstox_data import fetch_upstox_option_chain
        chain_df, _ = fetch_upstox_option_chain(
            auth_instance=auth_instance,
            symbol="BANKNIFTY",
            expiry=expiry,
        )
        if chain_df is None or chain_df.empty:
            return {}

        def _pick_leg(leg):
            leg_df = chain_df[
                (chain_df["Strike"].astype(float) == float(strike))
                & (chain_df["Type"].astype(str).str.upper() == leg)
            ]
            if leg_df.empty:
                leg_df = chain_df[
                    (chain_df["Strike"].astype(float) - float(strike)).abs() < 0.5
                ]
                leg_df = leg_df[leg_df["Type"].astype(str).str.upper() == leg]
            if leg_df.empty:
                return {}
            row = leg_df.iloc[0]
            payload = {}
            for col in leg_df.columns:
                if str(col).lower() == "type":
                    continue
                payload[col] = row.get(col)
            return payload

        return {"CE": _pick_leg("CE"), "PE": _pick_leg("PE")}
    except Exception:
        return {}


def _snapshot_to_static_features(snapshot: dict, prefix: str = "chain") -> dict:
    """Convert chain snapshot values to numeric static feature dict."""
    out = {}
    if not snapshot:
        return out
    for key, value in snapshot.items():
        try:
            numeric = float(pd.to_numeric(value, errors="coerce"))
        except Exception:
            numeric = np.nan
        if np.isfinite(numeric):
            clean_key = str(key).strip().lower()
            out[f"{prefix}_{clean_key}"] = numeric
    return out


def _constant_series_from_snapshot(value, target_index):
    """Create a constant series aligned to target index."""
    if not np.isfinite(value):
        return None
    if target_index is None or len(target_index) == 0:
        return pd.Series([value])
    return pd.Series([value] * len(target_index), index=target_index)


def _append_current_ltp(history: pd.Series, current_value: float) -> pd.Series:
    """Append current LTP as latest point to anchor forecasts."""
    if not np.isfinite(current_value):
        return history
    now = datetime.now(IST)
    series = _coerce_series_to_ist(history)
    if series.empty:
        return pd.Series([current_value], index=pd.DatetimeIndex([now]))
    last_val = float(series.iloc[-1])
    if np.isfinite(last_val) and abs(last_val - current_value) <= max(current_value * 0.02, 2.0):
        return series
    appended = pd.concat([series, pd.Series([current_value], index=pd.DatetimeIndex([now]))], axis=0)
    return _coerce_series_to_ist(appended)


def _select_training_window_days(series: pd.Series, candidates=None, min_points: int = 60) -> int:
    """Pick a training window (days) that minimizes short-horizon MAE."""
    if candidates is None:
        candidates = [3, 5, 8, 13, 21, 30]
    s = _coerce_series_to_ist(series)
    if s.empty or not isinstance(s.index, pd.DatetimeIndex):
        return max(candidates)
    best_days = candidates[0]
    best_mae = None
    for days in candidates:
        cutoff = datetime.now(IST) - timedelta(days=int(days))
        sub = s[s.index >= cutoff]
        if len(sub) < min_points:
            continue
        vals = pd.to_numeric(sub, errors="coerce").dropna()
        if len(vals) < 3:
            continue
        pred = vals.shift(1).dropna()
        actual = vals.loc[pred.index]
        mae = float((actual - pred).abs().mean())
        if best_mae is None or mae < best_mae - 1e-6 or (abs(mae - best_mae) < 1e-6 and days > best_days):
            best_mae = mae
            best_days = days
    return int(best_days)


def _build_ltp_forecast_plot(
    history: pd.Series,
    forecast_values,
    title: str,
    color: str,
    past_forecasts=None,
    step_minutes: int = 5,
    forecast_lower=None,
    forecast_upper=None,
):
    """Render LTP history + forecast plotly figure."""
    hist = pd.to_numeric(pd.Series(history), errors="coerce").dropna()
    forecast = pd.to_numeric(pd.Series(forecast_values), errors="coerce").dropna().tolist()
    fig = go.Figure()
    last_time = datetime.now(IST)
    recent_ohlc = None
    if isinstance(hist.index, pd.DatetimeIndex) and len(hist.index):
        hist = hist[~hist.index.duplicated(keep="last")].sort_index()
        last_time = _to_ist_timestamp(hist.index[-1]).floor("1min")
        bucket = hist.index.floor(f"{step_minutes}min")
        ohlc = (
            pd.DataFrame({"bucket": bucket, "ltp": hist.values})
            .groupby("bucket")["ltp"]
            .agg(["first", "max", "min", "last"])
        )
        if not ohlc.empty:
            recent_ohlc = ohlc.tail(60)
            # Expand flat candles using recent volatility so high/low aren't identical.
            spread = float(pd.to_numeric(hist.diff().abs().tail(30).mean(), errors="coerce")) if len(hist) > 3 else 0.0
            min_wick = max(spread, float(hist.tail(30).std()) if len(hist) >= 5 else 0.0, float(hist.iloc[-1]) * 0.002, 0.5)
            flat_mask = (ohlc["max"] - ohlc["min"]).abs() < 1e-6
            if flat_mask.any():
                ohlc.loc[flat_mask, "max"] = ohlc.loc[flat_mask, "last"] + min_wick
                ohlc.loc[flat_mask, "min"] = ohlc.loc[flat_mask, "last"] - min_wick
            fig.add_trace(
                go.Candlestick(
                    x=ohlc.index,
                    open=ohlc["first"],
                    high=ohlc["max"],
                    low=ohlc["min"],
                    close=ohlc["last"],
                    name="LTP Candles",
                    increasing_line_color="#10b981",
                    decreasing_line_color="#ef4444",
                    showlegend=False,
                )
            )
            ema = hist.ewm(span=12, adjust=False).mean()
            fig.add_trace(
                go.Scatter(
                    x=ema.index,
                    y=ema.values,
                    mode="lines",
                    name="Trend (EMA)",
                    line=dict(color="#f59e0b", width=2),
                )
            )
        else:
            fig.add_trace(
                go.Scatter(
                    x=hist.index,
                    y=hist.values,
                    mode="lines+markers",
                    name="History",
                    line=dict(color=color, width=2),
                    marker=dict(size=4),
                )
            )
    if past_forecasts:
        for entry in past_forecasts:
            anchor = _to_ist_timestamp(entry.get("anchor"))
            values = pd.to_numeric(pd.Series(entry.get("values", [])), errors="coerce").dropna().tolist()
            if pd.isna(anchor) or not values:
                continue
            dotted_times = pd.date_range(
                start=anchor + timedelta(minutes=step_minutes),
                periods=len(values),
                freq=f"{step_minutes}min",
                tz=IST,
            )
            fig.add_trace(
                go.Scatter(
                    x=dotted_times,
                    y=values,
                    mode="lines",
                    name="Prior Forecast",
                    line=dict(color="rgba(148, 163, 184, 0.7)", width=2, dash="dot"),
                    showlegend=False,
                )
            )

    if forecast:
        forecast_times = pd.date_range(
            start=last_time + timedelta(minutes=step_minutes),
            periods=len(forecast),
            freq=f"{step_minutes}min",
            tz=IST,
        )
        lower = pd.to_numeric(pd.Series(forecast_lower), errors="coerce").dropna().tolist() if forecast_lower is not None else []
        upper = pd.to_numeric(pd.Series(forecast_upper), errors="coerce").dropna().tolist() if forecast_upper is not None else []
        if not (lower and upper and len(lower) == len(forecast) and len(upper) == len(forecast)):
            lower = []
            upper = []
            if len(hist) >= 3:
                recent = pd.to_numeric(hist.diff().abs().tail(60), errors="coerce").dropna()
                sigma = float(recent.mean()) if not recent.empty else 0.0
            else:
                sigma = 0.0
            if sigma <= 0 and forecast:
                sigma = max(float(pd.Series(forecast).std()), float(pd.Series(forecast).mean()) * 0.002, 0.5)
            for value in forecast:
                lower.append(value - sigma)
                upper.append(value + sigma)

        forecast_open = []
        if len(hist):
            last_close = float(pd.to_numeric(hist.iloc[-1], errors="coerce"))
        else:
            last_close = float(forecast[0])
        for idx, value in enumerate(forecast):
            forecast_open.append(last_close if idx == 0 else float(forecast[idx - 1]))

        wick_base = float(pd.Series(hist).diff().abs().tail(30).mean()) if len(hist) > 3 else 0.0
        min_wick = max(wick_base, float(pd.Series(forecast).std()) if len(forecast) >= 3 else 0.0, float(forecast[0]) * 0.002, 0.5)
        candle_high = []
        candle_low = []
        for idx, value in enumerate(forecast):
            high = max(forecast_open[idx], value, upper[idx])
            low = min(forecast_open[idx], value, lower[idx])
            if abs(high - low) < min_wick:
                high += min_wick / 2
                low -= min_wick / 2
            candle_high.append(high)
            candle_low.append(low)

        fig.add_trace(
            go.Candlestick(
                x=forecast_times,
                open=forecast_open,
                high=candle_high,
                low=candle_low,
                close=forecast,
                name="Forecast Candles",
                increasing_line_color="#22d3ee",
                decreasing_line_color="#0ea5e9",
                showlegend=False,
            )
        )
        fig.add_trace(
            go.Scatter(
                x=forecast_times,
                y=forecast,
                mode="lines+markers",
                name="Forecast",
                line=dict(color="#22d3ee", width=3),
                marker=dict(size=6, color="#22d3ee"),
            )
        )
        if lower and upper and len(lower) == len(forecast) and len(upper) == len(forecast):
            band_times = list(forecast_times) + list(forecast_times[::-1])
            fig.add_trace(
                go.Scatter(
                    x=band_times,
                    y=upper + lower[::-1],
                    fill="toself",
                    fillcolor="rgba(34, 211, 238, 0.18)",
                    line=dict(color="rgba(255,255,255,0)"),
                    hoverinfo="skip",
                    showlegend=False,
                    name="Forecast Band",
                )
            )
    fig.update_layout(
        height=320,
        template="plotly_white",
        title=title,
        xaxis_title="Time (IST)",
        yaxis_title="LTP (₹)",
        hovermode="x unified",
        margin=dict(l=30, r=20, t=40, b=30),
    )
    # Tighten y-axis to recent visible candles + forecast
    try:
        recent_vals = []
        if recent_ohlc is not None and not recent_ohlc.empty:
            recent_vals += pd.to_numeric(recent_ohlc["max"], errors="coerce").dropna().tolist()
            recent_vals += pd.to_numeric(recent_ohlc["min"], errors="coerce").dropna().tolist()
        elif len(hist):
            recent_vals += pd.to_numeric(hist.tail(60), errors="coerce").dropna().tolist()
        if forecast:
            recent_vals += list(pd.to_numeric(pd.Series(forecast), errors="coerce").dropna().values)
        if lower and upper:
            recent_vals += lower + upper
        if recent_vals:
            y_min = float(np.nanmin(recent_vals))
            y_max = float(np.nanmax(recent_vals))
            pad = max((y_max - y_min) * 0.08, max(abs(y_min), abs(y_max)) * 0.005, 1.0)
            fig.update_yaxes(range=[y_min - pad, y_max + pad], autorange=False)
    except Exception:
        pass
    return fig


def _refine_forecast_bands(history: pd.Series, result: dict) -> dict:
    if result is None:
        return result
    vals = None
    try:
        vals = result.get("forecast")
    except Exception:
        return result
    if vals is None:
        return result
    try:
        if len(vals) == 0:
            return result
    except Exception:
        return result
    forecast = pd.to_numeric(pd.Series(result.get("forecast", [])), errors="coerce").dropna().tolist()
    if not forecast:
        return result
    lower = pd.to_numeric(pd.Series(result.get("lower_band", [])), errors="coerce").dropna().tolist()
    upper = pd.to_numeric(pd.Series(result.get("upper_band", [])), errors="coerce").dropna().tolist()

    hist = pd.to_numeric(pd.Series(history), errors="coerce").dropna()
    if len(hist) >= 3:
        diffs = hist.diff().abs().dropna()
        move_scale = float(diffs.tail(40).median()) if len(diffs) else np.nan
        if not np.isfinite(move_scale) or move_scale <= 0:
            move_scale = float(diffs.tail(40).std()) if len(diffs) else np.nan
        if not np.isfinite(move_scale) or move_scale <= 0:
            move_scale = max(float(hist.iloc[-1]) * 0.003, 1.0)
        base_width = max(move_scale * 1.6, float(hist.iloc[-1]) * 0.003, 1.0)
    else:
        base_width = max(float(np.mean(forecast)) * 0.003, 1.0)

    refined_lower = []
    refined_upper = []
    horizon = max(len(forecast), 1)
    for i, value in enumerate(forecast):
        grow = 1.0 + 0.35 * np.sqrt((i + 1) / horizon)
        width = base_width * grow
        lo = lower[i] if i < len(lower) else value - width
        hi = upper[i] if i < len(upper) else value + width
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            lo = value - width
            hi = value + width
        if (value - lo) < width * 0.4:
            lo = value - width
        if (hi - value) < width * 0.4:
            hi = value + width
        refined_lower.append(float(lo))
        refined_upper.append(float(hi))

    result["lower_band"] = refined_lower
    result["upper_band"] = refined_upper
    return result


def _has_forecast(result) -> bool:
    if result is None:
        return False
    try:
        values = result.get("forecast")
    except Exception:
        return False
    if values is None:
        return False
    try:
        return len(values) > 0
    except Exception:
        return False


def _run_persistence_forecast(
    historic_ltps: pd.Series,
    forecast_steps: int = FORECAST_STEPS,
):
    series = _clean_option_series_5m(historic_ltps)
    if series is None or len(series) == 0:
        return {"error": "no LTP history available"}

    current_price = float(pd.to_numeric(series.iloc[-1], errors="coerce"))
    if not np.isfinite(current_price) or current_price <= 0:
        return {"error": "invalid current LTP"}

    forecast_steps = max(int(forecast_steps), 1)
    forecast = [current_price] * forecast_steps
    diffs = pd.to_numeric(series.diff().abs(), errors="coerce").dropna()
    move_scale = float(diffs.tail(40).median()) if len(diffs) else np.nan
    if not np.isfinite(move_scale) or move_scale <= 0:
        move_scale = float(diffs.tail(40).std()) if len(diffs) else np.nan
    if not np.isfinite(move_scale) or move_scale <= 0:
        move_scale = max(current_price * 0.003, 1.0)

    lower = []
    upper = []
    for idx in range(forecast_steps):
        grow = 1.0 + 0.12 * np.sqrt(idx + 1)
        width = move_scale * grow
        lower.append(float(current_price - width))
        upper.append(float(current_price + width))

    returns = pd.to_numeric(series.pct_change(), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    recent_vol = float(returns.tail(30).std()) if len(returns) else np.nan
    if not np.isfinite(recent_vol):
        recent_vol = 0.0
    confidence = float(np.clip(62.0 - (recent_vol * 1800.0), 22.0, 55.0))

    return {
        "forecast": forecast,
        "lower_band": lower,
        "upper_band": upper,
        "current_ltp": current_price,
        "confidence": confidence,
        "model": "persistence_2h",
        "metrics": {"baseline": "last_close"},
    }


def _forecast_validation_metrics(
    train_series: pd.Series,
    actual_series: pd.Series,
    forecast_values,
) -> dict | None:
    train = pd.to_numeric(pd.Series(train_series), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    actual = pd.to_numeric(pd.Series(actual_series), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    forecast = pd.to_numeric(pd.Series(forecast_values), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    horizon = int(min(len(actual), len(forecast)))
    if horizon <= 0:
        return None

    actual_vals = actual.iloc[:horizon].to_numpy(dtype=float)
    pred_vals = forecast.iloc[:horizon].to_numpy(dtype=float)
    anchor = float(train.iloc[-1]) if len(train) else float(actual_vals[0])

    mae = float(np.mean(np.abs(actual_vals - pred_vals)))
    rmse = float(np.sqrt(np.mean((actual_vals - pred_vals) ** 2)))
    endpoint_abs_error = float(abs(actual_vals[-1] - pred_vals[-1]))
    actual_steps = np.sign(np.r_[actual_vals[0] - anchor, np.diff(actual_vals)])
    pred_steps = np.sign(np.r_[pred_vals[0] - anchor, np.diff(pred_vals)])
    dir_acc = float((actual_steps == pred_steps).mean() * 100.0)

    return {
        "mae": mae,
        "rmse": rmse,
        "endpoint_abs_error": endpoint_abs_error,
        "dir_acc": dir_acc,
        "horizon": horizon,
    }


def _run_option_forecast_candidate(
    model_key: str,
    historic_ltps: pd.Series,
    strike: float,
    option_type: str,
    option_frame: pd.DataFrame | None = None,
    underlying_prices=None,
    iv_series=None,
    oi_series=None,
    volume_series=None,
    spread_series=None,
    imbalance_series=None,
    microprice_series=None,
    signed_volume_series=None,
    static_features=None,
    days_to_expiry=None,
    forecast_steps: int = FORECAST_STEPS,
    auth_instance=None,
    expiry: str | None = None,
):
    normalized = str(model_key or "").strip().upper()
    result = None
    if normalized == "PERSISTENCE":
        return _run_persistence_forecast(historic_ltps, forecast_steps=forecast_steps)
    if normalized == "CATBOOST":
        if option_frame is None:
            return {"error": "option frame required for CatBoost"}
        result = _run_catboost_forecast(
            option_frame,
            strike=strike,
            option_type=option_type,
            underlying_prices=underlying_prices,
            static_features=static_features,
            days_to_expiry=days_to_expiry,
            forecast_steps=forecast_steps,
        )
    if normalized == "DLINEAR":
        result = _run_dlinear_forecast(
            historic_ltps,
            strike=strike,
            option_type=option_type,
            underlying_prices=underlying_prices,
            iv_series=iv_series,
            oi_series=oi_series,
            volume_series=volume_series,
            spread_series=spread_series,
            imbalance_series=imbalance_series,
            microprice_series=microprice_series,
            signed_volume_series=signed_volume_series,
            static_features=static_features,
            days_to_expiry=days_to_expiry,
            forecast_steps=forecast_steps,
        )
    if normalized == "LSTM":
        result = _run_lstm_forecast(
            historic_ltps,
            strike=strike,
            option_type=option_type,
            underlying_prices=underlying_prices,
            iv_series=iv_series,
            oi_series=oi_series,
            volume_series=volume_series,
            spread_series=spread_series,
            imbalance_series=imbalance_series,
            microprice_series=microprice_series,
            signed_volume_series=signed_volume_series,
            static_features=static_features,
            days_to_expiry=days_to_expiry,
            forecast_steps=forecast_steps,
        )
    if normalized == "IMPROVED":
        result = _run_improved_forecast(
            historic_ltps,
            strike=strike,
            option_type=option_type,
            underlying_prices=underlying_prices,
            iv_series=iv_series,
            oi_series=oi_series,
            volume_series=volume_series,
            spread_series=spread_series,
            imbalance_series=imbalance_series,
            microprice_series=microprice_series,
            signed_volume_series=signed_volume_series,
            static_features=static_features,
            days_to_expiry=days_to_expiry,
            auth_instance=auth_instance,
            forecast_steps=forecast_steps,
            expiry=expiry,
        )
    elif normalized == "NHITS":
        result = _run_nhits_forecast(
            historic_ltps,
            strike=strike,
            option_type=option_type,
            forecast_steps=forecast_steps,
        )
    elif normalized not in {"LSTM", "IMPROVED", "NHITS", "DLINEAR", "CATBOOST"}:
        return {"error": f"unknown model: {model_key}"}
    return result


def _select_option_forecast_model(
    completed_ltps: pd.Series,
    strike: float,
    option_type: str,
    selected_expiry: str | None = None,
    option_frame: pd.DataFrame | None = None,
    underlying_prices=None,
    iv_series=None,
    oi_series=None,
    volume_series=None,
    spread_series=None,
    imbalance_series=None,
    microprice_series=None,
    signed_volume_series=None,
    static_features=None,
    days_to_expiry=None,
    forecast_steps: int = FORECAST_STEPS,
    auth_instance=None,
):
    series = _clean_option_series_5m(completed_ltps)
    if series is None or len(series) <= forecast_steps:
        return {
            "selected_model": "PERSISTENCE",
            "reason": "Insufficient completed history for recent holdout selection.",
            "diagnostics": [],
            "ranked_models": ["PERSISTENCE"],
        }

    selector_anchor = None
    try:
        selector_anchor = _to_ist_timestamp(series.index[-1]).floor("5min").isoformat()
    except Exception:
        selector_anchor = str(len(series))
    selector_key = (
        f"{int(round(float(strike)))}|{str(option_type).upper()}|SELECTOR|"
        f"{selected_expiry or 'NA'}|{forecast_steps}|{selector_anchor}|{len(series)}"
    )
    cached = _get_selector_cache(selector_key, ttl_sec=1800)
    if cached:
        return cached

    train_series = series.iloc[:-forecast_steps]
    actual_series = series.iloc[-forecast_steps:]
    diagnostics = []
    valid_scores = {}
    candidate_order = ["CATBOOST", "LSTM", "NHITS", "IMPROVED"]

    train_frame = None
    if option_frame is not None:
        try:
            frame = _coerce_option_frame_ist(option_frame)
            if not frame.empty and isinstance(train_series.index, pd.DatetimeIndex):
                train_frame = frame.reindex(train_series.index).ffill().bfill()
                train_frame = train_frame.loc[train_series.index.intersection(train_frame.index)]
            if train_frame is None or train_frame.empty:
                train_frame = frame.loc[frame.index <= train_series.index[-1]]
        except Exception:
            train_frame = None

    for model_key in candidate_order:
        result = _run_option_forecast_candidate(
            model_key,
            train_series,
            strike=strike,
            option_type=option_type,
            option_frame=train_frame,
            underlying_prices=underlying_prices,
            iv_series=iv_series,
            oi_series=oi_series,
            volume_series=volume_series,
            spread_series=spread_series,
            imbalance_series=imbalance_series,
            microprice_series=microprice_series,
            signed_volume_series=signed_volume_series,
            static_features=static_features,
            days_to_expiry=days_to_expiry,
            forecast_steps=forecast_steps,
            auth_instance=auth_instance,
            expiry=selected_expiry,
        )
        row = {
            "Model": model_key,
            "Status": "ok" if _has_forecast(result) else str((result or {}).get("error", "unavailable")),
            "MAE": np.nan,
            "RMSE": np.nan,
            "Endpoint": np.nan,
            "DirAcc": np.nan,
        }
        if _has_forecast(result):
            metrics = _forecast_validation_metrics(train_series, actual_series, result.get("forecast"))
            if metrics:
                row.update(
                    {
                        "MAE": float(metrics.get("mae", np.nan)),
                        "RMSE": float(metrics.get("rmse", np.nan)),
                        "Endpoint": float(metrics.get("endpoint_abs_error", np.nan)),
                        "DirAcc": float(metrics.get("dir_acc", np.nan)),
                    }
                )
                valid_scores[model_key] = metrics
        diagnostics.append(row)

    ranked_models = []
    if valid_scores:
        ranked_models = [
            model_key
            for model_key, _ in sorted(
                valid_scores.items(),
                key=lambda item: (
                    float(item[1].get("mae", np.inf)),
                    float(item[1].get("endpoint_abs_error", np.inf)),
                    float(item[1].get("rmse", np.inf)),
                    -float(item[1].get("dir_acc", -np.inf)),
                ),
            )
        ]

    selected_model = ranked_models[0] if ranked_models else "PERSISTENCE"
    selected_reason = "No valid holdout scores; using persistence fallback."
    persistence_metrics = valid_scores.get("PERSISTENCE")
    leader_metrics = valid_scores.get(selected_model)

    if leader_metrics:
        selected_reason = (
            f"Lowest recent completed {forecast_steps * 5}m holdout MAE "
            f"({float(leader_metrics.get('mae', np.nan)):.2f})."
        )

    if persistence_metrics and selected_model != "PERSISTENCE" and leader_metrics:
        if (
            float(leader_metrics.get("mae", np.inf)) > float(persistence_metrics.get("mae", np.inf)) * 1.03
            and float(leader_metrics.get("dir_acc", -np.inf)) <= float(persistence_metrics.get("dir_acc", -np.inf))
        ):
            selected_model = "PERSISTENCE"
            selected_reason = (
                "Advanced models did not beat persistence decisively on the most recent completed holdout."
            )
        elif (
            float(leader_metrics.get("endpoint_abs_error", np.inf)) > float(persistence_metrics.get("endpoint_abs_error", np.inf)) * 1.25
            and float(leader_metrics.get("dir_acc", -np.inf)) + 5.0 < float(persistence_metrics.get("dir_acc", -np.inf))
        ):
            selected_model = "PERSISTENCE"
            selected_reason = (
                "Advanced models had unstable recent holdout endpoint error; using persistence fallback."
            )
    elif persistence_metrics and selected_model == "PERSISTENCE":
        advanced_candidates = {
            key: value for key, value in valid_scores.items()
            if key != "PERSISTENCE"
        }
        if advanced_candidates:
            alt_model, alt_metrics = min(
                advanced_candidates.items(),
                key=lambda item: (
                    float(item[1].get("mae", np.inf)),
                    float(item[1].get("endpoint_abs_error", np.inf)),
                    -float(item[1].get("dir_acc", -np.inf)),
                ),
            )
            if (
                float(alt_metrics.get("mae", np.inf)) <= float(persistence_metrics.get("mae", np.inf)) * 1.08
                and float(alt_metrics.get("dir_acc", -np.inf)) >= float(persistence_metrics.get("dir_acc", -np.inf)) + 10.0
            ):
                selected_model = alt_model
                selected_reason = (
                    f"{alt_model} selected over persistence due to materially better recent direction capture "
                    f"with comparable MAE."
                )

    for row in diagnostics:
        row["Selected"] = "Yes" if row.get("Model") == selected_model else ""

    payload = {
        "selected_model": selected_model,
        "reason": selected_reason,
        "diagnostics": diagnostics,
        "ranked_models": ranked_models or ["PERSISTENCE"],
        "holdout_start": _to_ist_timestamp(actual_series.index[0]).isoformat() if isinstance(actual_series.index, pd.DatetimeIndex) and len(actual_series.index) else "",
        "holdout_end": _to_ist_timestamp(actual_series.index[-1]).isoformat() if isinstance(actual_series.index, pd.DatetimeIndex) and len(actual_series.index) else "",
    }
    _set_selector_cache(selector_key, payload)
    return payload

def _run_lstm_forecast(
    historic_ltps: pd.Series,
    strike: float,
    option_type: str,
    underlying_prices=None,
    iv_series=None,
    oi_series=None,
    volume_series=None,
    spread_series=None,
    imbalance_series=None,
    microprice_series=None,
    signed_volume_series=None,
    static_features=None,
    days_to_expiry=None,
    forecast_steps: int = FORECAST_STEPS,
):
    series = _clean_option_series_5m(historic_ltps)
    if series is None:
        return {"error": "no LTP history available"}
    min_points = max(60, forecast_steps + 16)
    if len(series) < min_points:
        return {"error": f"insufficient data: {len(series)} rows, need >= {min_points}"}

    key = f"{int(round(float(strike)))}|{str(option_type).upper()}|LSTM|{forecast_steps}"
    cached = _get_model_cache(key)
    model = None
    metrics = {}
    if cached and cached.get("model") is not None:
        cached_metrics = dict(cached.get("metrics") or {})
        cached_input = int(cached_metrics.get("input_size", 0) or 0)
        cached_len = int(cached.get("length", 0) or 0)
        min_cached = cached_input + forecast_steps + 20 if cached_input else 0
        if cached_len >= len(series) - 6 and (min_cached == 0 or len(series) >= min_cached):
            model = cached.get("model")
            metrics = cached_metrics

    if model is None:
        max_feasible = max(24, len(series) - forecast_steps - 10)
        lookback = int(min(96, max_feasible))
        if len(series) < lookback + forecast_steps + 10:
            return {"error": f"insufficient data for lookback {lookback}: need >= {lookback + forecast_steps + 10}"}
        model = OptionsLSTMForecaster(
            lookback=lookback,
            forecast_steps=forecast_steps,
            strike=strike,
            option_type=option_type,
        )
        aligned_under = _align_series_to_index(underlying_prices, series.index)
        aligned_iv = _align_series_to_index(iv_series, series.index)
        aligned_oi = _align_series_to_index(oi_series, series.index)
        aligned_vol = _align_series_to_index(volume_series, series.index)
        success, metrics = model.train(
            series,
            epochs=32 if len(series) >= 200 else 24,
            batch_size=16,
            validation_split=0.15,
            underlying_series=aligned_under,
            iv_series=aligned_iv,
            oi_series=aligned_oi,
            volume_series=aligned_vol,
            spread_series=_align_series_to_index(spread_series, series.index),
            imbalance_series=_align_series_to_index(imbalance_series, series.index),
            microprice_series=_align_series_to_index(microprice_series, series.index),
            signed_volume_series=_align_series_to_index(signed_volume_series, series.index),
            static_features=static_features or {},
            days_to_expiry=days_to_expiry,
        )
        if not success:
            return {"error": (metrics or {}).get("error", "LSTM training failed")}
        _set_model_cache(key, model, metrics, len(series))

    aligned_under = _align_series_to_index(underlying_prices, series.index)
    aligned_iv = _align_series_to_index(iv_series, series.index)
    aligned_oi = _align_series_to_index(oi_series, series.index)
    aligned_vol = _align_series_to_index(volume_series, series.index)
    forecast = model.forecast(
        series,
        underlying_series=aligned_under,
        iv_series=aligned_iv,
        oi_series=aligned_oi,
        volume_series=aligned_vol,
        spread_series=_align_series_to_index(spread_series, series.index),
        imbalance_series=_align_series_to_index(imbalance_series, series.index),
        microprice_series=_align_series_to_index(microprice_series, series.index),
        signed_volume_series=_align_series_to_index(signed_volume_series, series.index),
        static_features=static_features or {},
        days_to_expiry=days_to_expiry,
    )
    if forecast is None:
        return {"error": "LSTM prediction failed"}
    pred_vals = forecast.get("forecast")
    try:
        if pred_vals is None or len(pred_vals) == 0:
            return {"error": "LSTM prediction failed"}
    except Exception:
        return {"error": "LSTM prediction failed"}
    forecast["model"] = "lstm_2h"
    forecast["metrics"] = {**metrics, **(forecast.get("metrics") or {})}
    return forecast


def _run_dlinear_forecast(
    historic_ltps: pd.Series,
    strike: float,
    option_type: str,
    underlying_prices=None,
    iv_series=None,
    oi_series=None,
    volume_series=None,
    spread_series=None,
    imbalance_series=None,
    microprice_series=None,
    signed_volume_series=None,
    static_features=None,
    days_to_expiry=None,
    forecast_steps: int = FORECAST_STEPS,
):
    series = _clean_option_series_5m(historic_ltps)
    if series is None:
        return {"error": "no LTP history available"}
    min_points = max(96, forecast_steps + 36)
    if len(series) < min_points:
        return {"error": f"insufficient data: {len(series)} rows, need >= {min_points}"}

    input_len = int(min(120, max(48, forecast_steps * 4)))
    if len(series) < input_len + forecast_steps + 8:
        input_len = max(36, len(series) - forecast_steps - 8)
    if input_len < 36:
        return {"error": "insufficient data for DLinear input window"}

    key = f"{int(round(float(strike)))}|{str(option_type).upper()}|DLINEAR|{forecast_steps}|{input_len}"
    cached = _get_model_cache(key)
    model = None
    metrics = {}
    if cached and cached.get("model") is not None:
        cached_len = int(cached.get("length", 0) or 0)
        if cached_len >= len(series) - 6:
            model = cached.get("model")
            metrics = dict(cached.get("metrics") or {})

    if model is None:
        model = DLinearForecaster(
            input_len=input_len,
            horizon=int(forecast_steps),
            ma_window=25,
            ridge_alpha=1.0,
        )
        exog = {
            "underlying": _align_series_to_index(underlying_prices, series.index),
            "iv": _align_series_to_index(iv_series, series.index),
            "oi": _align_series_to_index(oi_series, series.index),
            "volume": _align_series_to_index(volume_series, series.index),
            "spread": _align_series_to_index(spread_series, series.index),
            "imbalance": _align_series_to_index(imbalance_series, series.index),
            "microprice": _align_series_to_index(microprice_series, series.index),
            "signed_volume": _align_series_to_index(signed_volume_series, series.index),
        }
        success, metrics = model.fit(series, exog=exog)
        if not success:
            return {"error": (metrics or {}).get("error", "DLinear training failed")}
        _set_model_cache(key, model, metrics, len(series))

    exog = {
        "underlying": _align_series_to_index(underlying_prices, series.index),
        "iv": _align_series_to_index(iv_series, series.index),
        "oi": _align_series_to_index(oi_series, series.index),
        "volume": _align_series_to_index(volume_series, series.index),
        "spread": _align_series_to_index(spread_series, series.index),
        "imbalance": _align_series_to_index(imbalance_series, series.index),
        "microprice": _align_series_to_index(microprice_series, series.index),
        "signed_volume": _align_series_to_index(signed_volume_series, series.index),
    }
    pred = model.predict(series, exog=exog)
    if pred is None or pred.get("forecast") is None:
        return {"error": "DLinear prediction failed"}

    forecast_vals = np.asarray(pred["forecast"], dtype=float)
    forecast_vals = forecast_vals[: int(forecast_steps)]
    if forecast_vals.size == 0:
        return {"error": "DLinear prediction failed"}

    current_ltp = float(pd.to_numeric(series.iloc[-1], errors="coerce"))
    resid_std = float(pred.get("residual_std", 0.0) or 0.0)
    base_std = max(resid_std, max(current_ltp * 0.002, 0.8))
    t = np.arange(1, len(forecast_vals) + 1, dtype=float)
    band = base_std * np.sqrt(t)
    lower = np.maximum(forecast_vals - band, 0.01)
    upper = forecast_vals + band
    confidence = float(np.clip(100 - (base_std / max(current_ltp, 1e-6) * 500), 15, 90))

    return {
        "forecast": forecast_vals.tolist(),
        "lower_band": lower.tolist(),
        "upper_band": upper.tolist(),
        "current_ltp": float(current_ltp),
        "confidence": confidence,
        "model": "dlinear_2h",
        "metrics": {**metrics, "residual_std": resid_std, "input_len": input_len},
    }


def _run_catboost_forecast(
    option_frame: pd.DataFrame,
    strike: float,
    option_type: str,
    underlying_prices=None,
    static_features=None,
    days_to_expiry=None,
    forecast_steps: int = FORECAST_STEPS,
):
    frame = _coerce_option_frame_ist(option_frame)
    if frame.empty:
        return {"error": "no option candle history available"}
    if "Close" not in frame.columns:
        return {"error": "option frame missing Close column"}
    if len(frame) < max(140, forecast_steps + 80):
        return {"error": f"insufficient option frame history: {len(frame)} rows"}

    key = f"{int(round(float(strike)))}|{str(option_type).upper()}|CATBOOST|{forecast_steps}"
    cached = _get_model_cache(key, ttl_sec=1800)
    model = None
    metrics = {}
    if cached and cached.get("model") is not None:
        cached_len = int(cached.get("length", 0) or 0)
        if cached_len >= len(frame) - 6:
            model = cached.get("model")
            metrics = dict(cached.get("metrics") or {})

    if model is None:
        model = OptionsMicrostructureForecaster(
            strike=strike,
            option_type=option_type,
            forecast_steps=forecast_steps,
        )
        iterations = 180 if len(frame) < 900 else 220
        success, metrics = model.train(
            frame,
            underlying_series=underlying_prices,
            static_features=static_features or {},
            days_to_expiry=days_to_expiry,
            iterations=iterations,
        )
        if not success:
            return {"error": (metrics or {}).get("error", "CatBoost training failed")}
        _set_model_cache(key, model, metrics, len(frame))

    pred = model.predict_sequence(
        frame,
        underlying_series=underlying_prices,
        static_features=static_features or {},
        days_to_expiry=days_to_expiry,
    )
    if pred is None:
        return {"error": "CatBoost prediction failed"}

    forecast_vals = pd.to_numeric(pd.Series(pred.get("median", [])), errors="coerce").dropna().tolist()
    lower_vals = pd.to_numeric(pd.Series(pred.get("lower", [])), errors="coerce").dropna().tolist()
    upper_vals = pd.to_numeric(pd.Series(pred.get("upper", [])), errors="coerce").dropna().tolist()
    if not forecast_vals:
        return {"error": "CatBoost prediction failed"}

    return {
        "forecast": forecast_vals,
        "lower_band": lower_vals,
        "upper_band": upper_vals,
        "current_ltp": float(pred.get("current_ltp", np.nan)),
        "confidence": float(pred.get("confidence", 0.0)),
        "model": "catboost_micro_2h",
        "metrics": dict(pred.get("metrics") or metrics or {}),
    }


def _run_improved_forecast(
    historic_ltps: pd.Series,
    strike: float,
    option_type: str,
    underlying_prices=None,
    iv_series=None,
    oi_series=None,
    volume_series=None,
    spread_series=None,
    imbalance_series=None,
    microprice_series=None,
    signed_volume_series=None,
    static_features=None,
    days_to_expiry=None,
    auth_instance=None,
    forecast_steps: int = FORECAST_STEPS,
    expiry: str | None = None,
):
    series = _clean_option_series_5m(historic_ltps)
    if series is None:
        return {"error": "no LTP history available"}
    min_points = max(72, forecast_steps + 24)
    if len(series) < min_points:
        return {"error": f"insufficient data: {len(series)} rows, need >= {min_points}"}

    try:
        out = forecast_option_ltp_improved(
            strike=float(strike),
            option_type=str(option_type).upper(),
            historic_ltps=series,
            underlying_prices=underlying_prices,
            iv_series=iv_series,
            oi_series=oi_series,
            volume_series=volume_series,
            spread_series=spread_series,
            imbalance_series=imbalance_series,
            microprice_series=microprice_series,
            signed_volume_series=signed_volume_series,
            static_features=static_features or {},
            days_to_expiry=days_to_expiry,
            auth_instance=auth_instance,
            symbol="BANKNIFTY",
            fetch_missing_vars=False,
            print_diagnostics=False,
            expiry=expiry,
            forecast_steps=int(forecast_steps),
        )
    except Exception as e:
        return {"error": f"improved forecast failed: {e}"}

    if out is None:
        return {"error": "improved forecast unavailable"}
    pred_vals = out.get("forecast")
    try:
        if pred_vals is None or len(pred_vals) == 0:
            return {"error": str(out.get('error') or 'improved forecast unavailable')}
    except Exception:
        return {"error": "improved forecast unavailable"}
    return out


def _run_nhits_forecast(
    historic_ltps: pd.Series,
    strike: float,
    option_type: str,
    forecast_steps: int = FORECAST_STEPS,
):
    series = _clean_option_series_5m(historic_ltps)
    if series is None:
        return {"error": "no LTP history available"}

    min_buffer = int(forecast_steps) + 20
    max_input = len(series) - min_buffer
    if max_input < 48:
        return {"error": f"insufficient data: {len(series)} rows, need >= {min_buffer + 48}"}

    preferred_inputs = (180, 144, 120, 96, 72, 48)
    input_size = next((val for val in preferred_inputs if val <= max_input), max(48, int(max_input)))

    key = f"{int(round(float(strike)))}|{str(option_type).upper()}|NHITS|{forecast_steps}"
    cached = _get_model_cache(key)
    model = None
    metrics = {}
    if cached and cached.get("model") is not None:
        cached_metrics = dict(cached.get("metrics") or {})
        cached_input = int(cached_metrics.get("input_size", 0) or 0)
        cached_len = int(cached.get("length", 0) or 0)
        min_cached = cached_input + forecast_steps + 20 if cached_input else 0
        if cached_len >= len(series) - 6 and (min_cached == 0 or len(series) >= min_cached):
            model = cached.get("model")
            metrics = cached_metrics

    df = pd.DataFrame({"Close": series.values}, index=series.index)

    if model is None:
        model = NHITSForecaster(forecast_steps=forecast_steps, input_size=input_size)
        max_steps = 320 if len(series) < 350 else 400
        success, metrics = model.train(df, max_steps=max_steps, tune=False, profile="option_intraday")
        if not success:
            return {"error": (metrics or {}).get("error", "NHITS training failed")}
        _set_model_cache(key, model, metrics, len(series))

    pred = model.predict_sequence(df)
    if pred is None:
        return {"error": "NHITS prediction failed"}
    pred_vals = pred.get("median")
    try:
        if pred_vals is None or len(pred_vals) == 0:
            return {"error": "NHITS prediction failed"}
    except Exception:
        return {"error": "NHITS prediction failed"}

    forecast_vals = (
        pd.to_numeric(pd.Series(pred_vals), errors="coerce").dropna().to_list()
        if pred_vals is not None
        else []
    )
    if not forecast_vals:
        return {"error": "NHITS prediction failed"}

    out = {
        "forecast": forecast_vals,
        "lower_band": pred.get("lower", []),
        "upper_band": pred.get("upper", []),
        "current_ltp": float(pd.to_numeric(pd.Series(series), errors="coerce").dropna().iloc[-1]),
        "confidence": float(pred.get("confidence", 0.0)),
        "model": "nhits_2h",
        "metrics": metrics,
        "input_size": int(input_size),
    }
    return out


def _load_fetch_strike_option_history(
    auth_instance,
    symbol: str,
    expiry: str,
    strike: float,
    option_type: str,
    interval_minutes: int = 5,
    history_days: int = 120,
    cache_ttl_sec: int = 180,
) -> tuple[pd.Series, str]:
    """
    Load true strike-wise option LTP history from disk and optionally refresh from Upstox.
    Returns (series, source_tag).
    """
    cache = st.session_state.setdefault("option_ltp_hist_cache", {})
    key = (
        f"{symbol}|{expiry}|{int(round(float(strike)))}|{str(option_type).upper()}|"
        f"{interval_minutes}|{int(max(1, history_days))}"
    )
    now = float(pytime.time())
    cached = cache.get(key)
    if cached and (now - float(cached.get("ts", 0.0))) <= float(cache_ttl_sec):
        cached_series = _coerce_series_to_ist(cached.get("series"))
        if not cached_series.empty:
            return cached_series, str(cached.get("source", "cache"))

    source_parts = []
    merged = pd.Series(dtype=float)

    # 1) Load persisted local history.
    try:
        on_disk = load_option_ltp_data(
            expiry=expiry,
            symbol=symbol,
            option_type=str(option_type).upper(),
            strikes=[float(strike), int(round(float(strike)))],
        )
        if on_disk:
            for k, s in on_disk.items():
                if abs(float(k) - float(strike)) <= 0.5:
                    merged = _merge_series_ist(merged, s)
                    source_parts.append("disk")
                    break
    except Exception as e:
        print(f"⚠️ load_option_ltp_data failed for {option_type} {strike}: {e}")

    # 2) Refresh from Upstox option candle history when quote backoff is not active.
    allow_live_fetch = bool(auth_instance and getattr(auth_instance, "access_token", None))
    if allow_live_fetch:
        try:
            rate_limited_until = float(getattr(auth_instance, "rate_limited_until", 0.0) or 0.0)
            if now < rate_limited_until:
                allow_live_fetch = False
        except Exception:
            pass

    if allow_live_fetch:
        try:
            live_map, used_expiry = fetch_upstox_option_chain_ltp_timeseries(
                auth_instance,
                symbol=symbol,
                expiry=expiry,
                option_type=str(option_type).upper(),
                selected_strikes=[float(strike)],
                interval_minutes=int(max(1, interval_minutes)),
                include_non_intraday_history=True,
                history_days=int(max(1, history_days)),
            )
            if live_map:
                live_series = pd.Series(dtype=float)
                for k, s in live_map.items():
                    if abs(float(k) - float(strike)) <= 0.5:
                        live_series = _coerce_series_to_ist(s)
                        break
                if not live_series.empty:
                    merged = _merge_series_ist(merged, live_series)
                    source_parts.append("upstox")
                    try:
                        save_option_ltp_data(
                            {float(strike): live_series},
                            used_expiry or expiry,
                            symbol=symbol,
                            option_type=str(option_type).upper(),
                        )
                    except Exception:
                        pass
        except Exception as e:
            print(f"⚠️ fetch_upstox_option_chain_ltp_timeseries failed for {option_type} {strike}: {e}")

    source_tag = "+".join(sorted(set(source_parts))) if source_parts else "none"
    cache[key] = {"ts": now, "series": merged, "source": source_tag}
    return merged, source_tag


def _load_fetch_strike_option_frame_history(
    auth_instance,
    symbol: str,
    expiry: str,
    strike: float,
    option_type: str,
    interval_minutes: int = 5,
    history_days: int = 120,
    cache_ttl_sec: int = 180,
) -> tuple[pd.DataFrame, str]:
    """
    Load strike-wise option candle history with whatever feature columns are available.
    Prefers full Upstox candle frames; falls back to persisted close-only history.
    """
    cache = st.session_state.setdefault("option_ltp_frame_cache", {})
    key = (
        f"{symbol}|{expiry}|{int(round(float(strike)))}|{str(option_type).upper()}|frame|"
        f"{interval_minutes}|{int(max(1, history_days))}"
    )
    now = float(pytime.time())
    cached = cache.get(key)
    if cached and (now - float(cached.get("ts", 0.0))) <= float(cache_ttl_sec):
        cached_frame = _coerce_option_frame_ist(cached.get("frame"))
        if not cached_frame.empty:
            return cached_frame, str(cached.get("source", "cache"))

    source_parts = []
    merged = pd.DataFrame()

    try:
        on_disk = load_option_ltp_data(
            expiry=expiry,
            symbol=symbol,
            option_type=str(option_type).upper(),
            strikes=[float(strike), int(round(float(strike)))],
        )
        if on_disk:
            for k, series in on_disk.items():
                if abs(float(k) - float(strike)) <= 0.5:
                    merged = _merge_option_frames_ist(merged, _series_to_option_frame(series))
                    source_parts.append("disk")
                    break
    except Exception as e:
        print(f"⚠️ load_option_ltp_data frame fallback failed for {option_type} {strike}: {e}")

    allow_live_fetch = bool(auth_instance and getattr(auth_instance, "access_token", None))
    if allow_live_fetch:
        try:
            rate_limited_until = float(getattr(auth_instance, "rate_limited_until", 0.0) or 0.0)
            if now < rate_limited_until:
                allow_live_fetch = False
        except Exception:
            pass

    if allow_live_fetch:
        try:
            live_map, used_expiry = fetch_upstox_option_chain_ltp_timeseries(
                auth_instance,
                symbol=symbol,
                expiry=expiry,
                option_type=str(option_type).upper(),
                selected_strikes=[float(strike)],
                interval_minutes=int(max(1, interval_minutes)),
                include_non_intraday_history=True,
                history_days=int(max(1, history_days)),
                return_full_frame=True,
            )
            if live_map:
                live_frame = pd.DataFrame()
                for k, frame in live_map.items():
                    if abs(float(k) - float(strike)) <= 0.5:
                        live_frame = _coerce_option_frame_ist(frame)
                        break
                if not live_frame.empty:
                    merged = _merge_option_frames_ist(merged, live_frame)
                    source_parts.append("upstox")
                    try:
                        save_option_ltp_data(
                            {float(strike): live_frame["Close"].dropna()},
                            used_expiry or expiry,
                            symbol=symbol,
                            option_type=str(option_type).upper(),
                        )
                    except Exception:
                        pass
        except Exception as e:
            print(f"⚠️ fetch_upstox_option frame failed for {option_type} {strike}: {e}")

    source_tag = "+".join(sorted(set(source_parts))) if source_parts else "none"
    cache[key] = {"ts": now, "frame": merged, "source": source_tag}
    return merged, source_tag

def get_data_filepath(symbol: str, interval: str) -> Path:
    """Get filepath for cached data"""
    # Clean symbol for filename (remove special characters)
    clean_symbol = symbol.replace("^", "").replace(".", "_")
    filename = f"{clean_symbol}_{interval}.parquet"
    return DATA_DIR / filename

def load_cached_data(symbol: str, interval: str) -> pd.DataFrame:
    """Load cached historical data if exists"""
    filepath = get_data_filepath(symbol, interval)
    if filepath.exists():
        try:
            df = pd.read_parquet(filepath)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            print(f"⚠️ Error loading cache {filepath}: {e}")
            try:
                err = str(e).lower()
                if ("corrupt" in err) or ("snappy" in err):
                    bad_path = filepath.with_suffix(filepath.suffix + ".corrupt")
                    filepath.replace(bad_path)
                    print(f"⚠️ Moved corrupt cache to {bad_path}")
            except Exception:
                pass
    return None

def save_cached_data(df: pd.DataFrame, symbol: str, interval: str) -> bool:
    """Save data to local cache"""
    if df is None or df.empty:
        return False
    try:
        with _CACHE_WRITE_LOCK:
            existing_df = load_cached_data(symbol, interval)
            # Never replace a healthy cache with a tiny frame (common when live API returns partial data).
            if (
                existing_df is not None
                and not existing_df.empty
                and len(existing_df) >= 50
                and len(df) < 10
            ):
                print(
                    f"⚠️ Skip cache overwrite for {symbol} {interval}: "
                    f"new_rows={len(df)} existing_rows={len(existing_df)}"
                )
                return False

            filepath = get_data_filepath(symbol, interval)
            # Ensure parent directory exists
            filepath.parent.mkdir(parents=True, exist_ok=True)
            tmp_name = f"{filepath.name}.{os.getpid()}.{int(pytime.time() * 1000)}.{uuid4().hex}.tmp"
            tmp_path = filepath.with_name(tmp_name)
            df.to_parquet(tmp_path)
            os.replace(tmp_path, filepath)
            print(f"✅ Cached {len(df)} rows to {filepath}")
            return True
    except Exception as e:
        print(f"⚠️ Error saving cache: {e}")
        return False


def load_best_local_price_history(symbol: str, interval: str) -> pd.DataFrame:
    """
    Load best available local OHLC history from data/historical_prices.
    Prefers requested interval, otherwise picks largest available dataset.
    """
    clean_symbol = symbol.replace("^", "").replace(".", "_")
    files = sorted(DATA_DIR.glob(f"{clean_symbol}_*.parquet"))
    if not files:
        return None

    ranked = []
    for fp in files:
        try:
            df = pd.read_parquet(fp)
            if df is None or df.empty:
                continue
            interval_tag = fp.stem.split("_")[-1]
            ranked.append((interval_tag == interval, len(df), df))
        except Exception:
            continue

    if not ranked:
        return None

    ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best = ranked[0][2].copy()

    # Handle possible MultiIndex columns from yfinance parquet.
    if isinstance(best.columns, pd.MultiIndex) and best.columns.nlevels >= 2:
        best.columns = best.columns.get_level_values(-1)

    # If still not direct OHLC, try first level values.
    if not all(c in best.columns for c in ["Open", "High", "Low", "Close"]):
        as_str = [str(c) for c in best.columns]
        col_map = {}
        for target in ["Open", "High", "Low", "Close"]:
            for c in best.columns:
                if str(c).endswith(target):
                    col_map[target] = c
                    break
        if len(col_map) == 4:
            best = best[[col_map["Open"], col_map["High"], col_map["Low"], col_map["Close"]]]
            best.columns = ["Open", "High", "Low", "Close"]

    if not all(c in best.columns for c in ["Open", "High", "Low", "Close"]):
        return None

    best = best[["Open", "High", "Low", "Close"]]
    best.index = pd.to_datetime(best.index, errors="coerce")
    best = best[~pd.isna(best.index)].sort_index()
    if isinstance(best.index, pd.DatetimeIndex) and best.index.tz is not None:
        best.index = best.index.tz_localize(None)
    best.attrs["data_source"] = "local_history_fallback"
    return best


def fetch_banknifty_data(
    period: str = "180d",
    interval: str = "5m",
    force_refresh: bool = True,
    auth_instance=None,
) -> pd.DataFrame:
    """Fetch BankNifty data. Fresh fetch first, cache fallback."""

    def _normalize_index_to_ist_naive(frame: pd.DataFrame) -> pd.DataFrame:
        """
        Convert mixed timezone/object datetime indices to IST-naive DatetimeIndex safely.
        """
        if frame is None or frame.empty:
            return frame
        out = frame.copy()
        if isinstance(out.index, pd.DatetimeIndex):
            try:
                if out.index.tz is not None:
                    out.index = out.index.tz_convert(IST).tz_localize(None)
                else:
                    out.index = pd.to_datetime(out.index, errors="coerce")
            except Exception:
                out.index = pd.to_datetime(out.index, errors="coerce")
        else:
            converted = []
            for val in out.index:
                ts = pd.to_datetime(val, errors="coerce")
                if pd.isna(ts):
                    converted.append(pd.NaT)
                    continue
                try:
                    if getattr(ts, "tzinfo", None) is not None:
                        ts = ts.tz_convert(IST).tz_localize(None)
                except Exception:
                    try:
                        ts = ts.tz_localize(None)
                    except Exception:
                        pass
                converted.append(ts)
            out.index = pd.DatetimeIndex(converted)
        out = out[~pd.isna(out.index)]
        return out

    def _load_backup_history(symbol: str, target_interval: str) -> pd.DataFrame:
        """
        Recover history from any local interval cache if target cache is missing/tiny.
        """
        clean_symbol = symbol.replace("^", "").replace(".", "_")
        pattern = f"{clean_symbol}_*.parquet"
        candidates = []
        for fp in sorted(DATA_DIR.glob(pattern)):
            try:
                df_b = pd.read_parquet(fp)
                if df_b is None or df_b.empty:
                    continue
                interval_tag = fp.stem.split("_")[-1]
                candidates.append((len(df_b), interval_tag, df_b))
            except Exception:
                continue
        if not candidates:
            return None
        # Prefer requested interval with enough rows; else best by row count.
        exact = [c for c in candidates if c[1] == target_interval]
        if exact:
            exact = sorted(exact, key=lambda x: x[0], reverse=True)
            if exact[0][0] >= 10:
                return exact[0][2]
        best = max(candidates, key=lambda x: x[0])
        return best[2]

    def _interval_to_minutes_local(interval_value: str) -> int:
        try:
            token = str(interval_value).strip().lower()
            if token.endswith("m"):
                return max(int(float(token[:-1])), 1)
            if token.endswith("h"):
                return max(int(float(token[:-1]) * 60), 1)
            if token.endswith("d"):
                return max(int(float(token[:-1]) * 1440), 1)
        except Exception:
            pass
        return 5

    def _floor_to_bucket_ist_naive(ts, interval_value: str):
        minutes = _interval_to_minutes_local(interval_value)
        t = pd.to_datetime(ts, errors="coerce")
        if pd.isna(t):
            return pd.NaT
        if minutes >= 1440:
            return pd.Timestamp(year=t.year, month=t.month, day=t.day)
        floored_minute = (t.minute // minutes) * minutes
        return t.replace(minute=floored_minute, second=0, microsecond=0)

    def _extract_live_banknifty_quote(auth):
        if auth is None or not getattr(auth, "access_token", None):
            return None
        candidates = ["NSE_INDEX|Nifty Bank", "NSE_INDEX|NIFTY BANK", "NSE_INDEX|BANKNIFTY"]
        for key in candidates:
            try:
                quote = auth.get_market_quote([key])
                data = (quote or {}).get("data", {}) if isinstance(quote, dict) else {}
                payload = data.get(key) if isinstance(data, dict) else None
                if payload is None and isinstance(data, dict) and len(data) == 1:
                    payload = next(iter(data.values()))
                if not isinstance(payload, dict):
                    continue
                ltp = pd.to_numeric(payload.get("last_price", payload.get("ltp")), errors="coerce")
                if not np.isfinite(ltp):
                    continue
                ohlc = payload.get("ohlc", {}) or {}
                return {
                    "ltp": float(ltp),
                    "open": pd.to_numeric(ohlc.get("open"), errors="coerce"),
                    "high": pd.to_numeric(ohlc.get("high"), errors="coerce"),
                    "low": pd.to_numeric(ohlc.get("low"), errors="coerce"),
                }
            except Exception:
                continue
        return None

    def _inject_live_quote_candle(frame: pd.DataFrame, auth, interval_value: str):
        live = _extract_live_banknifty_quote(auth)
        if live is None:
            return frame, False

        out = frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame(columns=["Open", "High", "Low", "Close"])
        if out.empty:
            out = pd.DataFrame(columns=["Open", "High", "Low", "Close"])

        if not isinstance(out.index, pd.DatetimeIndex):
            out.index = pd.to_datetime(out.index, errors="coerce")
            out = out[~pd.isna(out.index)]
        if out.index.tz is not None:
            out.index = out.index.tz_localize(None)

        now_ist_naive = datetime.now(IST).replace(tzinfo=None)
        bucket = _floor_to_bucket_ist_naive(now_ist_naive, interval_value)
        if pd.isna(bucket):
            return out, False

        ltp = float(live["ltp"])

        if bucket in out.index:
            # Update existing bucket candle with latest LTP
            # Keep existing OHLC but update Close and High/Low if needed
            row = out.loc[bucket]
            existing_open = pd.to_numeric(row.get("Open"), errors="coerce")
            existing_high = pd.to_numeric(row.get("High"), errors="coerce")
            existing_low = pd.to_numeric(row.get("Low"), errors="coerce")
            existing_close = pd.to_numeric(row.get("Close"), errors="coerce")
            
            # Preserve open, but update high/low/close based on price movement
            new_high = max(float(existing_high) if np.isfinite(existing_high) else ltp, float(ltp))
            new_low = min(float(existing_low) if np.isfinite(existing_low) else ltp, float(ltp))
            
            out.loc[bucket, "High"] = new_high
            out.loc[bucket, "Low"] = new_low
            out.loc[bucket, "Close"] = ltp
        else:
            # Create new candle - use prev close as open, current LTP as close
            prev_close = pd.to_numeric(out["Close"].iloc[-1], errors="coerce") if len(out) > 0 else np.nan
            open_price = float(prev_close) if np.isfinite(prev_close) else float(ltp)
            
            out.loc[bucket, "Open"] = open_price
            out.loc[bucket, "High"] = max(float(open_price), float(ltp))  # High = max(open, close)
            out.loc[bucket, "Low"] = min(float(open_price), float(ltp))   # Low = min(open, close)
            out.loc[bucket, "Close"] = float(ltp)

        out = out.sort_index()
        # Remove any duplicate indices keeping the latest value
        out = out[~out.index.duplicated(keep='last')]
        out.attrs["live_quote_injected"] = True
        out.attrs["live_ltp"] = ltp
        return out, True

    def _prepare_ohlc(frame: pd.DataFrame, source: str) -> pd.DataFrame:
        if frame is None or frame.empty:
            return frame
        out = frame.copy()
        out = _normalize_index_to_ist_naive(out)
        if all(col in out.columns for col in ["Open", "High", "Low", "Close"]):
            out = out[["Open", "High", "Low", "Close"]]
        out.attrs["data_source"] = source
        return out

    def _merge_with_cached_history(fresh_frame: pd.DataFrame, cached_frame: pd.DataFrame, source: str) -> pd.DataFrame:
        """
        Merge fresh candles into cached history, replacing duplicates by latest timestamp.
        """
        cached_prepared = _prepare_ohlc(cached_frame, "cache") if cached_frame is not None else None
        fresh_prepared = _prepare_ohlc(fresh_frame, source) if fresh_frame is not None else None

        if fresh_prepared is None or fresh_prepared.empty:
            return cached_prepared
        if cached_prepared is None or cached_prepared.empty:
            return fresh_prepared

        combined = pd.concat([cached_prepared, fresh_prepared], axis=0)
        combined = _normalize_index_to_ist_naive(combined)

        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        combined.attrs["data_source"] = f"{source}+cache"
        return combined

    def _enforce_min_history(frame: pd.DataFrame, cached_frame: pd.DataFrame, min_rows: int = 50) -> pd.DataFrame:
        """
        Guardrail: prevent single-candle outputs when cached history is available.
        """
        out = frame
        cached_prepared = _prepare_ohlc(cached_frame, "cache") if cached_frame is not None else None
        if out is None or out.empty:
            return cached_prepared
        if cached_prepared is None or cached_prepared.empty:
            return out
        if len(out) >= min_rows:
            return out
        merged = _merge_with_cached_history(out, cached_prepared, str(out.attrs.get("data_source", "fresh")))
        return merged if merged is not None and not merged.empty else cached_prepared

    try:
        cached_df = load_cached_data("NSEBANK", interval)
        backup_df = _load_backup_history("NSEBANK", interval)
        if cached_df is None or cached_df.empty or len(cached_df) < 10:
            if backup_df is not None and not backup_df.empty:
                cached_df = backup_df
        else:
            if backup_df is not None and not backup_df.empty and len(cached_df) < max(200, len(backup_df) // 3):
                cached_df = _merge_with_cached_history(backup_df, cached_df, "cache")

        if not force_refresh and cached_df is not None and not cached_df.empty:
            out = _prepare_ohlc(cached_df, "cache")
            print(f"✅ Loaded {len(out)} rows from cache ({interval})")
            out, injected = _inject_live_quote_candle(out, auth_instance, interval)
            if injected:
                out.attrs["data_source"] = "cache+upstox_quote"
                save_cached_data(out, "NSEBANK", interval)
            return out

        # Primary fresh source for this app: Upstox index candles (requires auth).
        if auth_instance is not None and getattr(auth_instance, "access_token", None):
            print(f"⏳ Fetching fresh Upstox index data ({interval})...")
            upstox_df = fetch_upstox_index_ohlc(
                auth_instance=auth_instance,
                symbol="BANKNIFTY",
                period=period,
                interval=interval,
            )
            if upstox_df is not None and not upstox_df.empty:
                out = _merge_with_cached_history(upstox_df, cached_df, "upstox")
                if backup_df is not None and not backup_df.empty:
                    out = _merge_with_cached_history(backup_df, out, "cache")
                if out is not None and not out.empty:
                    out = _enforce_min_history(out, cached_df, min_rows=50)
                    print(f"✓ Got {len(out)} rows from Upstox")
                    out, injected = _inject_live_quote_candle(out, auth_instance, interval)
                    if injected:
                        out.attrs["data_source"] = "upstox+quote"
                    save_cached_data(out, "NSEBANK", interval)
                    return out

            # When Upstox auth is active but candle refresh is temporarily unavailable,
            # prefer cache+live-quote injection over hammering yfinance (often rate-limited).
            if cached_df is not None and not cached_df.empty:
                print("ℹ️ Upstox active; skipping yfinance fallback and using cache+live quote.")
                out = _prepare_ohlc(cached_df, "cache")
                out, injected = _inject_live_quote_candle(out, auth_instance, interval)
                if injected:
                    out.attrs["data_source"] = "cache+upstox_quote"
                    save_cached_data(out, "NSEBANK", interval)
                return out

        # Secondary fresh source: yfinance (disable when Upstox auth exists to avoid rate-limit noise).
        if auth_instance is not None and getattr(auth_instance, "access_token", None):
            print("ℹ️ Upstox active; yfinance fallback disabled.")
            if cached_df is not None and not cached_df.empty:
                out = _prepare_ohlc(cached_df, "cache")
                out, injected = _inject_live_quote_candle(out, auth_instance, interval)
                if injected:
                    out.attrs["data_source"] = "cache+upstox_quote"
                    save_cached_data(out, "NSEBANK", interval)
                return out
            print("⚠️ Upstox active but no cache available; skipping yfinance.")
            return None

        # Secondary fresh source: yfinance.
        print(f"⏳ Fetching fresh yfinance data ({interval})...")
        fresh_df = fetch_nse_index_data(symbol="^NSEBANK", period=period, interval=interval)

        if fresh_df is not None and not fresh_df.empty:
            out = _merge_with_cached_history(fresh_df, cached_df, "yfinance")
            if backup_df is not None and not backup_df.empty:
                out = _merge_with_cached_history(backup_df, out, "cache")
            if out is not None and not out.empty and all(col in out.columns for col in ["Open", "High", "Low", "Close"]):
                out = _enforce_min_history(out, cached_df, min_rows=50)
                print(f"✓ Got {len(out)} rows from yfinance")
                out, injected = _inject_live_quote_candle(out, auth_instance, interval)
                if injected:
                    out.attrs["data_source"] = "yfinance+upstox_quote"
                save_cached_data(out, "NSEBANK", interval)
                return out
            print(f"❌ Missing OHLC columns in yfinance response: {list(fresh_df.columns)}")

        print("⚠️ Fresh fetch unavailable, using cache fallback")
        if cached_df is not None and not cached_df.empty:
            out = _prepare_ohlc(cached_df, "cache")
            if backup_df is not None and not backup_df.empty:
                out = _merge_with_cached_history(backup_df, out, "cache")
            print(f"✅ Loaded {len(out)} cached rows ({interval})")
            out, injected = _inject_live_quote_candle(out, auth_instance, interval)
            if injected:
                out.attrs["data_source"] = "cache+upstox_quote"
                save_cached_data(out, "NSEBANK", interval)
            return out
        return None

    except Exception as e:
        print(f"❌ Error fetching data: {e}")
        import traceback
        traceback.print_exc()
        cached_df = load_cached_data("NSEBANK", interval)
        if cached_df is not None and not cached_df.empty:
            out = _prepare_ohlc(cached_df, "cache")
            out, injected = _inject_live_quote_candle(out, auth_instance, interval)
            if injected:
                out.attrs["data_source"] = "cache+upstox_quote"
                save_cached_data(out, "NSEBANK", interval)
            return out
        return None

# Page configuration
st.set_page_config(
    page_title="BankNifty Options Trader",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Professional Light Theme with High Contrast
st.markdown("""
    <style>
    * {
        margin: 0;
        padding: 0;
    }
    html, body, [data-testid="stAppViewContainer"] {
        background: #ffffff !important;
        color: #1a1a1a !important;
    }
    [data-testid="stSidebar"] {
        background: #f5f5f5 !important;
        border-right: 2px solid #00d084 !important;
    }
    .stTabs [data-baseweb="tab-list"] {
        background-color: #ffffff !important;
        border-bottom: 2px solid #e0e0e0 !important;
        gap: 20px;
    }
    .stTabs [aria-selected="true"] {
        border-bottom-color: #00d084 !important;
        color: #00d084 !important;
    }
    [data-testid="stMetricValue"] {
        font-size: 2rem;
        font-weight: 700;
        color: #00d084 !important;
    }
    [data-testid="stMetricLabel"] {
        font-size: 0.75rem;
        color: #666666 !important;
        text-transform: uppercase;
        letter-spacing: 0.1em;
        font-weight: 600;
    }
    div.stButton > button {
        background: linear-gradient(135deg, #00d084 0%, #00a86b 100%) !important;
        color: #ffffff !important;
        border: none !important;
        border-radius: 4px !important;
        font-weight: 700 !important;
        font-size: 0.9rem !important;
        padding: 12px 24px !important;
        transition: all 0.3s !important;
        box-shadow: 0 4px 15px rgba(0, 208, 132, 0.3) !important;
    }
    div.stButton > button:hover {
        background: linear-gradient(135deg, #00a86b 0%, #008f5a 100%) !important;
        box-shadow: 0 4px 20px rgba(0, 208, 132, 0.5) !important;
        transform: translateY(-2px) !important;
    }
    .stTextInput > div > div > input {
        background: #ffffff !important;
        color: #1a1a1a !important;
        border: 2px solid #00d084 !important;
        border-radius: 4px !important;
    }
    .stSelectbox > div > div > select {
        background: #ffffff !important;
        color: #1a1a1a !important;
        border: 2px solid #00d084 !important;
    }
    .stSlider > div > div > div > div {
        background: #e0e0e0 !important;
    }
    .stMarkdown h1, .stMarkdown h2, .stMarkdown h3 {
        color: #1a1a1a !important;
        margin-top: 24px !important;
        margin-bottom: 12px !important;
    }
    .stCaption, .stText {
        color: #666666 !important;
    }
    [data-testid="stDivider"] {
        background-color: #e0e0e0 !important;
    }
    .metric-container {
        background: #f5f5f5 !important;
        border: 2px solid #00d084 !important;
        border-radius: 8px !important;
        padding: 16px !important;
        margin: 8px 0 !important;
    }
    .bullish { color: #00d084 !important; font-weight: 700; }
    .bearish { color: #ff4444 !important; font-weight: 700; }
    .neutral { color: #ff9500 !important; font-weight: 700; }
    </style>
""", unsafe_allow_html=True)


# Initialize session state
if "collector" not in st.session_state:
    st.session_state.collector = None
if "auth" not in st.session_state:
    st.session_state.auth = None
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "auth_pending" not in st.session_state:
    st.session_state.auth_pending = None
if "last_auth_code_used" not in st.session_state:
    st.session_state.last_auth_code_used = ""
if "auto_refresh" not in st.session_state:
    st.session_state.auto_refresh = True
if "ltp_history" not in st.session_state:
    st.session_state.ltp_history = {}
if "option_prev_forecasts" not in st.session_state:
    st.session_state.option_prev_forecasts = {}


def _read_upstox_cached_api_key() -> str:
    """Read cached API key from local token cache, if available."""
    try:
        cache_path = Path("data/.upstox_token_cache.json")
        if not cache_path.exists():
            payload = {}
        else:
            payload = json.loads(cache_path.read_text())
        key = str(payload.get("api_key", "") or "").strip()
        if key:
            return key
        meta_path = Path("data/.upstox_login_meta.json")
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            return str(meta.get("api_key", "") or "").strip()
        return ""
    except Exception:
        return ""


def _read_saved_upstox_credentials() -> dict:
    """Load locally saved Upstox login metadata."""
    try:
        meta_path = Path("data/.upstox_login_meta.json")
        if not meta_path.exists():
            return {}
        payload = json.loads(meta_path.read_text())
        if not isinstance(payload, dict):
            return {}
        return payload
    except Exception:
        return {}


def _remember_upstox_credentials(api_key: str, api_secret: str = "", redirect_uri: str = "") -> None:
    """Persist Upstox login metadata locally for automatic prefill."""
    try:
        key = str(api_key or "").strip()
        if not key:
            return
        meta_path = Path("data/.upstox_login_meta.json")
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        existing = _read_saved_upstox_credentials()
        secret = str(api_secret or "").strip() or str(existing.get("api_secret", "") or "").strip()
        payload = {
            "api_key": key,
            "api_secret": secret,
            "redirect_uri": str(redirect_uri or "").strip() or str(existing.get("redirect_uri", "") or "").strip(),
            "updated_at": datetime.now(IST).isoformat(),
        }
        meta_path.write_text(json.dumps(payload, indent=2))
    except Exception:
        pass


def _safe_streamlit_secret(key: str, default: str = "") -> str:
    """Return Streamlit secret value without raising when secrets.toml is absent."""
    try:
        return str(st.secrets.get(key, default) or "").strip()
    except Exception:
        return str(default or "").strip()


def _extract_auth_code(raw_value) -> str:
    """Extract authorization code from query value, full URL, or plain code text."""
    try:
        if isinstance(raw_value, (list, tuple)):
            raw_value = raw_value[0] if raw_value else ""
        s = str(raw_value or "").strip()
        if not s:
            return ""
        # Full URL pasted instead of bare code.
        if "code=" in s:
            try:
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(s)
                qs = parse_qs(parsed.query)
                if qs.get("code"):
                    return str(qs.get("code")[0] or "").strip()
            except Exception:
                pass
            try:
                tail = s.split("code=", 1)[1]
                return tail.split("&", 1)[0].strip()
            except Exception:
                pass
        return s
    except Exception:
        return ""


def _has_live_upstox_quote(auth_instance) -> bool:
    """
    Validate auth by requesting a live BankNifty index quote.
    """
    if auth_instance is None or not getattr(auth_instance, "access_token", None):
        return False
    for key in ["NSE_INDEX|Nifty Bank", "NSE_INDEX|NIFTY BANK", "NSE_INDEX|BANKNIFTY"]:
        try:
            quote = auth_instance.get_market_quote([key])
            data = (quote or {}).get("data", {}) if isinstance(quote, dict) else {}
            payload = data.get(key) if isinstance(data, dict) else None
            if payload is None and isinstance(data, dict) and len(data) == 1:
                payload = next(iter(data.values()))
            if not isinstance(payload, dict):
                continue
            ltp = pd.to_numeric(payload.get("last_price", payload.get("ltp")), errors="coerce")
            if np.isfinite(ltp):
                return True
        except Exception:
            continue
    return False

# ============================================================================
# SIDEBAR - Configuration
# ============================================================================

with st.sidebar.expander("🔐 Upstox Login", expanded=False):
    cached_api_key = _read_upstox_cached_api_key()
    saved_creds = _read_saved_upstox_credentials()
    saved_api_key = str(saved_creds.get("api_key", "") or "").strip()
    saved_api_secret = str(saved_creds.get("api_secret", "") or "").strip()
    saved_redirect_uri = str(saved_creds.get("redirect_uri", "") or "").strip()
    secrets_api_key = _safe_streamlit_secret("UPSTOX_API_KEY") or _safe_streamlit_secret("UPSTOX_CLIENT_ID")
    secrets_api_secret = _safe_streamlit_secret("UPSTOX_API_SECRET") or _safe_streamlit_secret("UPSTOX_CLIENT_SECRET")
    secrets_redirect_uri = _safe_streamlit_secret("UPSTOX_REDIRECT_URI")
    default_api_key = (
        os.environ.get("UPSTOX_API_KEY", "")
        or os.environ.get("UPSTOX_CLIENT_ID", "")
        or secrets_api_key
        or saved_api_key
        or cached_api_key
    )
    default_api_secret = (
        os.environ.get("UPSTOX_API_SECRET", "")
        or os.environ.get("UPSTOX_CLIENT_SECRET", "")
        or secrets_api_secret
        or saved_api_secret
    )
    default_redirect_uri = os.environ.get("UPSTOX_REDIRECT_URI", "") or secrets_redirect_uri or saved_redirect_uri or "http://localhost:8501"

    api_key = st.text_input(
        "API Key",
        value=default_api_key,
        type="password"
    )
    
    api_secret = st.text_input(
        "API Secret",
        value=default_api_secret,
        type="password"
    )
    
    redirect_uri = st.text_input(
        "Redirect URI",
        value=default_redirect_uri
    )

    api_key_clean = str(api_key or "").strip()
    api_secret_clean = str(api_secret or "").strip()
    redirect_uri_clean = str(redirect_uri or "").strip() or default_redirect_uri
    if not api_key_clean:
        api_key_clean = str(getattr(st.session_state.get("auth"), "api_key", "") or "").strip()
    if not api_key_clean:
        pending_obj = st.session_state.get("auth_pending")
        if isinstance(pending_obj, dict):
            api_key_clean = str(pending_obj.get("api_key", "") or "").strip()
        else:
            api_key_clean = str(getattr(pending_obj, "api_key", "") or "").strip()
    if api_key_clean:
        _remember_upstox_credentials(api_key_clean, api_secret_clean, redirect_uri_clean)

    # Auto-bootstrap from cached token on app load (no button click required).
    if (not st.session_state.authenticated) and (st.session_state.auth is None):
        try:
            bootstrap_auth = UpstoxAuth(
                api_key=api_key_clean or cached_api_key or "cached-token-only",
                api_secret=api_secret_clean or "cached-token-only",
                redirect_uri=redirect_uri_clean,
            )
            if bootstrap_auth.is_authenticated():
                st.session_state.auth = bootstrap_auth
                st.session_state.collector = OptionsBidAskCollector(upstox_auth=bootstrap_auth)
                st.session_state.authenticated = True
        except Exception:
            pass

    if st.button("🔐 Get Fresh Login Link"):
        try:
            if not api_key_clean:
                st.sidebar.error("❌ API Key is required to generate a fresh login link.")
            else:
                auth_for_link = UpstoxAuth(
                    api_key=api_key_clean,
                    api_secret=api_secret_clean or "pending-secret",
                    redirect_uri=redirect_uri_clean,
                )
                login_url = auth_for_link.get_login_url()
                st.sidebar.info("📱 Click this link to authorize:\n\n" + login_url)
                st.sidebar.warning("⏱️ Authorization code expires in 5 minutes. Complete authentication quickly!")
                st.session_state.auth_pending = {
                    "api_key": api_key_clean,
                    "redirect_uri": redirect_uri_clean,
                    "ts": datetime.now(IST).isoformat(),
                }
                if not api_secret_clean:
                    st.sidebar.warning("🔑 Enter API Secret before clicking Connect.")
        except Exception as e:
            st.sidebar.error(f"❌ Error: {str(e)}")

    query_code = _extract_auth_code(st.query_params.get("code", ""))
    # If already connected, do not keep stale auth-code in URL-driven exchange flow.
    try:
        if st.session_state.authenticated and st.session_state.auth and getattr(st.session_state.auth, "access_token", None):
            if query_code:
                st.query_params.clear()
                query_code = ""
            st.session_state.auth_pending = None
    except Exception:
        pass
    if st.session_state.get("auth_pending") or query_code:
        st.divider()
        st.sidebar.info("⏳ **Status:** Waiting for authorization code from Upstox...")
        auth_code = query_code
        if query_code:
            st.sidebar.success(f"✅ Authorization code captured from URL!")

        auth_code = st.sidebar.text_input(
            "Authorization Code [or paste manually]",
            value=auth_code,
            placeholder="Auto-fills after login..."
        )
        auth_code_clean = _extract_auth_code(auth_code)
        if auth_code_clean:
            col1, col2 = st.sidebar.columns(2)
            with col1:
                if st.button("✅ Connect", width="stretch"):
                    try:
                        if (
                            st.session_state.authenticated
                            and st.session_state.auth
                            and getattr(st.session_state.auth, "access_token", None)
                        ):
                            st.sidebar.info("✅ Upstox already connected. Skipping code exchange.")
                            st.session_state.auth_pending = None
                            st.query_params.clear()
                            st.rerun()
                        if not api_key_clean:
                            st.sidebar.error("❌ API Key is required to exchange authorization code.")
                            st.stop()
                        if not api_secret_clean:
                            st.sidebar.error("❌ API Secret is required to exchange authorization code.")
                            st.stop()
                        if auth_code_clean == str(st.session_state.get("last_auth_code_used", "") or ""):
                            st.sidebar.warning("⚠️ This authorization code was already used. Get a fresh login link.")
                            st.stop()
                        auth = UpstoxAuth(
                            api_key=api_key_clean,
                            api_secret=api_secret_clean,
                            redirect_uri=redirect_uri_clean,
                        )
                        success, message = auth.generate_access_token(auth_code_clean)
                        
                        if success:
                            st.session_state.auth = auth
                            st.session_state.collector = OptionsBidAskCollector(upstox_auth=auth)
                            st.session_state.authenticated = True
                            _remember_upstox_credentials(api_key_clean, api_secret_clean, redirect_uri_clean)
                            st.sidebar.success("✅ " + message)
                            if not _has_live_upstox_quote(auth):
                                st.sidebar.warning("⚠️ Connected, but live quote check is temporarily unavailable (likely rate-limit).")
                            st.session_state.last_auth_code_used = auth_code_clean
                            st.session_state.auth_pending = None
                            st.query_params.clear()
                            st.rerun()
                        else:
                            st.sidebar.error("❌ " + message)
                            with st.expander("🔍 Debug Info"):
                                st.code(message, language="json")
                    
                    except Exception as e:
                        st.sidebar.error(f"❌ Error: {str(e)}")
                        with st.expander("🔍 Debug Info"):
                            st.code(f"Error: {str(e)}\n\nTroubleshooting:\n- Code expired? Get a fresh one\n- Wrong code? Copy carefully from URL\n- API credentials wrong? Check API Key & Secret", language="text")
            
            with col2:
                if st.button("🔄 Retry", width="stretch"):
                    st.session_state.auth_pending = None
                    st.query_params.clear()
                    st.rerun()

if st.session_state.authenticated:
    st.sidebar.success("✅ Live Data Ready")
else:
    st.sidebar.warning("⚠️ Click button above to connect")

# Keep auth state consistent across reruns when token exists.
try:
    if (not st.session_state.authenticated) and st.session_state.auth and getattr(st.session_state.auth, "access_token", None):
        st.session_state.authenticated = True
        if st.session_state.collector is None:
            st.session_state.collector = OptionsBidAskCollector(upstox_auth=st.session_state.auth)
except Exception:
    pass

st.sidebar.divider()

# ============================================================================
# EXPIRY CALENDAR - Monthly only
# ============================================================================
st.sidebar.subheader("Expiry Calendar (Monthly)")

# Generate monthly expiry dates (last Tuesday of each month, approximate)
def get_monthly_expirations(months_ahead=8):
    """
    Generate monthly expiry dates.
    Prefer expiries available in local instrument-key mapping (best for live bid/ask lookup).
    """
    # 1) Prefer mapped expiries from collector instrument key cache.
    try:
        collector = st.session_state.get("collector")
        instrument_map = getattr(collector, "instrument_keys", {}) if collector is not None else {}
        all_strikes = instrument_map.get("all_strikes", {}) if isinstance(instrument_map, dict) else {}
        mapped = set()
        for strike_payload in all_strikes.values():
            if not isinstance(strike_payload, dict):
                continue
            for exp_str in strike_payload.keys():
                dt = pd.to_datetime(exp_str, errors="coerce")
                if pd.notna(dt):
                    mapped.add(dt.strftime("%Y-%m-%d"))
        if mapped:
            today = pd.Timestamp.now().normalize()
            mapped_sorted = sorted([d for d in mapped if pd.to_datetime(d, errors="coerce") >= today])
            if mapped_sorted:
                return mapped_sorted[:months_ahead]
    except Exception:
        pass

    # 2) Fallback: approximate monthly expiries using last Tuesday.
    expiries = []
    base_month = pd.Timestamp.now().to_period("M")
    for i in range(months_ahead):
        month_start = (base_month + i).to_timestamp()
        last_day = month_start + pd.offsets.MonthEnd(0)
        while last_day.weekday() != 1:  # Tuesday
            last_day = last_day - pd.Timedelta(days=1)
        expiries.append(last_day.strftime("%Y-%m-%d"))
    return expiries

monthly_expiries = get_monthly_expirations()
selected_expiry = st.sidebar.selectbox(
    "Select Monthly Expiry",
    options=monthly_expiries,
    index=0,
    help="BankNifty monthly expirations only (no weekly)"
)

st.sidebar.divider()

# ============================================================================
# TRADING SETTINGS
# ============================================================================
st.sidebar.markdown("""
    <div style='background: #f5f5f5; border-bottom: 2px solid #00d084; padding: 12px; margin: -24px -24px 12px -24px; border-radius: 4px;'>
        <h3 style='color: #00d084; margin: 0; font-size: 1.2rem; font-weight: 700;'>⚙️ SETTINGS</h3>
    </div>
""", unsafe_allow_html=True)

# Fetch latest BankNifty closing price from historical data
spot_price = 59400  # Default fallback

# Try live quote first (Upstox), then local history fallback (avoid yfinance dependency here).
try:
    if st.session_state.authenticated and st.session_state.auth:
        q = st.session_state.auth.get_market_quote(["NSE_INDEX|Nifty Bank"])
        data = (q or {}).get("data", {}) if isinstance(q, dict) else {}
        payload = data.get("NSE_INDEX|Nifty Bank") if isinstance(data, dict) else None
        if payload is None and isinstance(data, dict) and len(data) == 1:
            payload = next(iter(data.values()))
        ltp = pd.to_numeric((payload or {}).get("last_price", (payload or {}).get("ltp")), errors="coerce")
        if np.isfinite(ltp):
            spot_price = float(ltp)
    if spot_price == 59400:
        local_daily = load_best_local_price_history("NSEBANK", "1d")
        if local_daily is not None and not local_daily.empty:
            spot_price = float(pd.to_numeric(local_daily["Close"].iloc[-1], errors="coerce"))
except Exception as e:
    pass

# Default strike range on valid 100-point grid (ATM ± 1000 for full chain coverage)
atm_spot_rounded = int(round(float(spot_price) / 100.0) * 100)
strike_range = (atm_spot_rounded - 1000, atm_spot_rounded + 1000)

refresh_interval = st.sidebar.slider(
    "Refresh (minutes)",
    min_value=1,
    max_value=30,
    value=1,
    step=1
) * 60  # Convert to seconds
st.session_state.auto_refresh = st.sidebar.checkbox(
    "Auto-refresh dashboard",
    value=bool(st.session_state.auto_refresh),
    help="Automatically rerun at the selected interval."
)
if st.session_state.auto_refresh:
    sleep_for, now_ist = _seconds_until_next_ist_tick(refresh_interval)
    next_refresh_ist = now_ist + timedelta(seconds=sleep_for)
    st.sidebar.caption(
        f"IST now: {now_ist.strftime('%H:%M:%S')} | Next refresh: {next_refresh_ist.strftime('%H:%M:%S')}"
    )

st.sidebar.divider()


# ============================================================================
# MAIN CONTENT
# ============================================================================

if not st.session_state.authenticated:
    if st.session_state.auth_pending:
        st.info("⏳ Authorization pending. Complete login in sidebar and click Connect.")
    else:
        st.warning("⚠️ Upstox is not connected. Running in fallback/mock mode until you reconnect in sidebar.")
    if st.session_state.collector is None:
        try:
            st.session_state.collector = OptionsBidAskCollector(upstox_auth=None)
        except Exception:
            st.session_state.collector = None

# Page title
st.markdown("""
    <div style='background: linear-gradient(90deg, #ffffff 0%, #f5f5f5 100%); padding: 24px; border-bottom: 2px solid #00d084; margin: -24px -24px 24px -24px;'>
        <h1 style='color: #00d084; margin: 0; font-size: 2.5rem; font-weight: 700;'>📈 BANKNIFTY</h1>
        <p style='color: #666666; margin: 8px 0 0 0; font-size: 0.9rem;'>Options Trading Terminal</p>
    </div>
""", unsafe_allow_html=True)

# Current market info
current_time = datetime.now(IST)
market_open = _is_market_open(current_time)

# ============================================================================
# FETCH DATA FOR ALL TABS (Before metrics/tabs)
# ============================================================================

# Initialize data container
bidask_records = []
# Fetch strikes with 100-point intervals for full coverage
strikes_to_fetch = list(range(strike_range[0], strike_range[1] + 1, 100))

# Black-Scholes pricing for forecast
from scipy.stats import norm

def black_scholes_call(S, K, T, r, sigma):
    """Calculate call option price using Black-Scholes"""
    if T <= 0 or sigma <= 0:
        return max(S - K, 0)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)

def black_scholes_put(S, K, T, r, sigma):
    """Calculate put option price using Black-Scholes"""
    if T <= 0 or sigma <= 0:
        return max(K - S, 0)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

# Market parameters for forecasting
market_params = {
    'current_price': spot_price,
    'risk_free_rate': 0.06,  # 6% annual
    'volatility': 0.20,      # 20% IV
    'expiry_date': pd.to_datetime(selected_expiry),
    'current_date': pd.Timestamp.now(),
}

# Calculate days to expiry
dte = (market_params['expiry_date'] - market_params['current_date']).days
time_to_expiry = max(dte / 365.0, 1/365.0)  # Avoid zero

# Forecast spot price movement (±2%)
forecasted_spot = spot_price * 1.02

chain_df = None
chain_expiry_used = None
if strikes_to_fetch and st.session_state.authenticated and st.session_state.auth:
    try:
        from src.upstox_data import fetch_upstox_option_chain
        chain_df, chain_expiry_used = fetch_upstox_option_chain(
            auth_instance=st.session_state.auth,
            symbol="BANKNIFTY",
            expiry=selected_expiry,
        )
    except Exception:
        chain_df = None

def _empty_leg(source: str = "missing"):
    return {
        "bid": np.nan,
        "ask": np.nan,
        "spread": np.nan,
        "bid_volume": 0,
        "ask_volume": 0,
        "ltp": np.nan,
        "open_interest": 0,
        "volume": 0,
        "change": np.nan,
        "change_pct": np.nan,
        "prev_close": np.nan,
        "oi_change": np.nan,
        "total_buy_qty": np.nan,
        "total_sell_qty": np.nan,
        "source": source,
    }

def _chain_leg_from_row(row, source: str = "upstox_chain"):
    if row is None:
        return None
    bid = float(pd.to_numeric(row.get("Bid", np.nan), errors="coerce"))
    ask = float(pd.to_numeric(row.get("Ask", np.nan), errors="coerce"))
    spread = float(pd.to_numeric(row.get("Spread", np.nan), errors="coerce"))
    ltp = float(pd.to_numeric(row.get("Price", np.nan), errors="coerce"))
    bid_qty = int(pd.to_numeric(row.get("BidQty", 0), errors="coerce") or 0)
    ask_qty = int(pd.to_numeric(row.get("AskQty", 0), errors="coerce") or 0)
    oi_val = int(pd.to_numeric(row.get("OI", 0), errors="coerce") or 0)
    vol_val = int(pd.to_numeric(row.get("Volume", 0), errors="coerce") or 0)
    return {
        "bid": bid,
        "ask": ask,
        "spread": spread,
        "bid_volume": bid_qty,
        "ask_volume": ask_qty,
        "ltp": ltp,
        "open_interest": oi_val,
        "volume": vol_val,
        "change": float(pd.to_numeric(row.get("Change", np.nan), errors="coerce")),
        "change_pct": float(pd.to_numeric(row.get("NetChange", np.nan), errors="coerce")),
        "prev_close": float(pd.to_numeric(row.get("PrevClose", np.nan), errors="coerce")),
        "oi_change": float(pd.to_numeric(row.get("OI_Change", np.nan), errors="coerce")),
        "total_buy_qty": float(pd.to_numeric(row.get("TotalBuyQty", np.nan), errors="coerce")),
        "total_sell_qty": float(pd.to_numeric(row.get("TotalSellQty", np.nan), errors="coerce")),
        "source": source,
    }


def _merge_leg_data(primary: dict | None, fallback: dict | None) -> dict | None:
    if primary is None:
        return fallback
    if fallback is None:
        return primary
    merged = dict(primary)
    for key in (
        "bid", "ask", "spread", "ltp", "bid_volume", "ask_volume", "open_interest",
        "volume", "change", "change_pct", "prev_close", "oi_change",
        "total_buy_qty", "total_sell_qty",
    ):
        primary_val = merged.get(key)
        fallback_val = fallback.get(key)
        if isinstance(primary_val, (int, float, np.integer, np.floating)):
            primary_missing = not np.isfinite(primary_val)
        else:
            primary_missing = primary_val in (None, "", 0) and key not in {"bid_volume", "ask_volume", "open_interest", "volume"}
        if isinstance(fallback_val, (int, float, np.integer, np.floating)):
            fallback_ok = np.isfinite(fallback_val)
        else:
            fallback_ok = fallback_val not in (None, "")
        if primary_missing and fallback_ok:
            merged[key] = fallback_val
    primary_source = str(primary.get("source", "")).strip()
    fallback_source = str(fallback.get("source", "")).strip()
    if fallback_source and fallback_source.lower() not in primary_source.lower():
        merged["source"] = "+".join([part for part in (primary_source, fallback_source) if part])
    return merged

def _pick_chain_row(chain_df, strike: float, option_type: str):
    if chain_df is None or chain_df.empty:
        return None
    df = chain_df
    row = df[
        (df["Strike"] == float(strike)) &
        (df["Type"] == str(option_type).upper())
    ]
    if row.empty:
        row = df[
            (df["Strike"] - float(strike)).abs() < 0.5
        ]
        row = row[row["Type"] == str(option_type).upper()]
    if row.empty:
        return None
    return row.iloc[0]

chain_mode = False
if strikes_to_fetch and isinstance(chain_df, pd.DataFrame) and not chain_df.empty:
    chain_mode = True
    chain_df = chain_df.copy()
    chain_df["Strike"] = pd.to_numeric(chain_df["Strike"], errors="coerce")
    chain_df["Type"] = chain_df["Type"].astype(str).str.upper()
    chain_df = chain_df.dropna(subset=["Strike"])
    chain_df = chain_df[chain_df["Strike"].isin(strikes_to_fetch)]

if strikes_to_fetch:
    bulk_bidask = {}
    if st.session_state.collector is not None:
        try:
            bulk_bidask = st.session_state.collector.fetch_option_bidask_bulk(
                symbol="BANKNIFTY",
                strikes=strikes_to_fetch,
                expiry=selected_expiry,
                option_types=("CE", "PE"),
            )
        except Exception:
            bulk_bidask = {}

    for strike in strikes_to_fetch:
        try:
            # Fetch CE and PE data
            ce_data = None
            pe_data = None
            if chain_mode:
                ce_row = _pick_chain_row(chain_df, strike, "CE")
                pe_row = _pick_chain_row(chain_df, strike, "PE")
                ce_data = _chain_leg_from_row(ce_row, source="upstox_chain")
                pe_data = _chain_leg_from_row(pe_row, source="upstox_chain")

            ce_data = _merge_leg_data(ce_data, (bulk_bidask or {}).get((strike, "CE")))
            pe_data = _merge_leg_data(pe_data, (bulk_bidask or {}).get((strike, "PE")))
            if ce_data is None and st.session_state.collector is not None:
                ce_data = st.session_state.collector.fetch_option_bidask(
                    symbol="BANKNIFTY",
                    strike=strike,
                    expiry=selected_expiry,
                    option_type="CE"
                )
            if pe_data is None and st.session_state.collector is not None:
                pe_data = st.session_state.collector.fetch_option_bidask(
                    symbol="BANKNIFTY",
                    strike=strike,
                    expiry=selected_expiry,
                    option_type="PE"
                )

            ce_data = ce_data or _empty_leg("missing")
            pe_data = pe_data or _empty_leg("missing")
            
            # Calculate forecasted LTP using Black-Scholes
            ce_forecast = black_scholes_call(
                forecasted_spot,
                strike,
                time_to_expiry,
                market_params['risk_free_rate'],
                market_params['volatility']
            )
            
            pe_forecast = black_scholes_put(
                forecasted_spot,
                strike,
                time_to_expiry,
                market_params['risk_free_rate'],
                market_params['volatility']
            )
            
            # Calculate change from current
            ce_current = ce_data.get('ltp', 0)
            pe_current = pe_data.get('ltp', 0)
            _update_ltp_history_snapshot(
                strike,
                ce_current,
                pe_current,
                ce_source=ce_data.get("source", ""),
                pe_source=pe_data.get("source", ""),
            )
            ce_change = ((ce_forecast - ce_current) / (ce_current + 0.01)) * 100 if ce_current > 0 else 0
            pe_change = ((pe_forecast - pe_current) / (pe_current + 0.01)) * 100 if pe_current > 0 else 0

            ce_bid = ce_data.get('bid', np.nan)
            ce_ask = ce_data.get('ask', np.nan)
            pe_bid = pe_data.get('bid', np.nan)
            pe_ask = pe_data.get('ask', np.nan)
            ce_bid_vol = float(pd.to_numeric(ce_data.get('bid_volume', 0), errors="coerce"))
            ce_ask_vol = float(pd.to_numeric(ce_data.get('ask_volume', 0), errors="coerce"))
            pe_bid_vol = float(pd.to_numeric(pe_data.get('bid_volume', 0), errors="coerce"))
            pe_ask_vol = float(pd.to_numeric(pe_data.get('ask_volume', 0), errors="coerce"))
            ce_oi = float(pd.to_numeric(ce_data.get('open_interest', np.nan), errors="coerce"))
            pe_oi = float(pd.to_numeric(pe_data.get('open_interest', np.nan), errors="coerce"))
            ce_volume = float(pd.to_numeric(ce_data.get('volume', np.nan), errors="coerce"))
            pe_volume = float(pd.to_numeric(pe_data.get('volume', np.nan), errors="coerce"))
            ce_oi_change = float(pd.to_numeric(ce_data.get('oi_change', np.nan), errors="coerce"))
            pe_oi_change = float(pd.to_numeric(pe_data.get('oi_change', np.nan), errors="coerce"))
            ce_total_buy_qty = float(pd.to_numeric(ce_data.get('total_buy_qty', np.nan), errors="coerce"))
            ce_total_sell_qty = float(pd.to_numeric(ce_data.get('total_sell_qty', np.nan), errors="coerce"))
            pe_total_buy_qty = float(pd.to_numeric(pe_data.get('total_buy_qty', np.nan), errors="coerce"))
            pe_total_sell_qty = float(pd.to_numeric(pe_data.get('total_sell_qty', np.nan), errors="coerce"))
            ce_market_change = float(pd.to_numeric(ce_data.get('change', np.nan), errors="coerce"))
            pe_market_change = float(pd.to_numeric(pe_data.get('change', np.nan), errors="coerce"))
            ce_market_change_pct = float(pd.to_numeric(ce_data.get('change_pct', np.nan), errors="coerce"))
            pe_market_change_pct = float(pd.to_numeric(pe_data.get('change_pct', np.nan), errors="coerce"))
            ce_prev_close = float(pd.to_numeric(ce_data.get('prev_close', np.nan), errors="coerce"))
            pe_prev_close = float(pd.to_numeric(pe_data.get('prev_close', np.nan), errors="coerce"))
            if not np.isfinite(ce_market_change_pct) and np.isfinite(ce_prev_close) and ce_prev_close > 0 and np.isfinite(ce_market_change):
                ce_market_change_pct = (ce_market_change / ce_prev_close) * 100.0
            if not np.isfinite(pe_market_change_pct) and np.isfinite(pe_prev_close) and pe_prev_close > 0 and np.isfinite(pe_market_change):
                pe_market_change_pct = (pe_market_change / pe_prev_close) * 100.0
            ce_spread = ce_ask - ce_bid if np.isfinite(ce_ask) and np.isfinite(ce_bid) else float(pd.to_numeric(ce_data.get('spread', np.nan), errors="coerce"))
            pe_spread = pe_ask - pe_bid if np.isfinite(pe_ask) and np.isfinite(pe_bid) else float(pd.to_numeric(pe_data.get('spread', np.nan), errors="coerce"))

            def _derive_flow_metrics(current_ltp, bid_qty, ask_qty, total_buy_qty, total_sell_qty, volume_val, oi_change_val, change_pct_val):
                bid_qty = float(pd.to_numeric(bid_qty, errors="coerce"))
                ask_qty = float(pd.to_numeric(ask_qty, errors="coerce"))
                total_buy_qty = float(pd.to_numeric(total_buy_qty, errors="coerce"))
                total_sell_qty = float(pd.to_numeric(total_sell_qty, errors="coerce"))
                volume_val = float(pd.to_numeric(volume_val, errors="coerce"))
                oi_change_val = float(pd.to_numeric(oi_change_val, errors="coerce"))
                change_pct_val = float(pd.to_numeric(change_pct_val, errors="coerce"))

                flow_bid = bid_qty if np.isfinite(bid_qty) and bid_qty > 0 else np.nan
                flow_ask = ask_qty if np.isfinite(ask_qty) and ask_qty > 0 else np.nan

                if not np.isfinite(flow_bid) and np.isfinite(total_buy_qty) and total_buy_qty > 0:
                    flow_bid = total_buy_qty
                if not np.isfinite(flow_ask) and np.isfinite(total_sell_qty) and total_sell_qty > 0:
                    flow_ask = total_sell_qty

                if np.isfinite(flow_bid) and np.isfinite(flow_ask) and (flow_bid + flow_ask) > 0:
                    net_qty = flow_bid - flow_ask
                    imbalance = net_qty / max(flow_bid + flow_ask, 1.0)
                else:
                    activity = max(
                        volume_val if np.isfinite(volume_val) else 0.0,
                        abs(oi_change_val) if np.isfinite(oi_change_val) else 0.0,
                        0.0,
                    )
                    direction = np.sign(change_pct_val) if np.isfinite(change_pct_val) else 0.0
                    if direction == 0 and np.isfinite(oi_change_val):
                        direction = np.sign(oi_change_val)
                    if direction == 0:
                        direction = 1.0
                    net_qty = direction * activity
                    imbalance = direction if activity > 0 else 0.0

                flow_value = float(current_ltp) * net_qty if np.isfinite(current_ltp) else 0.0
                oi_component = np.tanh(oi_change_val / max(abs(oi_change_val), 1.0)) if np.isfinite(oi_change_val) else 0.0
                px_component = np.tanh(change_pct_val / 6.0) if np.isfinite(change_pct_val) else 0.0
                vol_component = np.tanh(max(volume_val, 0.0) / 50000.0) if np.isfinite(volume_val) else 0.0
                flow_score = (0.45 * imbalance) + (0.25 * px_component) + (0.20 * oi_component) + (0.10 * vol_component)
                return float(net_qty), float(imbalance), float(flow_value), float(flow_score)

            ce_net_qty, ce_imbalance, ce_flow_value, ce_flow_score = _derive_flow_metrics(
                ce_current, ce_bid_vol, ce_ask_vol, ce_total_buy_qty, ce_total_sell_qty, ce_volume, ce_oi_change, ce_market_change_pct
            )
            pe_net_qty, pe_imbalance, pe_flow_value, pe_flow_score = _derive_flow_metrics(
                pe_current, pe_bid_vol, pe_ask_vol, pe_total_buy_qty, pe_total_sell_qty, pe_volume, pe_oi_change, pe_market_change_pct
            )
            
            bidask_records.append({
                'Strike': strike,
                'CE Bid': ce_bid,
                'CE Ask': ce_ask,
                'CE Bid Vol': ce_bid_vol,
                'CE Ask Vol': ce_ask_vol,
                'CE LTP': ce_current,
                'CE Source': ce_data.get('source', 'unknown'),
                'CE Forecast': ce_forecast,
                'CE Change %': ce_change,
                'CE Spread': ce_spread,
                'CE Volume': ce_volume,
                'CE OI': ce_oi,
                'CE OI Change': ce_oi_change,
                'CE Net Qty': ce_net_qty,
                'CE Imbalance': ce_imbalance,
                'CE Flow Value': ce_flow_value,
                'CE Flow Score': ce_flow_score,
                'PE Bid': pe_bid,
                'PE Ask': pe_ask,
                'PE Bid Vol': pe_bid_vol,
                'PE Ask Vol': pe_ask_vol,
                'PE LTP': pe_current,
                'PE Source': pe_data.get('source', 'unknown'),
                'PE Forecast': pe_forecast,
                'PE Change %': pe_change,
                'PE Spread': pe_spread,
                'PE Volume': pe_volume,
                'PE OI': pe_oi,
                'PE OI Change': pe_oi_change,
                'PE Net Qty': pe_net_qty,
                'PE Imbalance': pe_imbalance,
                'PE Flow Value': pe_flow_value,
                'PE Flow Score': pe_flow_score,
            })
        except Exception as e:
            st.warning(f"Error fetching strike {strike}: {str(e)[:60]}")

# ============================================================================
# CALCULATE METRICS (After data fetching)
# ============================================================================

atm_strike = int((spot_price // 100) * 100)
atm_ce_ltp = 0
atm_pe_ltp = 0
pcr_total = 1.0
pcr_near = 1.0
max_pain = atm_strike
signal = "NEUTRAL"

if bidask_records:
    df_metrics = pd.DataFrame(bidask_records)
    
    # ATM Straddle (sum of CE and PE at ATM)
    atm_row = df_metrics[df_metrics['Strike'] == float(atm_strike)]
    if not atm_row.empty:
        atm_ce_ltp = float(atm_row.iloc[0]['CE LTP'])
        atm_pe_ltp = float(atm_row.iloc[0]['PE LTP'])
    
    # Simple PCR estimation (based on bid-ask spread patterns)
    ce_spreads = df_metrics['CE Spread'].mean()
    pe_spreads = df_metrics['PE Spread'].mean()
    pcr_total = max(pe_spreads / (ce_spreads + 0.01), 0.5)
    pcr_near = pcr_total * 0.95  # Slightly lower for near-term
    
    # Max Pain estimate (typically between strikes with highest OI)
    avg_ce_ltps = df_metrics['CE LTP'].mean()
    avg_pe_ltps = df_metrics['PE LTP'].mean()
    max_pain = (atm_strike + spot_price) / 2
    
    # Signal based on forecast direction
    avg_ce_change = df_metrics['CE Change %'].mean()
    if avg_ce_change > 2:
        signal = "BULLISH"
    elif avg_ce_change < -2:
        signal = "BEARISH"
    else:
        signal = "NEUTRAL"

atm_straddle = atm_ce_ltp + atm_pe_ltp

# Key metrics row - matching original app.py structure
col1, col2, col3, col4, col5, col6 = st.columns(6)

col1.metric("Spot", f"₹ {spot_price:,.0f}", "+120")
col2.metric("Signal", signal, delta_color="off")
col3.metric("PCR (Total)", f"{pcr_total:.2f}", f"{(pcr_total-1)*100:+.1f}%")
col4.metric("PCR (Near)", f"{pcr_near:.2f}", f"{(pcr_near-1)*100:+.1f}%")
col5.metric("Max Pain", f"₹ {max_pain:,.0f}", f"+{max_pain-spot_price:.0f}")
col6.metric("ATM Straddle", f"₹ {atm_straddle:.2f}", f"+{atm_straddle-100:.1f}")

# Insight card
st.markdown("""
    <div class="insight-card">
        <div class="insight-title">Trader Bias</div>
        <div class="insight-value">""" + signal + """</div>
        <div style="color:#94a3b8;font-size:0.85rem;">
            Spot: ₹{:,} | Expiry: {} | Strike Range: {} - {} | Status: {}
        </div>
    </div>
""".format(spot_price, selected_expiry, strike_range[0], strike_range[1], "OPEN" if market_open else "CLOSED"), 
unsafe_allow_html=True)

st.divider()

# Main tabs - matching original app.py structure
tab1, tab2, tab3, tab4 = st.tabs([
    "Price Action",
    "Option Flow",
    "Option Chain",
    "Option LTP Forecast"
])

with tab1:
    st.subheader("💹 Price Action - BankNifty Candlestick")
    
    # Interval selector and refresh
    col1, col2, col_refresh = st.columns([1, 2, 1])
    
    with col1:
        time_interval = st.radio(
            "Interval",
            options=["5m", "15m", "1h", "1d"],
            horizontal=True
        )
    
    with col2:
        # Add auto-refresh option
        auto_refresh_enabled = st.checkbox("🔄 Auto-refresh (every 30s)", value=True, key="chart_auto_refresh")
    
    with col_refresh:
        st.write("")
        st.write("")
        if st.button("⟳ Manual Refresh", key="tab1_refresh_btn"):
            # Clear cache to force fresh data fetch
            if f"banknifty_data_cache_{time_interval}" in st.session_state:
                del st.session_state[f"banknifty_data_cache_{time_interval}"]
            st.rerun()
    
    # Map interval to period
    period_map = {"5m": "180d", "15m": "60d", "1h": "60d", "1d": "1y"}
    period = period_map.get(time_interval, "180d")
    
    # Load data - always force fresh fetch to get latest candles
    st.write(f"Loading {time_interval} data...")
    
    # Add cache timestamp to force fresh fetch on each page load
    import time as time_module
    current_timestamp = time_module.time()
    
    df_price = fetch_banknifty_data(
        period=period,
        interval=time_interval,
        force_refresh=True,  # Always fetch fresh data, not cached
        auth_instance=st.session_state.auth,
    )
    
    # Trigger auto-refresh if enabled
    if auto_refresh_enabled:
        st.info("Updates every 30 seconds - check network tab for fresh data fetches. "
                "If data stops updating, click Manual Refresh or check Upstox auth status.")

    if isinstance(df_price, pd.DataFrame) and len(df_price) < 10:
        recovered_df = load_best_local_price_history("NSEBANK", time_interval)
        if recovered_df is not None and not recovered_df.empty and len(recovered_df) > len(df_price):
            df_price = recovered_df
            st.info("Recovered chart history from local cache backup.")
    
    # Display data status
    if df_price is None:
        st.error("❌ Failed to load data")
        st.info("Debug: fetch_banknifty_data returned None")
    elif not isinstance(df_price, pd.DataFrame):
        st.error(f"❌ Invalid data type: {type(df_price)}")
    elif df_price.empty:
        st.error("❌ DataFrame is empty")
    else:
        # Data is valid - show summary
        st.success(f"✅ Loaded {len(df_price)} candles for {time_interval}")
        data_source = str(df_price.attrs.get("data_source", "unknown")).lower()
        live_quote_injected = bool(df_price.attrs.get("live_quote_injected", False))
        live_ltp = pd.to_numeric(df_price.attrs.get("live_ltp", np.nan), errors="coerce")
        last_candle = pd.to_datetime(df_price.index[-1], errors="coerce")
        
        # Detect if candles are too frequent (indicating wrong interval)
        detected_interval_minutes = None
        if len(df_price) > 1:
            time_diffs = df_price.index.to_series().diff().dt.total_seconds() / 60
            median_diff = time_diffs[time_diffs > 0].median()
            if pd.notna(median_diff):
                detected_interval_minutes = round(median_diff)
                # Force resample if detected interval doesn't match requested
                expected_minutes = {"5m": 5, "15m": 15, "1h": 60, "1d": 1440}
                expected_interval = expected_minutes.get(time_interval, 5)
                
                if detected_interval_minutes < expected_interval / 2 and time_interval != "1m":
                    # Auto-resample to fix the issue
                    st.info(f"🔧 Auto-resampling {detected_interval_minutes}m data to {time_interval}...")
                    try:
                        from src.upstox_data import _resample_candles
                        df_price = _resample_candles(
                            df_price.reset_index().rename(columns={'index': 'Time'}),
                            interval_minutes=expected_interval
                        )
                        df_price = df_price.set_index('Time')
                        detected_interval_minutes = expected_interval
                    except Exception as e:
                        st.warning(f"⚠️ Auto-resample failed: {e}")
        
        if pd.notna(last_candle):
            caption_text = f"Source: {data_source} | Last candle: {last_candle.strftime('%Y-%m-%d %H:%M:%S')} IST"
            if detected_interval_minutes:
                caption_text += f" | Candle interval: {detected_interval_minutes}m"
            st.caption(caption_text)
            if live_quote_injected and np.isfinite(live_ltp):
                st.caption(f"Live quote sync active | Spot LTP: {live_ltp:.2f}")
            if time_interval in {"5m", "15m", "1h"}:
                now_ist_naive = datetime.now(IST).replace(tzinfo=None)
                lag = now_ist_naive - last_candle
                lag_minutes = int(lag.total_seconds() // 60)

                # Determine expected max lag based on interval
                max_lag_map = {"5m": 6, "15m": 20, "1h": 65, "1d": 1440}
                max_lag = max_lag_map.get(time_interval, 10)

                market_open_now = (
                    now_ist_naive.weekday() < 5
                    and (
                        (now_ist_naive.hour > 9 or (now_ist_naive.hour == 9 and now_ist_naive.minute >= 15))
                        and (now_ist_naive.hour < 15 or (now_ist_naive.hour == 15 and now_ist_naive.minute <= 35))
                    )
                )

                if not market_open_now:
                    st.info(
                        f"Market closed in IST. Last candle lag {lag_minutes} min is expected overnight."
                    )
                elif lag_minutes > max_lag:
                    st.warning(
                        f"⚠️ Data lag: {lag_minutes} min (expected max {max_lag}m). "
                        f"Live quote sync may be delayed. Click Refresh or re-login Upstox."
                    )
                elif lag_minutes > max_lag / 2:
                    st.info(f"ℹ️ Last candle: {lag_minutes} min ago (updating...)")
        
        # Verify columns
        cols_available = list(df_price.columns)
        required_cols = ['Open', 'High', 'Low', 'Close']
        
        if not all(col in df_price.columns for col in required_cols):
            st.error(f"❌ Missing columns. Have: {cols_available}")
        else:
            # Calculate Supertrend
            try:
                df_with_trend = calculate_supertrend(df_price.copy(), period=7, multiplier=3.0)
                has_trend = 'Supertrend' in df_with_trend.columns
            except Exception as e:
                st.warning(f"⚠️ Supertrend failed: {e}")
                df_with_trend = df_price.copy()
                has_trend = False
            
            # Create Plotly chart
            fig = go.Figure()
            
            # Candlestick trace
            fig.add_trace(go.Candlestick(
                x=df_with_trend.index,
                open=df_with_trend['Open'],
                high=df_with_trend['High'],
                low=df_with_trend['Low'],
                close=df_with_trend['Close'],
                name=f'BankNifty {time_interval}'
            ))
            
            # Trend line (if available)
            if has_trend:
                fig.add_trace(go.Scatter(
                    x=df_with_trend.index,
                    y=df_with_trend['Supertrend'],
                    mode='lines',
                    name='Supertrend',
                    line=dict(color='#00d084', width=2),
                    opacity=0.8
                ))

            # AI forecast line + confidence band
            ai_steps_map = {"5m": 12, "15m": 8, "1h": 6, "1d": 5}
            ai_steps = ai_steps_map.get(time_interval, 8)
            ai_forecast = _build_ai_forecast(df_with_trend["Close"], steps=ai_steps)
            last_ts = pd.to_datetime(df_with_trend.index[-1], errors="coerce")
            if ai_forecast is not None and pd.notna(last_ts):
                step_delta = _interval_to_timedelta(time_interval)
                future_times = [last_ts + (step_delta * (i + 1)) for i in range(ai_steps)]

                fig.add_trace(go.Scatter(
                    x=future_times,
                    y=ai_forecast["median"],
                    mode='lines+markers',
                    name='AI Forecast',
                    line=dict(color='#7c3aed', width=3),
                    marker=dict(size=6),
                    hovertemplate='<b>AI Forecast</b><br>%{x}<br>Price: ₹%{y:.2f}<extra></extra>'
                ))

                fig.add_trace(go.Scatter(
                    x=future_times + future_times[::-1],
                    y=ai_forecast["upper"] + ai_forecast["lower"][::-1],
                    fill='toself',
                    fillcolor='rgba(124, 58, 237, 0.12)',
                    line=dict(color='rgba(255,255,255,0)'),
                    showlegend=False,
                    hoverinfo='skip',
                    name='AI Confidence'
                ))
                st.caption(
                    f"AI Forecast: {ai_forecast['direction']} | "
                    f"Confidence: {ai_forecast['confidence']:.0f}% | Horizon: {ai_steps} steps"
                )
                if isinstance(ai_forecast, dict):
                    b_model = str(ai_forecast.get("model", "adaptive_momentum"))
                    b_mae = float(pd.to_numeric((ai_forecast.get("metrics") or {}).get("walk_mae_h1", np.nan), errors="coerce"))
                    if np.isfinite(b_mae):
                        st.caption(f"Forecast model: {b_model} | Walk MAE(h1): {b_mae:.2f}")
                    else:
                        st.caption(f"Forecast model: {b_model}")
            
            # Layout
            fig.update_layout(
                title=f"BankNifty {time_interval} | {len(df_with_trend)} candles",
                xaxis_title="Time",
                yaxis_title="Price (₹)",
                height=600,
                template='plotly_white',
                hovermode='x unified',
                xaxis_rangeslider_visible=False
            )
            
            st.plotly_chart(fig, width="stretch")

with tab2:
    if not bidask_records:
        st.warning("No data available.")
    else:
        df_flow = pd.DataFrame(bidask_records)
        numeric_flow_cols = [
            "CE Spread", "PE Spread", "CE Change %", "PE Change %",
            "CE Net Qty", "PE Net Qty", "CE Imbalance", "PE Imbalance",
            "CE Flow Value", "PE Flow Value", "CE Flow Score", "PE Flow Score",
            "CE Volume", "PE Volume", "CE OI", "PE OI",
        ]
        for col in numeric_flow_cols:
            if col in df_flow.columns:
                df_flow[col] = pd.to_numeric(df_flow[col], errors="coerce").fillna(0.0)
        
        left_chart, right_chart = st.columns([2, 1])
        
        with left_chart:
            
            spread_fig = go.Figure()
            
            # CE spreads (negative, going left)
            spread_fig.add_trace(go.Bar(
                y=df_flow['Strike'],
                x=-df_flow['CE Spread'],
                orientation='h',
                name='CE Spread',
                marker_color='rgba(34, 197, 94, 0.7)',
            ))
            
            # PE spreads (positive, going right)
            spread_fig.add_trace(go.Bar(
                y=df_flow['Strike'],
                x=df_flow['PE Spread'],
                orientation='h',
                name='PE Spread',
                marker_color='rgba(239, 68, 68, 0.7)',
            ))
            
            spread_fig.add_vline(x=0, line_color="#94a3b8", line_width=1)
            spread_fig.update_layout(
                template='plotly_white',
                height=400,
                xaxis_title='CE Spread <- -> PE Spread',
                yaxis_title='Strike',
                barmode='relative',
                showlegend=True,
                hovermode='y'
            )
            
            st.plotly_chart(spread_fig, width="stretch")
        
        with right_chart:
            st.subheader("Flow Metrics")
            
            # Calculate metrics
            total_ce_spread = df_flow['CE Spread'].sum()
            total_pe_spread = df_flow['PE Spread'].sum()
            spread_ratio = total_pe_spread / (total_ce_spread + 0.01) if total_ce_spread > 0 else 1.0
            total_ce_flow = float(df_flow.get('CE Flow Value', pd.Series(dtype=float)).sum())
            total_pe_flow = float(df_flow.get('PE Flow Value', pd.Series(dtype=float)).sum())
            net_ce_qty = float(df_flow.get('CE Net Qty', pd.Series(dtype=float)).sum())
            net_pe_qty = float(df_flow.get('PE Net Qty', pd.Series(dtype=float)).sum())
            
            st.metric("Total CE Spread", f"₹ {total_ce_spread:,.0f}", delta_color="off")
            st.metric("Total PE Spread", f"₹ {total_pe_spread:,.0f}", delta_color="off")
            st.metric("Spread Ratio", f"{spread_ratio:.2f}", delta_color="off")
            st.metric("CE Flow Value", f"₹ {total_ce_flow:,.0f}", delta_color="off")
            st.metric("PE Flow Value", f"₹ {total_pe_flow:,.0f}", delta_color="off")
            st.metric("CE Net Qty", f"{net_ce_qty:,.0f}", delta_color="off")
            st.metric("PE Net Qty", f"{net_pe_qty:,.0f}", delta_color="off")
            
            # Buy/Sell imbalance indicator
            st.divider()
            st.subheader("Market Sentiment")
            
            # Based on forecast changes
            avg_ce_change = df_flow['CE Change %'].mean()
            avg_pe_change = df_flow['PE Change %'].mean()
            net_bias = (avg_ce_change + avg_pe_change) / 2
            
            if net_bias > 1:
                sentiment = "🟢 BULLISH"
            elif net_bias < -1:
                sentiment = "🔴 BEARISH"
            else:
                sentiment = "⚪ NEUTRAL"
            
            st.metric("Net Bias", sentiment, delta_color="off")
        
        # LTP change analysis
        st.divider()
        st.subheader("LTP Movement Expected by Strike")
        
        movement_fig = go.Figure()
        
        movement_fig.add_trace(go.Bar(
            x=df_flow['Strike'],
            y=df_flow['CE Change %'],
            name='CE Expected Change %',
            marker_color='rgba(34, 211, 238, 0.7)',
        ))
        
        movement_fig.add_trace(go.Bar(
            x=df_flow['Strike'],
            y=df_flow['PE Change %'],
            name='PE Expected Change %',
            marker_color='rgba(248, 113, 113, 0.7)',
        ))
        
        movement_fig.update_layout(
            template='plotly_white',
            height=350,
            xaxis_title='Strike Price',
            yaxis_title='Expected Change %',
            barmode='group',
            hovermode='x unified'
        )
        
        movement_fig.add_hline(y=0, line_dash="dash", line_color="gray")
        
        st.plotly_chart(movement_fig, width="stretch")

        st.divider()
        st.subheader("Option Flow Values")
        flow_table_cols = [
            "Strike",
            "CE Flow Value", "CE Net Qty", "CE Imbalance", "CE Volume", "CE OI",
            "PE Flow Value", "PE Net Qty", "PE Imbalance", "PE Volume", "PE OI",
        ]
        available_cols = [col for col in flow_table_cols if col in df_flow.columns]
        flow_table = df_flow[available_cols].copy()
        st.dataframe(flow_table, width="stretch", height=260)

with tab3:
    # Display full chain
    if bidask_records:
        df_chain = pd.DataFrame(bidask_records)
        
        st.dataframe(
            df_chain,
            width="stretch",
            height=400
        )

with tab4:
    _load_forecast_history()
    
    if not bidask_records:
        st.info("No strike data available. Configure strikes in sidebar.")
    else:
        view_options = ["Today", "Last 3h", "Last 6h", "Last 1d", "Last 5d", "All"]
        view_choice = st.selectbox("LTP View Window", view_options, index=0)
        df_strikes = pd.DataFrame(bidask_records)
        
        # Full strike range display
        all_strikes = sorted(df_strikes['Strike'].unique())
        
        # Option Chain data is now fetched internally by the improved LTP forecaster
        # Hidden from UI as per user request - data is used for better forecasting
        
        # Note: Option chain variables (IV, OI, Volume, etc.) are automatically fetched
        # from Upstox API and used to improve LTP forecasting accuracy
        # but are not displayed in the UI
        
        # Select strike for forecasting (persist across refreshes).
        all_strikes = sorted(df_strikes['Strike'].unique())
        atm_strike = min(all_strikes, key=lambda x: abs(x - float(spot_price))) if all_strikes else None
        previous_strike = st.session_state.get("selected_ltp_strike")
        if previous_strike not in all_strikes:
            previous_strike = atm_strike
            st.session_state["selected_ltp_strike"] = previous_strike
        selected_index = all_strikes.index(previous_strike) if previous_strike in all_strikes else 0
        selected_strike = st.selectbox(
            "Select strike for LTP forecast",
            options=all_strikes,
            index=selected_index,
            key="selected_ltp_strike",
        )
        atm_strike = selected_strike
        
        if atm_strike is None:
            st.warning("No strikes available for forecasting")
        else:
            st.subheader(f"LTP Forecast - ATM Strike ₹{atm_strike:,.0f}")
            forecasting_enabled = _is_market_open()
            if not forecasting_enabled:
                st.info("Market closed (09:15–15:30 IST). Forecasting paused; showing saved forecasts only.")
            
            # Get strike row data
            strike_row = df_strikes[df_strikes['Strike'] == atm_strike].iloc[0] if not df_strikes[df_strikes['Strike'] == atm_strike].empty else None
            
            if strike_row is not None:
                # Load full option candle history so the forecaster gets real volume/OI features.
                ce_live_hist = _coerce_series_to_ist(_get_ltp_history_series(atm_strike, "CE").tail(720))
                pe_live_hist = _coerce_series_to_ist(_get_ltp_history_series(atm_strike, "PE").tail(720))
                ce_frame, _ = _load_fetch_strike_option_frame_history(
                    auth_instance=st.session_state.get("auth"),
                    symbol="BANKNIFTY",
                    expiry=selected_expiry,
                    strike=float(atm_strike),
                    option_type="CE",
                    interval_minutes=5,
                    history_days=30,
                )
                pe_frame, _ = _load_fetch_strike_option_frame_history(
                    auth_instance=st.session_state.get("auth"),
                    symbol="BANKNIFTY",
                    expiry=selected_expiry,
                    strike=float(atm_strike),
                    option_type="PE",
                    interval_minutes=5,
                    history_days=30,
                )
                ce_frame = _merge_option_frames_ist(ce_frame, _series_to_option_frame(ce_live_hist))
                pe_frame = _merge_option_frames_ist(pe_frame, _series_to_option_frame(pe_live_hist))
                ce_hist = _coerce_series_to_ist(ce_frame.get("Close"))
                pe_hist = _coerce_series_to_ist(pe_frame.get("Close"))

                def _apply_view_window(series: pd.Series, choice: str) -> pd.Series:
                    if not isinstance(series.index, pd.DatetimeIndex) or series.empty:
                        return series
                    now_ts = datetime.now(IST)
                    choice = str(choice or "").lower()
                    if choice == "today":
                        cutoff = now_ts.replace(hour=0, minute=0, second=0, microsecond=0)
                    elif choice == "last 3h":
                        cutoff = now_ts - timedelta(hours=3)
                    elif choice == "last 6h":
                        cutoff = now_ts - timedelta(hours=6)
                    elif choice == "last 1d":
                        cutoff = now_ts - timedelta(days=1)
                    elif choice == "last 5d":
                        cutoff = now_ts - timedelta(days=5)
                    else:
                        return series
                    return series[series.index >= cutoff]

                ce_plot_hist = _apply_view_window(ce_hist, view_choice)
                pe_plot_hist = _apply_view_window(pe_hist, view_choice)
                ce_days = _select_training_window_days(ce_hist)
                pe_days = _select_training_window_days(pe_hist)
                ce_cutoff = datetime.now(IST) - timedelta(days=ce_days)
                pe_cutoff = datetime.now(IST) - timedelta(days=pe_days)
                if isinstance(ce_hist.index, pd.DatetimeIndex):
                    ce_hist = ce_hist[ce_hist.index >= ce_cutoff]
                if isinstance(pe_hist.index, pd.DatetimeIndex):
                    pe_hist = pe_hist[pe_hist.index >= pe_cutoff]
                ce_current = float(pd.to_numeric(strike_row.get('CE LTP', np.nan), errors="coerce"))
                pe_current = float(pd.to_numeric(strike_row.get('PE LTP', np.nan), errors="coerce"))
                ce_source = str(strike_row.get("CE Source", "")).lower()
                pe_source = str(strike_row.get("PE Source", "")).lower()
                ce_bid = float(pd.to_numeric(strike_row.get('CE Bid', np.nan), errors="coerce"))
                ce_ask = float(pd.to_numeric(strike_row.get('CE Ask', np.nan), errors="coerce"))
                pe_bid = float(pd.to_numeric(strike_row.get('PE Bid', np.nan), errors="coerce"))
                pe_ask = float(pd.to_numeric(strike_row.get('PE Ask', np.nan), errors="coerce"))
                ce_spread = float(pd.to_numeric(strike_row.get('CE Spread', np.nan), errors="coerce"))
                pe_spread = float(pd.to_numeric(strike_row.get('PE Spread', np.nan), errors="coerce"))

                chain_snapshot = _fetch_chain_snapshot(st.session_state.get("auth"), atm_strike, selected_expiry)
                ce_snap = chain_snapshot.get("CE", {})
                pe_snap = chain_snapshot.get("PE", {})

                # Replace mock LTP with chain snapshot when available to avoid scale mismatch.
                ce_chain_ltp = float(pd.to_numeric(ce_snap.get("Price", np.nan), errors="coerce"))
                pe_chain_ltp = float(pd.to_numeric(pe_snap.get("Price", np.nan), errors="coerce"))
                if ("mock" in ce_source) or not np.isfinite(ce_current):
                    if np.isfinite(ce_chain_ltp):
                        ce_current = ce_chain_ltp
                if ("mock" in pe_source) or not np.isfinite(pe_current):
                    if np.isfinite(pe_chain_ltp):
                        pe_current = pe_chain_ltp
                if np.isfinite(ce_chain_ltp):
                    ce_current = ce_chain_ltp
                if np.isfinite(pe_chain_ltp):
                    pe_current = pe_chain_ltp

                ce_completed_hist = ce_hist.copy()
                pe_completed_hist = pe_hist.copy()
                ce_completed_frame = ce_frame.copy()
                pe_completed_frame = pe_frame.copy()

                ce_hist = _append_current_ltp(ce_hist, ce_current)
                pe_hist = _append_current_ltp(pe_hist, pe_current)
                ce_frame = _append_current_ltp_to_frame(ce_frame, ce_current)
                pe_frame = _append_current_ltp_to_frame(pe_frame, pe_current)

                ce_index = ce_hist.index if isinstance(ce_hist.index, pd.DatetimeIndex) else None
                pe_index = pe_hist.index if isinstance(pe_hist.index, pd.DatetimeIndex) else None
                ce_iv_series = _constant_series_from_snapshot(pd.to_numeric(ce_snap.get("IV", np.nan), errors="coerce"), ce_index)
                pe_iv_series = _constant_series_from_snapshot(pd.to_numeric(pe_snap.get("IV", np.nan), errors="coerce"), pe_index)
                ce_oi_series = _align_series_to_index(ce_frame.get("OI"), ce_index) if "OI" in ce_frame.columns else None
                pe_oi_series = _align_series_to_index(pe_frame.get("OI"), pe_index) if "OI" in pe_frame.columns else None
                ce_vol_series = _align_series_to_index(ce_frame.get("Volume"), ce_index) if "Volume" in ce_frame.columns else None
                pe_vol_series = _align_series_to_index(pe_frame.get("Volume"), pe_index) if "Volume" in pe_frame.columns else None
                ce_spread_series = _align_series_to_index(ce_frame.get("Spread"), ce_index) if "Spread" in ce_frame.columns else None
                pe_spread_series = _align_series_to_index(pe_frame.get("Spread"), pe_index) if "Spread" in pe_frame.columns else None
                ce_micro_series = _align_series_to_index(ce_frame.get("MicroPrice"), ce_index) if "MicroPrice" in ce_frame.columns else None
                pe_micro_series = _align_series_to_index(pe_frame.get("MicroPrice"), pe_index) if "MicroPrice" in pe_frame.columns else None
                ce_imb_series = _align_series_to_index(ce_frame.get("DepthImbalance"), ce_index) if "DepthImbalance" in ce_frame.columns else None
                pe_imb_series = _align_series_to_index(pe_frame.get("DepthImbalance"), pe_index) if "DepthImbalance" in pe_frame.columns else None

                ce_bid_vol = float(pd.to_numeric(strike_row.get("CE Bid Vol", np.nan), errors="coerce"))
                ce_ask_vol = float(pd.to_numeric(strike_row.get("CE Ask Vol", np.nan), errors="coerce"))
                pe_bid_vol = float(pd.to_numeric(strike_row.get("PE Bid Vol", np.nan), errors="coerce"))
                pe_ask_vol = float(pd.to_numeric(strike_row.get("PE Ask Vol", np.nan), errors="coerce"))

                if ce_spread_series is None and np.isfinite(ce_spread):
                    ce_spread_series = _constant_series_from_snapshot(ce_spread, ce_index)
                if pe_spread_series is None and np.isfinite(pe_spread):
                    pe_spread_series = _constant_series_from_snapshot(pe_spread, pe_index)
                if ce_micro_series is None and np.isfinite(ce_bid) and np.isfinite(ce_ask):
                    ce_micro_series = _constant_series_from_snapshot((ce_bid + ce_ask) / 2.0, ce_index)
                if pe_micro_series is None and np.isfinite(pe_bid) and np.isfinite(pe_ask):
                    pe_micro_series = _constant_series_from_snapshot((pe_bid + pe_ask) / 2.0, pe_index)
                if ce_imb_series is None and np.isfinite(ce_bid_vol) and np.isfinite(ce_ask_vol):
                    ce_imb_series = _constant_series_from_snapshot((ce_bid_vol - ce_ask_vol) / (ce_bid_vol + ce_ask_vol + 1e-9), ce_index)
                if pe_imb_series is None and np.isfinite(pe_bid_vol) and np.isfinite(pe_ask_vol):
                    pe_imb_series = _constant_series_from_snapshot((pe_bid_vol - pe_ask_vol) / (pe_bid_vol + pe_ask_vol + 1e-9), pe_index)

                def _build_signed_volume(hist_series, vol_series):
                    if vol_series is None:
                        return None
                    vol = pd.to_numeric(pd.Series(vol_series), errors="coerce")
                    hist = pd.to_numeric(pd.Series(hist_series), errors="coerce")
                    if vol.empty or hist.empty:
                        return None
                    if isinstance(hist.index, pd.DatetimeIndex):
                        if isinstance(vol.index, pd.DatetimeIndex):
                            vol = vol.reindex(hist.index, method="ffill")
                        else:
                            vol = vol.reset_index(drop=True).reindex(range(len(hist))).set_axis(hist.index)
                    ret_sign = np.sign(hist.diff().fillna(0.0))
                    signed_vol = (vol * ret_sign).replace([np.inf, -np.inf], np.nan).dropna()
                    return signed_vol if not signed_vol.empty else None

                if "SignedVolume" in ce_frame.columns:
                    ce_signed_vol = _align_series_to_index(ce_frame.get("SignedVolume"), ce_index)
                else:
                    ce_signed_vol = _build_signed_volume(ce_hist, ce_vol_series)
                if "SignedVolume" in pe_frame.columns:
                    pe_signed_vol = _align_series_to_index(pe_frame.get("SignedVolume"), pe_index)
                else:
                    pe_signed_vol = _build_signed_volume(pe_hist, pe_vol_series)

                def _log_feature_snapshot(label, snap, signed_vol):
                    try:
                        sv_val = np.nan
                        if signed_vol is not None:
                            sv_series = pd.to_numeric(pd.Series(signed_vol), errors="coerce").dropna()
                            if not sv_series.empty:
                                sv_val = float(sv_series.iloc[-1])
                        print(
                            f"🔎 {label} snapshot | IV={snap.get('IV')} OI={snap.get('OI')} "
                            f"VOL={snap.get('Volume')} SPR={snap.get('Spread')} IMB={snap.get('DepthImbalance')} "
                            f"MP={snap.get('MicroPrice')} SignedVol={sv_val}"
                        )
                    except Exception:
                        pass

                _log_feature_snapshot("CE", ce_snap, ce_signed_vol)
                _log_feature_snapshot("PE", pe_snap, pe_signed_vol)
                
                # Bootstrap history if empty - use current prices to create initial series
                if ce_hist.empty and np.isfinite(ce_current):
                    ce_hist = pd.Series(
                        [ce_current] * 5,  # Create 5 data points for minimal forecast
                        index=pd.date_range(end=datetime.now(IST), periods=5, freq='1min')
                    )
                
                if pe_hist.empty and np.isfinite(pe_current):
                    pe_hist = pd.Series(
                        [pe_current] * 5,  # Create 5 data points for minimal forecast
                        index=pd.date_range(end=datetime.now(IST), periods=5, freq='1min')
                    )

                # Avoid artificial padding to keep true price dynamics.

                auth_instance = st.session_state.get("auth")
                if auth_instance is None or not getattr(auth_instance, "access_token", None):
                    st.warning("Upstox auth missing; IV/OI/volume/spread/imbalance features cannot be fetched.")

                # Get underlying data
                underlying_series = None
                if isinstance(locals().get("df_price"), pd.DataFrame) and not df_price.empty and "Close" in df_price.columns:
                    underlying_series = pd.to_numeric(df_price["Close"], errors="coerce").dropna()

                # Run forecasts with recent-holdout model selector
                ce_col, pe_col = st.columns(2)

                with ce_col:
                    st.markdown("**Call (CE) Forecast**")
                    st.metric("Current LTP", f"₹{ce_current:.2f}")
                    st.caption(f"Training window: {ce_days}d")
                    with st.expander("Feature snapshot", expanded=False):
                        st.dataframe(
                            pd.DataFrame({
                                "Feature": ["IV", "OI", "Volume", "Spread", "Imbalance", "MicroPrice", "SignedVol"],
                                "Value": [
                                    float(pd.to_numeric(ce_snap.get("IV", np.nan), errors="coerce")),
                                    float(pd.to_numeric(ce_snap.get("OI", np.nan), errors="coerce")),
                                    float(pd.to_numeric(ce_snap.get("Volume", np.nan), errors="coerce")),
                                    float(pd.to_numeric(ce_snap.get("Spread", np.nan), errors="coerce")),
                                    float(pd.to_numeric(ce_snap.get("DepthImbalance", np.nan), errors="coerce")),
                                    float(pd.to_numeric(ce_snap.get("MicroPrice", np.nan), errors="coerce")),
                                    float(pd.to_numeric(pd.Series(ce_signed_vol).dropna().iloc[-1], errors="coerce")) if ce_signed_vol is not None and not pd.Series(ce_signed_vol).dropna().empty else np.nan,
                                ],
                            }),
                            width="stretch",
                            height=220,
                        )
                    with st.expander("Forecast history", expanded=False):
                        if st.button("Clear forecast history (CE)"):
                            history_payload = st.session_state.get("ltp_forecast_history", {})
                            history_payload.pop(_forecast_history_key(atm_strike, "CE", "AUTO", selected_expiry), None)
                            history_payload.pop(_forecast_history_key(atm_strike, "CE", "LSTM", selected_expiry), None)
                            history_payload.pop(_forecast_history_key(atm_strike, "CE", "NHITS", selected_expiry), None)
                            history_payload.pop(_legacy_forecast_key(atm_strike, "CE", "AUTO"), None)
                            history_payload.pop(_legacy_forecast_key(atm_strike, "CE", "LSTM"), None)
                            history_payload.pop(_legacy_forecast_key(atm_strike, "CE", "NHITS"), None)
                            st.session_state["ltp_forecast_history"] = history_payload
                            _save_forecast_history(history_payload)
                            st.success("CE forecast history cleared.")

                    ce_static_features = {
                        **_snapshot_to_static_features(ce_snap, prefix="chain"),
                        "bid": ce_bid,
                        "ask": ce_ask,
                        "spread": ce_spread,
                        "iv": float(pd.to_numeric(ce_snap.get("IV", np.nan), errors="coerce")),
                        "oi": float(pd.to_numeric(ce_snap.get("OI", np.nan), errors="coerce")),
                        "volume": float(pd.to_numeric(ce_snap.get("Volume", np.nan), errors="coerce")),
                        "microprice": float(pd.to_numeric(ce_snap.get("MicroPrice", np.nan), errors="coerce")),
                        "imbalance": float(pd.to_numeric(ce_snap.get("DepthImbalance", np.nan), errors="coerce")),
                        "bid_qty": float(pd.to_numeric(ce_snap.get("BidQty", np.nan), errors="coerce")),
                        "ask_qty": float(pd.to_numeric(ce_snap.get("AskQty", np.nan), errors="coerce")),
                        "ce_bid_vol": ce_bid_vol,
                        "ce_ask_vol": ce_ask_vol,
                        "ce_net_qty": float(pd.to_numeric(strike_row.get("CE Net Qty", np.nan), errors="coerce")),
                        "ce_flow_value": float(pd.to_numeric(strike_row.get("CE Flow Value", np.nan), errors="coerce")),
                        "ce_flow_score": float(pd.to_numeric(strike_row.get("CE Flow Score", np.nan), errors="coerce")),
                        "moneyness": float(spot_price / float(atm_strike)) if float(atm_strike) > 0 else np.nan,
                    }
                    ce_dte = max(int(dte), 0) if np.isfinite(dte) else None
                    ce_selector = None
                    ce_auto_result = None
                    ce_auto_model = None
                    if forecasting_enabled:
                        ce_selector = _select_option_forecast_model(
                            ce_completed_hist,
                            strike=float(atm_strike),
                            option_type="CE",
                            selected_expiry=selected_expiry,
                            option_frame=ce_completed_frame,
                            underlying_prices=underlying_series,
                            iv_series=ce_iv_series,
                            oi_series=ce_oi_series,
                            volume_series=ce_vol_series,
                            spread_series=ce_spread_series,
                            imbalance_series=ce_imb_series,
                            microprice_series=ce_micro_series,
                            signed_volume_series=ce_signed_vol,
                            static_features=ce_static_features,
                            days_to_expiry=ce_dte,
                            forecast_steps=FORECAST_STEPS,
                            auth_instance=st.session_state.get("auth"),
                        )
                        ranked_models = ce_selector.get("ranked_models", []) or ["PERSISTENCE"]
                        try_models = [ce_selector.get("selected_model")] + [m for m in ranked_models if m != ce_selector.get("selected_model")]
                        for model_key in try_models:
                            attempt = _run_option_forecast_candidate(
                                model_key,
                                ce_hist,
                                strike=float(atm_strike),
                                option_type="CE",
                                option_frame=ce_frame,
                                underlying_prices=underlying_series,
                                iv_series=ce_iv_series,
                                oi_series=ce_oi_series,
                                volume_series=ce_vol_series,
                                spread_series=ce_spread_series,
                                imbalance_series=ce_imb_series,
                                microprice_series=ce_micro_series,
                                signed_volume_series=ce_signed_vol,
                                static_features=ce_static_features,
                                days_to_expiry=ce_dte,
                                forecast_steps=FORECAST_STEPS,
                                auth_instance=st.session_state.get("auth"),
                                expiry=selected_expiry,
                            )
                            if _has_forecast(attempt):
                                ce_auto_result = attempt
                                ce_auto_model = str(model_key).upper()
                                break
                        if ce_auto_result is None:
                            attempt = _run_option_forecast_candidate(
                                "PERSISTENCE",
                                ce_hist,
                                strike=float(atm_strike),
                                option_type="CE",
                                option_frame=ce_frame,
                                underlying_prices=underlying_series,
                                iv_series=ce_iv_series,
                                oi_series=ce_oi_series,
                                volume_series=ce_vol_series,
                                spread_series=ce_spread_series,
                                imbalance_series=ce_imb_series,
                                microprice_series=ce_micro_series,
                                signed_volume_series=ce_signed_vol,
                                static_features=ce_static_features,
                                days_to_expiry=ce_dte,
                                forecast_steps=FORECAST_STEPS,
                                auth_instance=st.session_state.get("auth"),
                                expiry=selected_expiry,
                            )
                            if _has_forecast(attempt):
                                ce_auto_result = attempt
                                ce_auto_model = "PERSISTENCE"
                        if ce_auto_result is not None:
                            ce_auto_result["selected_model_key"] = ce_auto_model
                            ce_auto_result["selector_reason"] = ce_selector.get("reason", "")
                            ce_auto_result["selector_diagnostics"] = ce_selector.get("diagnostics", [])
                            ce_auto_result["selector_holdout_start"] = ce_selector.get("holdout_start", "")
                            ce_auto_result["selector_holdout_end"] = ce_selector.get("holdout_end", "")

                    ce_auto_key = _forecast_history_key(atm_strike, "CE", "AUTO", selected_expiry)
                    legacy_ce_auto_key = _legacy_forecast_key(atm_strike, "CE", "AUTO")
                    model_colors = {"PERSISTENCE": "#94a3b8", "CATBOOST": "#0ea5e9", "LSTM": "#f59e0b", "NHITS": "#6366f1", "IMPROVED": "#22c55e", "DLINEAR": "#10b981"}

                    if ce_selector and ce_selector.get("diagnostics"):
                        with st.expander("Model selector diagnostics", expanded=False):
                            diag_df = pd.DataFrame(ce_selector.get("diagnostics", []))
                            st.caption(
                                f"Selector basis: latest completed {FORECAST_HORIZON_LABEL} holdout "
                                f"({ce_selector.get('holdout_start', '')} → {ce_selector.get('holdout_end', '')})."
                            )
                            st.dataframe(diag_df, width="stretch", height=220)

                    if _has_forecast(ce_auto_result):
                        ce_auto_result = _refine_forecast_bands(ce_hist, ce_auto_result)
                        _load_forecast_history()
                        forecast_store = st.session_state.setdefault("ltp_forecast_history", {})
                        auto_anchor = (
                            _to_ist_timestamp(ce_plot_hist.index[-1])
                            if isinstance(ce_plot_hist.index, pd.DatetimeIndex) and len(ce_plot_hist.index)
                            else datetime.now(IST)
                        )
                        ce_auto_hist = forecast_store.setdefault(ce_auto_key, [])
                        new_entry = {
                            "anchor": auto_anchor,
                            "values": ce_auto_result.get("forecast", []),
                            "model": ce_auto_model,
                        }
                        if (
                            ce_auto_hist
                            and _to_ist_timestamp(ce_auto_hist[-1].get("anchor")) == _to_ist_timestamp(auto_anchor)
                            and str(ce_auto_hist[-1].get("model", "")).upper() == str(ce_auto_model).upper()
                        ):
                            ce_auto_hist[-1] = new_entry
                        else:
                            ce_auto_hist.append(new_entry)
                        forecast_store[ce_auto_key] = ce_auto_hist
                        _save_forecast_history(forecast_store)

                        ce_auto_forecast = float(ce_auto_result["forecast"][-1])
                        ce_auto_conf = float(ce_auto_result.get("confidence", 0))
                        ce_selected_diag = next(
                            (
                                row
                                for row in (ce_selector or {}).get("diagnostics", [])
                                if str(row.get("Model", "")).upper() == str(ce_auto_model).upper()
                            ),
                            {},
                        )
                        st.metric("Forecast LTP", f"₹{ce_auto_forecast:.2f}", delta=f"₹{ce_auto_forecast - ce_current:+.2f}")
                        st.metric("Confidence", f"{ce_auto_conf:.0f}%")
                        st.metric("Selected Model", ce_auto_model)
                        if np.isfinite(float(pd.to_numeric(ce_selected_diag.get("MAE", np.nan), errors="coerce"))):
                            st.metric("Holdout MAE", f"{float(pd.to_numeric(ce_selected_diag.get('MAE', np.nan), errors='coerce')):.2f}")
                        st.caption(ce_auto_result.get("selector_reason", ""))

                        ce_auto_past = (forecast_store.get(ce_auto_key, []) or []) + (forecast_store.get(legacy_ce_auto_key, []) or [])
                        ce_auto_fig = _build_ltp_forecast_plot(
                            ce_plot_hist.tail(240),
                            ce_auto_result.get("forecast", []),
                            title=f"CE Auto Forecast ({FORECAST_HORIZON_LABEL})",
                            color=model_colors.get(ce_auto_model, "#22d3ee"),
                            past_forecasts=ce_auto_past,
                            forecast_lower=ce_auto_result.get("lower_band"),
                            forecast_upper=ce_auto_result.get("upper_band"),
                        )
                        st.plotly_chart(ce_auto_fig, width="stretch")
                    elif not forecasting_enabled:
                        _load_forecast_history()
                        forecast_store = st.session_state.setdefault("ltp_forecast_history", {})
                        ce_auto_past = (forecast_store.get(ce_auto_key, []) or []) + (forecast_store.get(legacy_ce_auto_key, []) or [])
                        if not ce_auto_past:
                            ce_auto_past = (
                                (forecast_store.get(_forecast_history_key(atm_strike, "CE", "LSTM", selected_expiry), []) or [])
                                + (forecast_store.get(_forecast_history_key(atm_strike, "CE", "NHITS", selected_expiry), []) or [])
                                + (forecast_store.get(_legacy_forecast_key(atm_strike, "CE", "LSTM"), []) or [])
                                + (forecast_store.get(_legacy_forecast_key(atm_strike, "CE", "NHITS"), []) or [])
                            )
                        if ce_auto_past:
                            latest_model = str((ce_auto_past[-1] or {}).get("model", "AUTO")).upper()
                            ce_auto_fig = _build_ltp_forecast_plot(
                                ce_plot_hist.tail(240),
                                [],
                                title=f"CE Auto Forecast ({FORECAST_HORIZON_LABEL})",
                                color=model_colors.get(latest_model, "#22d3ee"),
                                past_forecasts=ce_auto_past,
                            )
                            st.plotly_chart(ce_auto_fig, width="stretch")
                        else:
                            st.info("Market closed; no auto-selected CE forecasts stored yet.")
                    else:
                        err_msg = ""
                        if ce_auto_result and ce_auto_result.get("error"):
                            err_msg = str(ce_auto_result.get("error"))
                        elif ce_selector and ce_selector.get("reason"):
                            err_msg = str(ce_selector.get("reason"))
                        st.warning(f"Auto forecast unavailable: {err_msg or 'no valid candidate model'}")
                
                with pe_col:
                    st.markdown("**Put (PE) Forecast**")
                    st.metric("Current LTP", f"₹{pe_current:.2f}")
                    st.caption(f"Training window: {pe_days}d")
                    with st.expander("Feature snapshot", expanded=False):
                        st.dataframe(
                            pd.DataFrame({
                                "Feature": ["IV", "OI", "Volume", "Spread", "Imbalance", "MicroPrice", "SignedVol"],
                                "Value": [
                                    float(pd.to_numeric(pe_snap.get("IV", np.nan), errors="coerce")),
                                    float(pd.to_numeric(pe_snap.get("OI", np.nan), errors="coerce")),
                                    float(pd.to_numeric(pe_snap.get("Volume", np.nan), errors="coerce")),
                                    float(pd.to_numeric(pe_snap.get("Spread", np.nan), errors="coerce")),
                                    float(pd.to_numeric(pe_snap.get("DepthImbalance", np.nan), errors="coerce")),
                                    float(pd.to_numeric(pe_snap.get("MicroPrice", np.nan), errors="coerce")),
                                    float(pd.to_numeric(pd.Series(pe_signed_vol).dropna().iloc[-1], errors="coerce")) if pe_signed_vol is not None and not pd.Series(pe_signed_vol).dropna().empty else np.nan,
                                ],
                            }),
                            width="stretch",
                            height=220,
                        )
                    with st.expander("Forecast history", expanded=False):
                        if st.button("Clear forecast history (PE)"):
                            history_payload = st.session_state.get("ltp_forecast_history", {})
                            history_payload.pop(_forecast_history_key(atm_strike, "PE", "AUTO", selected_expiry), None)
                            history_payload.pop(_forecast_history_key(atm_strike, "PE", "LSTM", selected_expiry), None)
                            history_payload.pop(_forecast_history_key(atm_strike, "PE", "NHITS", selected_expiry), None)
                            history_payload.pop(_legacy_forecast_key(atm_strike, "PE", "AUTO"), None)
                            history_payload.pop(_legacy_forecast_key(atm_strike, "PE", "LSTM"), None)
                            history_payload.pop(_legacy_forecast_key(atm_strike, "PE", "NHITS"), None)
                            st.session_state["ltp_forecast_history"] = history_payload
                            _save_forecast_history(history_payload)
                            st.success("PE forecast history cleared.")

                    pe_static_features = {
                        **_snapshot_to_static_features(pe_snap, prefix="chain"),
                        "bid": pe_bid,
                        "ask": pe_ask,
                        "spread": pe_spread,
                        "iv": float(pd.to_numeric(pe_snap.get("IV", np.nan), errors="coerce")),
                        "oi": float(pd.to_numeric(pe_snap.get("OI", np.nan), errors="coerce")),
                        "volume": float(pd.to_numeric(pe_snap.get("Volume", np.nan), errors="coerce")),
                        "microprice": float(pd.to_numeric(pe_snap.get("MicroPrice", np.nan), errors="coerce")),
                        "imbalance": float(pd.to_numeric(pe_snap.get("DepthImbalance", np.nan), errors="coerce")),
                        "bid_qty": float(pd.to_numeric(pe_snap.get("BidQty", np.nan), errors="coerce")),
                        "ask_qty": float(pd.to_numeric(pe_snap.get("AskQty", np.nan), errors="coerce")),
                        "pe_bid_vol": pe_bid_vol,
                        "pe_ask_vol": pe_ask_vol,
                        "pe_net_qty": float(pd.to_numeric(strike_row.get("PE Net Qty", np.nan), errors="coerce")),
                        "pe_flow_value": float(pd.to_numeric(strike_row.get("PE Flow Value", np.nan), errors="coerce")),
                        "pe_flow_score": float(pd.to_numeric(strike_row.get("PE Flow Score", np.nan), errors="coerce")),
                        "moneyness": float(spot_price / float(atm_strike)) if float(atm_strike) > 0 else np.nan,
                    }
                    pe_dte = max(int(dte), 0) if np.isfinite(dte) else None
                    pe_selector = None
                    pe_auto_result = None
                    pe_auto_model = None
                    if forecasting_enabled:
                        pe_selector = _select_option_forecast_model(
                            pe_completed_hist,
                            strike=float(atm_strike),
                            option_type="PE",
                            selected_expiry=selected_expiry,
                            option_frame=pe_completed_frame,
                            underlying_prices=underlying_series,
                            iv_series=pe_iv_series,
                            oi_series=pe_oi_series,
                            volume_series=pe_vol_series,
                            spread_series=pe_spread_series,
                            imbalance_series=pe_imb_series,
                            microprice_series=pe_micro_series,
                            signed_volume_series=pe_signed_vol,
                            static_features=pe_static_features,
                            days_to_expiry=pe_dte,
                            forecast_steps=FORECAST_STEPS,
                            auth_instance=st.session_state.get("auth"),
                        )
                        ranked_models = pe_selector.get("ranked_models", []) or ["PERSISTENCE"]
                        try_models = [pe_selector.get("selected_model")] + [m for m in ranked_models if m != pe_selector.get("selected_model")]
                        for model_key in try_models:
                            attempt = _run_option_forecast_candidate(
                                model_key,
                                pe_hist,
                                strike=float(atm_strike),
                                option_type="PE",
                                option_frame=pe_frame,
                                underlying_prices=underlying_series,
                                iv_series=pe_iv_series,
                                oi_series=pe_oi_series,
                                volume_series=pe_vol_series,
                                spread_series=pe_spread_series,
                                imbalance_series=pe_imb_series,
                                microprice_series=pe_micro_series,
                                signed_volume_series=pe_signed_vol,
                                static_features=pe_static_features,
                                days_to_expiry=pe_dte,
                                forecast_steps=FORECAST_STEPS,
                                auth_instance=st.session_state.get("auth"),
                                expiry=selected_expiry,
                            )
                            if _has_forecast(attempt):
                                pe_auto_result = attempt
                                pe_auto_model = str(model_key).upper()
                                break
                        if pe_auto_result is None:
                            attempt = _run_option_forecast_candidate(
                                "PERSISTENCE",
                                pe_hist,
                                strike=float(atm_strike),
                                option_type="PE",
                                option_frame=pe_frame,
                                underlying_prices=underlying_series,
                                iv_series=pe_iv_series,
                                oi_series=pe_oi_series,
                                volume_series=pe_vol_series,
                                spread_series=pe_spread_series,
                                imbalance_series=pe_imb_series,
                                microprice_series=pe_micro_series,
                                signed_volume_series=pe_signed_vol,
                                static_features=pe_static_features,
                                days_to_expiry=pe_dte,
                                forecast_steps=FORECAST_STEPS,
                                auth_instance=st.session_state.get("auth"),
                                expiry=selected_expiry,
                            )
                            if _has_forecast(attempt):
                                pe_auto_result = attempt
                                pe_auto_model = "PERSISTENCE"
                        if pe_auto_result is not None:
                            pe_auto_result["selected_model_key"] = pe_auto_model
                            pe_auto_result["selector_reason"] = pe_selector.get("reason", "")
                            pe_auto_result["selector_diagnostics"] = pe_selector.get("diagnostics", [])
                            pe_auto_result["selector_holdout_start"] = pe_selector.get("holdout_start", "")
                            pe_auto_result["selector_holdout_end"] = pe_selector.get("holdout_end", "")

                    pe_auto_key = _forecast_history_key(atm_strike, "PE", "AUTO", selected_expiry)
                    legacy_pe_auto_key = _legacy_forecast_key(atm_strike, "PE", "AUTO")
                    model_colors = {"PERSISTENCE": "#94a3b8", "CATBOOST": "#0ea5e9", "LSTM": "#f59e0b", "NHITS": "#6366f1", "IMPROVED": "#22c55e", "DLINEAR": "#10b981"}

                    if pe_selector and pe_selector.get("diagnostics"):
                        with st.expander("Model selector diagnostics", expanded=False):
                            diag_df = pd.DataFrame(pe_selector.get("diagnostics", []))
                            st.caption(
                                f"Selector basis: latest completed {FORECAST_HORIZON_LABEL} holdout "
                                f"({pe_selector.get('holdout_start', '')} → {pe_selector.get('holdout_end', '')})."
                            )
                            st.dataframe(diag_df, width="stretch", height=220)

                    if _has_forecast(pe_auto_result):
                        pe_auto_result = _refine_forecast_bands(pe_hist, pe_auto_result)
                        _load_forecast_history()
                        forecast_store = st.session_state.setdefault("ltp_forecast_history", {})
                        auto_anchor = (
                            _to_ist_timestamp(pe_plot_hist.index[-1])
                            if isinstance(pe_plot_hist.index, pd.DatetimeIndex) and len(pe_plot_hist.index)
                            else datetime.now(IST)
                        )
                        pe_auto_hist = forecast_store.setdefault(pe_auto_key, [])
                        new_entry = {
                            "anchor": auto_anchor,
                            "values": pe_auto_result.get("forecast", []),
                            "model": pe_auto_model,
                        }
                        if (
                            pe_auto_hist
                            and _to_ist_timestamp(pe_auto_hist[-1].get("anchor")) == _to_ist_timestamp(auto_anchor)
                            and str(pe_auto_hist[-1].get("model", "")).upper() == str(pe_auto_model).upper()
                        ):
                            pe_auto_hist[-1] = new_entry
                        else:
                            pe_auto_hist.append(new_entry)
                        forecast_store[pe_auto_key] = pe_auto_hist
                        _save_forecast_history(forecast_store)

                        pe_auto_forecast = float(pe_auto_result["forecast"][-1])
                        pe_auto_conf = float(pe_auto_result.get("confidence", 0))
                        pe_selected_diag = next(
                            (
                                row
                                for row in (pe_selector or {}).get("diagnostics", [])
                                if str(row.get("Model", "")).upper() == str(pe_auto_model).upper()
                            ),
                            {},
                        )
                        st.metric("Forecast LTP", f"₹{pe_auto_forecast:.2f}", delta=f"₹{pe_auto_forecast - pe_current:+.2f}")
                        st.metric("Confidence", f"{pe_auto_conf:.0f}%")
                        st.metric("Selected Model", pe_auto_model)
                        if np.isfinite(float(pd.to_numeric(pe_selected_diag.get("MAE", np.nan), errors="coerce"))):
                            st.metric("Holdout MAE", f"{float(pd.to_numeric(pe_selected_diag.get('MAE', np.nan), errors='coerce')):.2f}")
                        st.caption(pe_auto_result.get("selector_reason", ""))

                        pe_auto_past = (forecast_store.get(pe_auto_key, []) or []) + (forecast_store.get(legacy_pe_auto_key, []) or [])
                        pe_auto_fig = _build_ltp_forecast_plot(
                            pe_plot_hist.tail(240),
                            pe_auto_result.get("forecast", []),
                            title=f"PE Auto Forecast ({FORECAST_HORIZON_LABEL})",
                            color=model_colors.get(pe_auto_model, "#22d3ee"),
                            past_forecasts=pe_auto_past,
                            forecast_lower=pe_auto_result.get("lower_band"),
                            forecast_upper=pe_auto_result.get("upper_band"),
                        )
                        st.plotly_chart(pe_auto_fig, width="stretch")
                    elif not forecasting_enabled:
                        _load_forecast_history()
                        forecast_store = st.session_state.setdefault("ltp_forecast_history", {})
                        pe_auto_past = (forecast_store.get(pe_auto_key, []) or []) + (forecast_store.get(legacy_pe_auto_key, []) or [])
                        if not pe_auto_past:
                            pe_auto_past = (
                                (forecast_store.get(_forecast_history_key(atm_strike, "PE", "LSTM", selected_expiry), []) or [])
                                + (forecast_store.get(_forecast_history_key(atm_strike, "PE", "NHITS", selected_expiry), []) or [])
                                + (forecast_store.get(_legacy_forecast_key(atm_strike, "PE", "LSTM"), []) or [])
                                + (forecast_store.get(_legacy_forecast_key(atm_strike, "PE", "NHITS"), []) or [])
                            )
                        if pe_auto_past:
                            latest_model = str((pe_auto_past[-1] or {}).get("model", "AUTO")).upper()
                            pe_auto_fig = _build_ltp_forecast_plot(
                                pe_plot_hist.tail(240),
                                [],
                                title=f"PE Auto Forecast ({FORECAST_HORIZON_LABEL})",
                                color=model_colors.get(latest_model, "#22d3ee"),
                                past_forecasts=pe_auto_past,
                            )
                            st.plotly_chart(pe_auto_fig, width="stretch")
                        else:
                            st.info("Market closed; no auto-selected PE forecasts stored yet.")
                    else:
                        err_msg = ""
                        if pe_auto_result and pe_auto_result.get("error"):
                            err_msg = str(pe_auto_result.get("error"))
                        elif pe_selector and pe_selector.get("reason"):
                            err_msg = str(pe_selector.get("reason"))
                        st.warning(f"Auto forecast unavailable: {err_msg or 'no valid candidate model'}")
# ============================================================================
# Auto-refresh with placeholder updates
# ============================================================================
if 'auto_refresh' in st.session_state and st.session_state.auto_refresh:
    import time
    sleep_for, _ = _seconds_until_next_ist_tick(refresh_interval)
    time.sleep(sleep_for)
    st.rerun()
