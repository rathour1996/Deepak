import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from src.nse_data import fetch_nse_index_data, fetch_nse_option_chain, fetch_nse_expiry_dates
from src.strategy import calculate_supertrend, generate_signals, generate_theoretical_option_chain
from src.lstm_model import LSTMModel
from src.catboost_model import CatBoostForecaster
from src.upstox_auth import UpstoxAuth
from src.upstox_data import (
    fetch_upstox_option_chain,
    fetch_upstox_expiry_dates,
    fetch_upstox_option_ltp_timeseries,
)
from src.data import save_forecast_history, load_forecast_history
import time

st.set_page_config(page_title="BankNifty Forecast Bot", layout="wide", initial_sidebar_state="expanded")
st.markdown(
    """
    <style>
    .stApp {
        background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%);
        color: #0f172a;
    }
    [data-testid="stMetricValue"] {font-size: 2rem;}
    [data-testid="stMetricLabel"] {font-size: 0.95rem;}
    div.stButton > button {
        background: linear-gradient(90deg, #0f766e 0%, #0e7490 100%);
        color: #ecfeff;
        border: 1px solid #155e75;
    }
    .insight-card {
        background: rgba(255, 255, 255, 0.95);
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 10px 12px;
        margin-bottom: 8px;
    }
    .insight-title {
        color: #475569;
        font-size: 0.78rem;
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }
    .insight-value {
        color: #0f172a;
        font-size: 1.25rem;
        font-weight: 700;
        line-height: 1.2;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def normalize_option_chain(chain_df):
    if chain_df is None or chain_df.empty:
        return pd.DataFrame()

    normalized = chain_df.copy()
    required = ["Strike", "Type", "Price", "Change", "OI", "OI_Change", "Volume", "IV", "Delta"]
    for col in required:
        if col not in normalized.columns:
            normalized[col] = 0

    for col in ["Strike", "Price", "Change", "OI", "OI_Change", "Volume", "IV", "Delta"]:
        normalized[col] = pd.to_numeric(normalized[col], errors="coerce")

    normalized = normalized.dropna(subset=["Strike", "Type"])
    normalized["Type"] = normalized["Type"].astype(str).str.upper()
    normalized = normalized[normalized["Type"].isin(["CE", "PE"])]
    return normalized


def build_wide_chain(chain_df):
    if chain_df is None or chain_df.empty:
        return pd.DataFrame()

    agg_dict = {
        "OI": "sum",
        "OI_Change": "sum",
        "Volume": "sum",
        "IV": "mean",
        "Delta": "mean",
        "Price": "mean",
        "Change": "mean",
    }
    ce = chain_df[chain_df["Type"] == "CE"].groupby("Strike", as_index=False).agg(agg_dict)
    pe = chain_df[chain_df["Type"] == "PE"].groupby("Strike", as_index=False).agg(agg_dict)

    ce = ce.rename(columns={
        "OI": "CE OI",
        "OI_Change": "CE OI Chg",
        "Volume": "CE Vol",
        "IV": "CE IV",
        "Delta": "CE Delta",
        "Price": "CE LTP",
        "Change": "CE Change",
    })
    pe = pe.rename(columns={
        "OI": "PE OI",
        "OI_Change": "PE OI Chg",
        "Volume": "PE Vol",
        "IV": "PE IV",
        "Delta": "PE Delta",
        "Price": "PE LTP",
        "Change": "PE Change",
    })

    wide = pd.merge(ce, pe, on="Strike", how="outer").sort_values("Strike").reset_index(drop=True)
    for col in wide.columns:
        if col != "Strike":
            wide[col] = pd.to_numeric(wide[col], errors="coerce")
    return wide


def compute_max_pain(wide_chain):
    if wide_chain is None or wide_chain.empty or "Strike" not in wide_chain.columns:
        return np.nan

    strikes = wide_chain["Strike"].dropna().to_numpy(dtype=float)
    if len(strikes) == 0:
        return np.nan

    ce_oi = wide_chain.get("CE OI", pd.Series(0, index=wide_chain.index)).fillna(0).to_numpy(dtype=float)
    pe_oi = wide_chain.get("PE OI", pd.Series(0, index=wide_chain.index)).fillna(0).to_numpy(dtype=float)

    pain_values = []
    for target in strikes:
        ce_pain = np.sum(np.maximum(strikes - target, 0) * ce_oi)
        pe_pain = np.sum(np.maximum(target - strikes, 0) * pe_oi)
        pain_values.append(ce_pain + pe_pain)

    if not pain_values:
        return np.nan

    return float(strikes[int(np.argmin(pain_values))])


def classify_buildup(price_change, oi_change):
    if pd.isna(price_change) or pd.isna(oi_change):
        return "NA"
    if price_change > 0 and oi_change > 0:
        return "Long Build-up"
    if price_change < 0 and oi_change > 0:
        return "Short Build-up"
    if price_change > 0 and oi_change < 0:
        return "Short Covering"
    if price_change < 0 and oi_change < 0:
        return "Long Unwinding"
    return "Neutral"


def trader_bias_label(pcr, bull=1.2, bear=0.8):
    if pd.isna(pcr):
        return "NA"
    if pcr >= bull:
        return "Bullish (Put Writing Bias)"
    if pcr <= bear:
        return "Bearish (Call Writing Bias)"
    return "Balanced / Neutral"

def fetch_training_data_with_fallback(preferred_period="180d", preferred_interval="15m"):
    """
    yfinance often returns empty for long lookbacks with intraday intervals.
    Try a safe sequence of period/interval combinations.
    """
    attempts = []
    seen = set()

    def add_attempt(period, interval):
        key = (period, interval)
        if key not in seen:
            attempts.append(key)
            seen.add(key)

    add_attempt(preferred_period, preferred_interval)

    intraday_intervals = {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"}
    if preferred_interval in intraday_intervals:
        add_attempt("60d", preferred_interval)

    add_attempt(preferred_period, "1h")
    add_attempt(preferred_period, "1d")
    add_attempt("180d", "1h")
    add_attempt("1y", "1d")
    add_attempt("2y", "1d")

    for period, interval in attempts:
        train_df = fetch_nse_index_data(period=period, interval=interval)
        if train_df is not None and not train_df.empty:
            return train_df, period, interval

    return pd.DataFrame(), None, None


if "lstm_model" not in st.session_state:
    st.session_state.lstm_model = LSTMModel(lookback=60, forecast_steps=5)
if "catboost_model" not in st.session_state:
    st.session_state.catboost_model = CatBoostForecaster(lookback=60, forecast_steps=5)
if "trained" not in st.session_state:
    st.session_state.trained = bool(
        st.session_state.catboost_model.is_ready()
        or st.session_state.lstm_model.model is not None
    )
if "trained_model_name" not in st.session_state:
    st.session_state.trained_model_name = "CatBoost" if st.session_state.catboost_model.is_ready() else "LSTM"
if "history_forecasts" not in st.session_state:
    st.session_state.history_forecasts = load_forecast_history()
if "strike_ltp_history" not in st.session_state:
    st.session_state.strike_ltp_history = {}
if "strike_ltp_scale_prefs" not in st.session_state:
    st.session_state.strike_ltp_scale_prefs = {}
if "upstox_auth" not in st.session_state:
    st.session_state.upstox_auth = None
if "upstox_logged_in" not in st.session_state:
    st.session_state.upstox_logged_in = False
if "last_signal_time" not in st.session_state:
    st.session_state.last_signal_time = None
if "upstox_access_token" not in st.session_state:
    st.session_state.upstox_access_token = None

st.sidebar.title("Configuration")
with st.sidebar.expander("Upstox Login (Optional)", expanded=True):
    api_key = st.text_input("API Key", value="YOUR_UPSTOX_API_KEY", type="password")
    api_secret = st.text_input("API Secret", value="YOUR_UPSTOX_API_SECRET", type="password")
    redirect_uri = st.text_input("Redirect URI", value="http://localhost:8501")

    auth_instance = UpstoxAuth(api_key, api_secret, redirect_uri)
    if st.session_state.upstox_access_token:
        auth_instance.restore_session(st.session_state.upstox_access_token)
        st.session_state.upstox_logged_in = True
        st.sidebar.success("Session Restored")

    if st.button("Generate Login URL"):
        st.session_state.upstox_auth = auth_instance
        url = auth_instance.get_login_url()
        st.markdown(f"[Click Here to Login]({url})", unsafe_allow_html=True)

    query_params = st.query_params
    default_code = query_params.get("code", "")
    auth_code = st.text_input("Auth Code", value=default_code)

    if st.button("Connect Upstox"):
        success, msg = auth_instance.generate_access_token(auth_code)
        if success:
            st.session_state.upstox_access_token = auth_instance.access_token
            st.session_state.upstox_logged_in = True
            st.success("Connected")
            st.query_params.clear()
        else:
            st.error(msg)

st.sidebar.markdown("---")
interval = st.sidebar.selectbox("Interval", ["1m", "5m", "15m", "1h"], index=1)
period = st.sidebar.number_input("Supertrend Period", value=7, min_value=1)
multiplier = st.sidebar.number_input("Supertrend Multiplier", value=3.0, min_value=0.1)

st.sidebar.subheader("Option Trader Settings")
strike_window = st.sidebar.slider("Strike Window (+/- points)", min_value=300, max_value=2500, value=1000, step=100)
top_oi_levels = st.sidebar.slider("Top OI Support/Resistance Levels", min_value=1, max_value=5, value=3, step=1)
pcr_bull_threshold = st.sidebar.slider("PCR Bullish Threshold", min_value=1.0, max_value=1.8, value=1.2, step=0.05)
pcr_bear_threshold = st.sidebar.slider("PCR Bearish Threshold", min_value=0.4, max_value=1.0, value=0.8, step=0.05)
show_iv_smile = st.sidebar.checkbox("Show IV Smile Chart", value=True)
show_oi_change_chart = st.sidebar.checkbox("Show OI Change Profile", value=True)

st.sidebar.subheader("Expiry Calendar")
expiry_source = None
available_expiries = []

if st.session_state.upstox_logged_in and st.session_state.upstox_access_token:
    expiry_auth = UpstoxAuth(api_key, api_secret, redirect_uri)
    expiry_auth.restore_session(st.session_state.upstox_access_token)
    available_expiries = fetch_upstox_expiry_dates(expiry_auth, symbol="BANKNIFTY", mode="monthly")
    if available_expiries:
        expiry_source = "Upstox Monthly"

if not available_expiries:
    available_expiries = fetch_nse_expiry_dates(symbol="BANKNIFTY", mode="monthly")
    if available_expiries:
        expiry_source = "NSE Monthly"

if not available_expiries:
    expiry_source = "Fallback Monthly"
    base_month = pd.Timestamp.now().to_period("M")
    fallback_expiries = []
    for i in range(0, 8):
        month_start = (base_month + i).to_timestamp()
        d = month_start + pd.offsets.MonthEnd(0)
        # Approximate monthly expiry as last Tuesday of month (holiday adjustments are API-driven when available).
        while d.weekday() != 1:
            d = d - pd.Timedelta(days=1)
        fallback_expiries.append(d.strftime("%Y-%m-%d"))
    available_expiries = fallback_expiries

if available_expiries:
    expiry_str = st.sidebar.selectbox("Expiry Date (Monthly)", options=available_expiries, index=0)
    st.sidebar.caption(f"Loaded {len(available_expiries)} expiries from {expiry_source}")
else:
    fallback_date = pd.Timestamp.now().strftime("%Y-%m-%d")
    expiry_str = st.sidebar.text_input("Expiry Date (YYYY-MM-DD)", value=fallback_date)

st.sidebar.subheader("Forecasting")
forecast_engine = st.sidebar.selectbox(
    "Forecast Model",
    ["CatBoost (Recommended)", "LSTM (Legacy)"],
    index=0,
)
train_period = st.sidebar.selectbox(
    "Training History",
    ["60d", "120d", "180d", "1y"],
    index=2,
)
train_interval = st.sidebar.selectbox(
    "Training Candle Interval",
    ["15m", "1h", "1d"],
    index=0,
)
catboost_iterations = st.sidebar.slider("CatBoost Iterations", min_value=200, max_value=1200, value=500, step=50)
lstm_epochs = st.sidebar.slider("LSTM Epochs", min_value=3, max_value=30, value=6, step=1)
if forecast_engine.startswith("CatBoost") and not st.session_state.catboost_model.available:
    st.sidebar.warning("CatBoost not installed. Run: pip install catboost")

if st.sidebar.button("Train Forecast Model"):
    with st.spinner("Fetching Data & Training Forecast Model..."):
        train_df, used_period, used_interval = fetch_training_data_with_fallback(
            preferred_period=train_period,
            preferred_interval=train_interval,
        )
        if train_df.empty:
            st.sidebar.error("No Data for Training. Try `60d + 15m` or `1y + 1d`.")
        else:
            st.sidebar.caption(
                f"Training data used: {used_period} @ {used_interval} | Rows: {len(train_df)}"
            )
            if used_period != train_period or used_interval != train_interval:
                st.sidebar.info("Requested combination had no data; fallback combination was used.")
            if forecast_engine.startswith("CatBoost"):
                success, metrics = st.session_state.catboost_model.train(train_df, iterations=catboost_iterations)
                if success:
                    st.session_state.trained = True
                    st.session_state.trained_model_name = "CatBoost"
                    mae = metrics.get("mae_avg", np.nan)
                    wf_mae = metrics.get("walk_forward_mae_h1", np.nan)
                    dir_acc = metrics.get("directional_accuracy_h1", np.nan)
                    st.sidebar.success(
                        f"CatBoost Trained | MAE: {mae:.2f} | WF-MAE(H1): {wf_mae:.2f} | Dir Acc: {dir_acc:.1f}%"
                        if pd.notna(mae) and pd.notna(wf_mae) and pd.notna(dir_acc)
                        else "CatBoost Trained"
                    )
                else:
                    st.sidebar.error(metrics.get("error", "CatBoost training failed."))
            else:
                model, loss = st.session_state.lstm_model.train(train_df, epochs=lstm_epochs)
                if model:
                    st.session_state.trained = True
                    st.session_state.trained_model_name = "LSTM"
                    st.sidebar.success(f"LSTM Trained | Loss: {loss:.4f}")
                else:
                    st.sidebar.error("LSTM Training Failed.")

if st.session_state.catboost_model.is_ready():
    cb_metrics = st.session_state.catboost_model.metrics
    st.sidebar.caption(
        "CatBoost Last Metrics | "
        f"MAE: {cb_metrics.get('mae_avg', np.nan):.2f}, "
        f"WF-MAE(H1): {cb_metrics.get('walk_forward_mae_h1', np.nan):.2f}, "
        f"DirAcc(H1): {cb_metrics.get('directional_accuracy_h1', np.nan):.1f}%"
    )

auto_refresh = st.sidebar.checkbox("Auto Refresh", value=False)
refresh_seconds = st.sidebar.selectbox("Refresh Every (seconds)", [15, 30, 60, 120], index=2)

st.title("BankNifty Options Trading Terminal")
st.caption("Quant trend + options positioning dashboard with trader-grade option flow, OI profiles, and strike intelligence.")
st.caption(f"Active Forecast Engine: {forecast_engine}")
if forecast_engine.startswith("CatBoost") and not st.session_state.catboost_model.is_ready():
    st.warning("CatBoost model is not trained/loaded yet. Forecast line will appear after training.")

with st.spinner("Fetching Live Data..."):
    df = fetch_nse_index_data(period="5d", interval=interval)
    option_chain_df = None
    selected_expiry = expiry_str
    expiry = None

    if st.session_state.upstox_logged_in and st.session_state.upstox_access_token:
        current_auth = UpstoxAuth(api_key, api_secret, redirect_uri)
        current_auth.restore_session(st.session_state.upstox_access_token)
        option_chain_df, expiry = fetch_upstox_option_chain(
            current_auth,
            symbol="BANKNIFTY",
            expiry=selected_expiry,
            expiry_mode="monthly",
        )
        if option_chain_df is None or option_chain_df.empty:
            option_chain_df = None
            st.warning("Upstox Chain Fetch Failed. Using Fallback.")

    if option_chain_df is None:
        option_chain_df, expiry = fetch_nse_option_chain(expiry=selected_expiry)
        if option_chain_df is None and not df.empty:
            option_chain_df = generate_theoretical_option_chain(df.iloc[-1]["Close"])
            expiry = "Theoretical (Black-Scholes)"

    if expiry and str(expiry) != str(selected_expiry) and "Theoretical" not in str(expiry):
        st.info(f"Selected expiry {selected_expiry} not available. Showing {expiry}.")

if df.empty:
    st.error("Market Data Unavailable. Check Connection.")
else:
    df = calculate_supertrend(df, period, multiplier)
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else df.iloc[-1]

    runtime_engine = forecast_engine
    catboost_ready = st.session_state.catboost_model.is_ready()
    lstm_ready = st.session_state.lstm_model.model is not None

    if forecast_engine.startswith("CatBoost") and not catboost_ready and lstm_ready:
        runtime_engine = "LSTM (Legacy)"
    elif forecast_engine.startswith("LSTM") and not lstm_ready and catboost_ready:
        runtime_engine = "CatBoost (Recommended)"

    model_ready = (
        (runtime_engine.startswith("CatBoost") and catboost_ready)
        or (runtime_engine.startswith("LSTM") and lstm_ready)
    )

    forecast_path = []
    forecast_lower = []
    forecast_upper = []
    forecast_status = "Forecast unavailable"
    if model_ready:
        if runtime_engine.startswith("CatBoost"):
            pred_bundle = st.session_state.catboost_model.predict_sequence(df)
            if pred_bundle:
                forecast_path = pred_bundle.get("median", []) or []
                forecast_lower = pred_bundle.get("lower", []) or []
                forecast_upper = pred_bundle.get("upper", []) or []
        else:
            forecast_path = st.session_state.lstm_model.predict_sequence(df) or []

        if forecast_path:
            forecast_status = f"{runtime_engine} | points: {len(forecast_path)}"
            current_time = latest.name
            if not st.session_state.history_forecasts or st.session_state.history_forecasts[-1][0] != current_time:
                st.session_state.history_forecasts.append((current_time, forecast_path))
                save_forecast_history(st.session_state.history_forecasts)
                if len(st.session_state.history_forecasts) > 100:
                    st.session_state.history_forecasts.pop(0)
        else:
            forecast_status = f"{runtime_engine} ready, but prediction empty"
    else:
        forecast_status = "No trained model loaded (train CatBoost/LSTM from sidebar)"

    next_price = forecast_path[0] if forecast_path else None
    signal = generate_signals(df, next_price, option_chain_df) or {
        "action": "WAIT",
        "sl": 0,
        "target1": 0,
        "target2": 0,
    }

    normalized_chain = normalize_option_chain(option_chain_df)
    wide_chain = build_wide_chain(normalized_chain)
    atm_strike = round(latest["Close"] / 100) * 100
    view_chain = wide_chain[
        (wide_chain["Strike"] >= atm_strike - strike_window)
        & (wide_chain["Strike"] <= atm_strike + strike_window)
    ].copy() if not wide_chain.empty else pd.DataFrame()
    if not wide_chain.empty and view_chain.empty:
        view_chain = wide_chain.copy()

    total_ce_oi = wide_chain["CE OI"].fillna(0).sum() if "CE OI" in wide_chain.columns else np.nan
    total_pe_oi = wide_chain["PE OI"].fillna(0).sum() if "PE OI" in wide_chain.columns else np.nan
    pcr_total = np.nan
    if pd.notna(total_ce_oi) and total_ce_oi > 0 and pd.notna(total_pe_oi):
        pcr_total = total_pe_oi / total_ce_oi

    near_window = max(300, strike_window // 2)
    near_chain = wide_chain[
        (wide_chain["Strike"] >= atm_strike - near_window)
        & (wide_chain["Strike"] <= atm_strike + near_window)
    ] if not wide_chain.empty else pd.DataFrame()
    near_ce_oi = near_chain["CE OI"].fillna(0).sum() if "CE OI" in near_chain.columns else np.nan
    near_pe_oi = near_chain["PE OI"].fillna(0).sum() if "PE OI" in near_chain.columns else np.nan
    pcr_near = np.nan
    if pd.notna(near_ce_oi) and near_ce_oi > 0 and pd.notna(near_pe_oi):
        pcr_near = near_pe_oi / near_ce_oi

    max_pain = compute_max_pain(wide_chain)

    atm_row = pd.Series(dtype=float)
    if not wide_chain.empty and "Strike" in wide_chain.columns:
        idx = (wide_chain["Strike"] - latest["Close"]).abs().idxmin()
        atm_row = wide_chain.loc[idx]

    atm_ce_ltp = float(atm_row.get("CE LTP", np.nan)) if not atm_row.empty else np.nan
    atm_pe_ltp = float(atm_row.get("PE LTP", np.nan)) if not atm_row.empty else np.nan
    atm_straddle = np.nan
    if not np.isnan(atm_ce_ltp) and not np.isnan(atm_pe_ltp):
        atm_straddle = atm_ce_ltp + atm_pe_ltp

    expected_move_up = latest["Close"] + atm_straddle if not np.isnan(atm_straddle) else np.nan
    expected_move_down = latest["Close"] - atm_straddle if not np.isnan(atm_straddle) else np.nan
    atm_iv = np.nanmean([atm_row.get("CE IV", np.nan), atm_row.get("PE IV", np.nan)]) if not atm_row.empty else np.nan
    iv_skew = atm_row.get("CE IV", np.nan) - atm_row.get("PE IV", np.nan) if not atm_row.empty else np.nan
    ce_buildup = classify_buildup(atm_row.get("CE Change", np.nan), atm_row.get("CE OI Chg", np.nan)) if not atm_row.empty else "NA"
    pe_buildup = classify_buildup(atm_row.get("PE Change", np.nan), atm_row.get("PE OI Chg", np.nan)) if not atm_row.empty else "NA"

    resistance_df = wide_chain[["Strike", "CE OI"]].dropna().sort_values("CE OI", ascending=False).head(top_oi_levels) if "CE OI" in wide_chain.columns else pd.DataFrame()
    support_df = wide_chain[["Strike", "PE OI"]].dropna().sort_values("PE OI", ascending=False).head(top_oi_levels) if "PE OI" in wide_chain.columns else pd.DataFrame()
    resistance_text = ", ".join([f"{int(x)}" for x in resistance_df["Strike"].tolist()]) if not resistance_df.empty else "NA"
    support_text = ", ".join([f"{int(x)}" for x in support_df["Strike"].tolist()]) if not support_df.empty else "NA"

    expiry_ts = pd.to_datetime(expiry, errors="coerce")
    dte = (expiry_ts.normalize() - pd.Timestamp.now().normalize()).days if not pd.isna(expiry_ts) else np.nan

    trend_strength = "Neutral"
    if forecast_path:
        slope = forecast_path[-1] - forecast_path[0]
        if slope > 50:
            trend_strength = "Strong Up"
        elif slope < -50:
            trend_strength = "Strong Down"

    col1, col2, col3, col4, col5, col6 = st.columns(6)
    price_change = latest["Close"] - prev["Close"]
    col1.metric("Spot", f"{latest['Close']:.2f}", f"{price_change:.2f}")

    signal_color = "off"
    if "BULLISH" in signal["action"]:
        signal_color = "green"
    elif "BEARISH" in signal["action"]:
        signal_color = "red"
    col2.metric("Signal", signal["action"], delta_color=signal_color)
    col3.metric("PCR (Total)", f"{pcr_total:.2f}" if not np.isnan(pcr_total) else "NA")
    col4.metric("PCR (Near ATM)", f"{pcr_near:.2f}" if not np.isnan(pcr_near) else "NA")
    col5.metric("Max Pain", f"{max_pain:.0f}" if not np.isnan(max_pain) else "NA")
    col6.metric("ATM Straddle", f"{atm_straddle:.2f}" if not np.isnan(atm_straddle) else "NA")

    st.markdown(
        f"""
        <div class="insight-card">
            <div class="insight-title">Trader Bias</div>
            <div class="insight-value">{trader_bias_label(pcr_total, pcr_bull_threshold, pcr_bear_threshold)}</div>
            <div style="color:#94a3b8;font-size:0.85rem;">
                Model: {runtime_engine} | DTE: {int(dte) if not np.isnan(dte) else 'NA'} | Trend: {trend_strength}
                | CE build-up: {ce_buildup} | PE build-up: {pe_buildup}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    tab_price, tab_flow, tab_chain = st.tabs(["Price Action", "Option Flow", "Option Chain"])

    with tab_price:
        c1, c2 = st.columns([3, 1])
        with c1:
            st.subheader("Price Forecast Path")
            st.caption(f"Forecast Status: {forecast_status}")
            fig = go.Figure()
            fig.add_trace(go.Candlestick(x=df.index, open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"], name="History"))
            fig.add_trace(go.Scatter(x=df.index, y=df["Supertrend"], mode="lines", name="Supertrend", line=dict(color="#60a5fa", width=2)))

            buy_signals = df[df["Signal"] == 1]
            sell_signals = df[df["Signal"] == -1]
            if not buy_signals.empty:
                fig.add_trace(go.Scatter(x=buy_signals.index, y=buy_signals["Low"] - 20, mode="markers+text", marker=dict(symbol="triangle-up", size=14, color="#22c55e"), text="BUY", textposition="bottom center", name="BUY"))
            if not sell_signals.empty:
                fig.add_trace(go.Scatter(x=sell_signals.index, y=sell_signals["High"] + 20, mode="markers+text", marker=dict(symbol="triangle-down", size=14, color="#ef4444"), text="SELL", textposition="top center", name="SELL"))

            time_delta = pd.Timedelta(minutes=5)
            if interval == "1m":
                time_delta = pd.Timedelta(minutes=1)
            elif interval == "15m":
                time_delta = pd.Timedelta(minutes=15)
            elif interval == "1h":
                time_delta = pd.Timedelta(hours=1)

            if st.session_state.history_forecasts:
                for timestamp, f_path in st.session_state.history_forecasts:
                    if pd.isna(timestamp):
                        continue
                    f_times = [timestamp + (i + 1) * time_delta for i in range(len(f_path))]
                    fig.add_trace(go.Scatter(x=f_times, y=f_path, mode="lines", line=dict(color="rgba(147, 51, 234, 0.35)", width=1, dash="dot"), showlegend=False, hoverinfo="skip"))

            if forecast_path:
                last_time = df.index[-1]
                future_times = [last_time + (i + 1) * time_delta for i in range(len(forecast_path))]
                fig.add_trace(go.Scatter(x=future_times, y=forecast_path, mode="lines+markers", name="AI Forecast", line=dict(color="#22d3ee", width=3)))
                if len(forecast_lower) == len(forecast_path) and len(forecast_upper) == len(forecast_path):
                    lower_band = forecast_lower
                    upper_band = forecast_upper
                else:
                    upper_band = [p * 1.002 for p in forecast_path]
                    lower_band = [p * 0.998 for p in forecast_path]
                fig.add_trace(go.Scatter(x=future_times + future_times[::-1], y=upper_band + lower_band[::-1], fill="toself", fillcolor="rgba(34, 211, 238, 0.12)", line=dict(color="rgba(255,255,255,0)"), hoverinfo="skip", showlegend=False, name="Confidence"))

            if not np.isnan(expected_move_up) and not np.isnan(expected_move_down):
                fig.add_hline(y=expected_move_up, line_dash="dash", line_color="#f97316", annotation_text="Expected Move High")
                fig.add_hline(y=expected_move_down, line_dash="dash", line_color="#f97316", annotation_text="Expected Move Low")

            fig.update_layout(height=620, xaxis_rangeslider_visible=False, template="plotly_white", title=f"Bank Nifty (Interval: {interval})")
            st.plotly_chart(fig, use_container_width=True)

        with c2:
            st.subheader("Execution Panel")
            st.metric("Trend", trend_strength)
            st.metric("ATM IV", f"{atm_iv:.2f}" if not np.isnan(atm_iv) else "NA")
            st.metric("IV Skew (CE-PE)", f"{iv_skew:.2f}" if not np.isnan(iv_skew) else "NA")
            if not np.isnan(expected_move_up):
                st.metric("Expected Move", f"+/-{atm_straddle:.2f}")
                st.caption(f"Range: {expected_move_down:.2f} - {expected_move_up:.2f}")

            st.markdown("---")
            if latest["Signal"] != 0 and latest.name != st.session_state.last_signal_time:
                st.session_state.last_signal_time = latest.name
                if latest["Signal"] == 1:
                    st.toast("BUY SIGNAL DETECTED", icon="✅")
                elif latest["Signal"] == -1:
                    st.toast("SELL SIGNAL DETECTED", icon="⚠️")

            st.info(f"**Action:** {signal['action']}")
            if "BULLISH" in signal["action"] or "BEARISH" in signal["action"]:
                st.write(f"**Entry:** {latest['Close']:.2f}")
                st.write(f"**Stop Loss:** {signal['sl']:.2f}")
                st.write(f"**Target 1:** {signal['target1']:.2f}")
                st.write(f"**Target 2:** {signal['target2']:.2f}")
            st.caption(f"Support: {support_text}")
            st.caption(f"Resistance: {resistance_text}")

    with tab_flow:
        if wide_chain.empty:
            st.warning("Option flow analytics unavailable.")
        else:
            a1, a2, a3, a4 = st.columns(4)
            a1.metric("Total CE OI", f"{total_ce_oi:,.0f}")
            a2.metric("Total PE OI", f"{total_pe_oi:,.0f}")
            a3.metric("PCR Regime", trader_bias_label(pcr_near, pcr_bull_threshold, pcr_bear_threshold))
            a4.metric("Expiry DTE", f"{int(dte)}" if not np.isnan(dte) else "NA")

            left_chart, right_chart = st.columns([2, 1])
            with left_chart:
                st.subheader("OI Profile by Strike")
                oi_fig = go.Figure()
                if "CE OI" in view_chain.columns:
                    oi_fig.add_trace(go.Bar(
                        y=view_chain["Strike"],
                        x=-view_chain["CE OI"].fillna(0),
                        orientation="h",
                        name="CE OI",
                        marker_color="rgba(34,197,94,0.75)",
                    ))
                if "PE OI" in view_chain.columns:
                    oi_fig.add_trace(go.Bar(
                        y=view_chain["Strike"],
                        x=view_chain["PE OI"].fillna(0),
                        orientation="h",
                        name="PE OI",
                        marker_color="rgba(239,68,68,0.75)",
                    ))
                oi_fig.add_vline(x=0, line_color="#94a3b8", line_width=1)
                oi_fig.update_layout(template="plotly_white", barmode="relative", height=420, xaxis_title="CE OI <- -> PE OI", yaxis_title="Strike")
                st.plotly_chart(oi_fig, use_container_width=True)

            with right_chart:
                st.subheader("PCR Gauge")
                pcr_val = float(pcr_total) if not np.isnan(pcr_total) else 0.0
                gauge = go.Figure(go.Indicator(
                    mode="gauge+number",
                    value=pcr_val,
                    title={"text": "Put/Call Ratio"},
                    gauge={
                        "axis": {"range": [0, 2]},
                        "bar": {"color": "#22d3ee"},
                        "steps": [
                            {"range": [0, pcr_bear_threshold], "color": "rgba(239,68,68,0.35)"},
                            {"range": [pcr_bear_threshold, pcr_bull_threshold], "color": "rgba(250,204,21,0.25)"},
                            {"range": [pcr_bull_threshold, 2], "color": "rgba(34,197,94,0.35)"},
                        ],
                    },
                ))
                gauge.update_layout(template="plotly_white", height=300, margin=dict(l=20, r=20, t=60, b=10))
                st.plotly_chart(gauge, use_container_width=True)
                st.caption(f"Support Strikes: {support_text}")
                st.caption(f"Resistance Strikes: {resistance_text}")

            if show_oi_change_chart and "CE OI Chg" in view_chain.columns and "PE OI Chg" in view_chain.columns:
                st.subheader("Change in OI Profile")
                chg_fig = go.Figure()
                chg_fig.add_trace(go.Bar(
                    y=view_chain["Strike"],
                    x=-view_chain["CE OI Chg"].fillna(0),
                    orientation="h",
                    name="CE OI Change",
                    marker_color="rgba(16,185,129,0.75)",
                ))
                chg_fig.add_trace(go.Bar(
                    y=view_chain["Strike"],
                    x=view_chain["PE OI Chg"].fillna(0),
                    orientation="h",
                    name="PE OI Change",
                    marker_color="rgba(244,63,94,0.75)",
                ))
                chg_fig.add_vline(x=0, line_color="#94a3b8", line_width=1)
                chg_fig.update_layout(template="plotly_white", barmode="relative", height=380, xaxis_title="CE OI Chg <- -> PE OI Chg", yaxis_title="Strike")
                st.plotly_chart(chg_fig, use_container_width=True)

            if show_iv_smile and "CE IV" in view_chain.columns and "PE IV" in view_chain.columns:
                st.subheader("IV Smile")
                iv_fig = go.Figure()
                iv_fig.add_trace(go.Scatter(x=view_chain["Strike"], y=view_chain["CE IV"], mode="lines+markers", name="CE IV", line=dict(color="#22c55e", width=2)))
                iv_fig.add_trace(go.Scatter(x=view_chain["Strike"], y=view_chain["PE IV"], mode="lines+markers", name="PE IV", line=dict(color="#ef4444", width=2)))
                iv_fig.add_vline(x=atm_strike, line_dash="dot", line_color="#fbbf24")
                iv_fig.update_layout(template="plotly_white", height=360, xaxis_title="Strike", yaxis_title="IV")
                st.plotly_chart(iv_fig, use_container_width=True)

    with tab_chain:
        st.subheader(f"Option Chain (Expiry: {expiry})")
        if wide_chain.empty:
            st.warning("Option Chain Data Unavailable.")
        else:
            st.markdown("### Strike LTP Tracker")
            all_strikes = sorted(
                [float(s) for s in wide_chain["Strike"].dropna().tolist()]
            ) if "Strike" in wide_chain.columns else []

            if all_strikes:
                nearest_idx = int(np.argmin([abs(s - latest["Close"]) for s in all_strikes]))
                t1, t2, t3, t4 = st.columns([2, 1, 1, 1])
                picked_strike = t1.selectbox(
                    "Pick Strike",
                    options=all_strikes,
                    index=nearest_idx,
                    format_func=lambda x: f"{x:,.0f}",
                    key="ltp_tracker_strike",
                )
                picked_leg = t2.selectbox(
                    "Contract",
                    options=["Both", "CE", "PE"],
                    index=0,
                    key="ltp_tracker_leg",
                )
                plot_points = t3.slider(
                    "Plot Points",
                    min_value=20,
                    max_value=300,
                    value=120,
                    step=10,
                    key="ltp_tracker_points",
                )
                candle_tf = t4.selectbox(
                    "Candle",
                    options=["1m", "5m", "15m"],
                    index=0,
                    key="ltp_tracker_tf",
                )
                candle_minutes = {"1m": 1, "5m": 5, "15m": 15}.get(candle_tf, 1)

                selected_row = wide_chain[wide_chain["Strike"] == picked_strike]
                if selected_row.empty:
                    selected_row = wide_chain.iloc[[(wide_chain["Strike"] - picked_strike).abs().idxmin()]]
                selected_row = selected_row.iloc[0]

                ce_ltp = float(selected_row.get("CE LTP", np.nan))
                pe_ltp = float(selected_row.get("PE LTP", np.nan))
                now_ts = pd.Timestamp.now()
                history_key_base = f"{expiry}|{int(round(picked_strike))}"

                def append_ltp_history(history_key, value):
                    if pd.isna(value):
                        return
                    hist = st.session_state.strike_ltp_history.get(history_key, [])
                    if not hist or hist[-1][0] != now_ts:
                        hist.append((now_ts, float(value)))
                    if len(hist) > 2000:
                        hist = hist[-2000:]
                    st.session_state.strike_ltp_history[history_key] = hist

                if picked_leg in ("Both", "CE"):
                    append_ltp_history(f"{history_key_base}|CE", ce_ltp)
                if picked_leg in ("Both", "PE"):
                    append_ltp_history(f"{history_key_base}|PE", pe_ltp)

                m1, m2, m3 = st.columns(3)
                m1.metric("Selected Strike", f"{picked_strike:,.0f}")
                m2.metric("CE LTP", f"{ce_ltp:.2f}" if not np.isnan(ce_ltp) else "NA")
                m3.metric("PE LTP", f"{pe_ltp:.2f}" if not np.isnan(pe_ltp) else "NA")

                source_labels = []
                intraday_ce = pd.DataFrame()
                intraday_pe = pd.DataFrame()
                can_fetch_intraday = (
                    st.session_state.upstox_logged_in
                    and st.session_state.upstox_access_token
                    and isinstance(expiry, str)
                    and "Theoretical" not in str(expiry)
                )
                if can_fetch_intraday:
                    tracker_auth = UpstoxAuth(api_key, api_secret, redirect_uri)
                    tracker_auth.restore_session(st.session_state.upstox_access_token)
                    if picked_leg in ("Both", "CE"):
                        intraday_ce = fetch_upstox_option_ltp_timeseries(
                            tracker_auth,
                            symbol="BANKNIFTY",
                            expiry=str(expiry),
                            strike=picked_strike,
                            option_type="CE",
                            interval_minutes=candle_minutes,
                            expiry_mode="monthly",
                        )
                        if not intraday_ce.empty:
                            source_labels.append("CE: Upstox intraday")
                    if picked_leg in ("Both", "PE"):
                        intraday_pe = fetch_upstox_option_ltp_timeseries(
                            tracker_auth,
                            symbol="BANKNIFTY",
                            expiry=str(expiry),
                            strike=picked_strike,
                            option_type="PE",
                            interval_minutes=candle_minutes,
                            expiry_mode="monthly",
                        )
                        if not intraday_pe.empty:
                            source_labels.append("PE: Upstox intraday")

                ltp_fig = go.Figure()
                plot_frames = []
                if picked_leg in ("Both", "CE"):
                    if not intraday_ce.empty:
                        ce_hist_df = intraday_ce[["Time", "Close"]].rename(columns={"Close": "LTP"}).tail(plot_points)
                    else:
                        ce_hist = st.session_state.strike_ltp_history.get(f"{history_key_base}|CE", [])
                        ce_hist_df = pd.DataFrame(ce_hist, columns=["Time", "LTP"]).tail(plot_points) if ce_hist else pd.DataFrame()
                    if not ce_hist_df.empty:
                        ltp_fig.add_trace(
                            go.Scatter(
                                x=ce_hist_df["Time"],
                                y=ce_hist_df["LTP"],
                                mode="lines+markers",
                                name=f"CE {picked_strike:,.0f}",
                                line=dict(color="#16a34a", width=2),
                            )
                        )
                        plot_frames.append(ce_hist_df[["Time", "LTP"]].copy())
                if picked_leg in ("Both", "PE"):
                    if not intraday_pe.empty:
                        pe_hist_df = intraday_pe[["Time", "Close"]].rename(columns={"Close": "LTP"}).tail(plot_points)
                    else:
                        pe_hist = st.session_state.strike_ltp_history.get(f"{history_key_base}|PE", [])
                        pe_hist_df = pd.DataFrame(pe_hist, columns=["Time", "LTP"]).tail(plot_points) if pe_hist else pd.DataFrame()
                    if not pe_hist_df.empty:
                        ltp_fig.add_trace(
                            go.Scatter(
                                x=pe_hist_df["Time"],
                                y=pe_hist_df["LTP"],
                                mode="lines+markers",
                                name=f"PE {picked_strike:,.0f}",
                                line=dict(color="#dc2626", width=2),
                            )
                        )
                        plot_frames.append(pe_hist_df[["Time", "LTP"]].copy())

                if ltp_fig.data:
                    # Keep zoom/scale fixed as set by user across auto-refresh reruns.
                    user_scale_revision = f"ltp-user-{int(round(picked_strike))}-{picked_leg}-{candle_tf}"
                    scale_pref_key = f"{int(round(picked_strike))}|{picked_leg}|{candle_tf}"

                    data_y_min = np.nan
                    data_y_max = np.nan
                    if plot_frames:
                        merged_plot_df = pd.concat(plot_frames, ignore_index=True)
                        merged_plot_df["LTP"] = pd.to_numeric(merged_plot_df["LTP"], errors="coerce")
                        merged_plot_df = merged_plot_df.dropna(subset=["LTP"])
                        if not merged_plot_df.empty:
                            data_y_min = float(merged_plot_df["LTP"].min())
                            data_y_max = float(merged_plot_df["LTP"].max())

                    pref = st.session_state.strike_ltp_scale_prefs.get(scale_pref_key, {})
                    default_lock = bool(pref.get("lock_y", False))
                    if np.isnan(data_y_min) or np.isnan(data_y_max):
                        data_y_min = float(pref.get("y_min", 0.0))
                        data_y_max = float(pref.get("y_max", 1.0))
                    if data_y_max <= data_y_min:
                        data_y_max = data_y_min + 1.0
                    data_pad = max((data_y_max - data_y_min) * 0.05, 0.05)

                    scale_cols = st.columns([1, 1, 1, 1])
                    lock_y_scale = scale_cols[0].checkbox(
                        "Lock Y Scale",
                        value=default_lock,
                        key=f"ltp_lock_y_{scale_pref_key}",
                    )
                    y_min_input = scale_cols[1].number_input(
                        "Y Min",
                        value=float(pref.get("y_min", data_y_min - data_pad)),
                        step=0.5,
                        key=f"ltp_ymin_{scale_pref_key}",
                    )
                    y_max_input = scale_cols[2].number_input(
                        "Y Max",
                        value=float(pref.get("y_max", data_y_max + data_pad)),
                        step=0.5,
                        key=f"ltp_ymax_{scale_pref_key}",
                    )
                    if scale_cols[3].button("Reset Scale", key=f"ltp_reset_scale_{scale_pref_key}"):
                        lock_y_scale = False
                        y_min_input = float(data_y_min - data_pad)
                        y_max_input = float(data_y_max + data_pad)
                        st.session_state[f"ltp_lock_y_{scale_pref_key}"] = lock_y_scale
                        st.session_state[f"ltp_ymin_{scale_pref_key}"] = y_min_input
                        st.session_state[f"ltp_ymax_{scale_pref_key}"] = y_max_input

                    st.session_state.strike_ltp_scale_prefs[scale_pref_key] = {
                        "lock_y": bool(lock_y_scale),
                        "y_min": float(y_min_input),
                        "y_max": float(y_max_input),
                    }

                    ltp_fig.update_xaxes(fixedrange=False)
                    if lock_y_scale and y_max_input > y_min_input:
                        ltp_fig.update_yaxes(range=[y_min_input, y_max_input], autorange=False, fixedrange=False)
                    else:
                        ltp_fig.update_yaxes(autorange=True, fixedrange=False)
                    ltp_fig.update_layout(
                        template="plotly_white",
                        height=320,
                        xaxis_title="Time",
                        yaxis_title="LTP",
                        margin=dict(l=40, r=20, t=40, b=40),
                        title=f"LTP Track - Strike {picked_strike:,.0f} ({expiry})",
                        uirevision=user_scale_revision,
                    )
                    st.plotly_chart(
                        ltp_fig,
                        use_container_width=True,
                        key=f"ltp_chart_{int(round(picked_strike))}_{picked_leg}_{candle_tf}",
                    )
                    if source_labels:
                        st.caption(f"Tracker Source: {', '.join(source_labels)} | Candle: {candle_tf}")
                    elif can_fetch_intraday:
                        st.caption(f"Tracker Source: Snapshot fallback (intraday candles unavailable) | Candle: {candle_tf}")
                    else:
                        st.caption(f"Tracker Source: Snapshot fallback (login required) | Candle: {candle_tf}")
                else:
                    if can_fetch_intraday:
                        st.info("No intraday candles available for this contract yet.")
                    else:
                        st.info("No LTP points collected yet. Keep Auto Refresh ON or login to Upstox for full intraday history.")

                if st.button("Reset LTP Tracker", key="reset_ltp_tracker"):
                    st.session_state.strike_ltp_history = {}
                    st.success("LTP tracker history reset.")
            else:
                st.info("No strikes available for LTP tracking.")

            st.markdown("---")
            st.caption(f"Rows shown: {len(view_chain)} | Total fetched: {len(normalized_chain)}")
            ce_cols = [c for c in ["CE OI", "CE OI Chg", "CE Vol", "CE IV", "CE Delta", "CE Change", "CE LTP"] if c in view_chain.columns]
            pe_cols = [c for c in ["PE LTP", "PE Change", "PE Delta", "PE IV", "PE Vol", "PE OI Chg", "PE OI"] if c in view_chain.columns]
            table_df = view_chain[ce_cols + ["Strike"] + pe_cols].copy()

            format_rules = {"Strike": "{:,.0f}"}
            for col in table_df.columns:
                if col == "Strike":
                    continue
                if "OI" in col or "Vol" in col:
                    format_rules[col] = "{:,.0f}"
                elif "Change" in col:
                    format_rules[col] = "{:+.2f}"
                else:
                    format_rules[col] = "{:,.2f}"

            def style_strike(col):
                return [
                    "background-color:#d8b450;color:#111827;font-weight:700;" if (not pd.isna(v) and abs(float(v) - float(atm_strike)) < 0.001)
                    else "background-color:#f8fafc;color:#0f172a;font-weight:600;"
                    for v in col
                ]

            def style_change(v):
                if pd.isna(v):
                    return ""
                if v > 0:
                    return "background-color:#dcfce7;color:#166534;font-weight:700;"
                if v < 0:
                    return "background-color:#fee2e2;color:#991b1b;font-weight:700;"
                return "background-color:#f1f5f9;color:#334155;font-weight:700;"

            change_cols = [c for c in table_df.columns if "Change" in c]
            styled_chain = (
                table_df.style
                .format(format_rules, na_rep="-")
                .background_gradient(cmap="Greens", subset=[c for c in table_df.columns if c.startswith("CE ") and "Change" not in c])
                .background_gradient(cmap="Reds", subset=[c for c in table_df.columns if c.startswith("PE ") and "Change" not in c])
                .apply(style_strike, subset=["Strike"])
                .set_properties(**{"text-align": "center"})
                .set_table_styles(
                    [
                        {"selector": "th", "props": [("background-color", "#f1f5f9"), ("color", "#0f172a"), ("text-align", "center"), ("font-weight", "700")]},
                        {"selector": "td", "props": [("color", "#0f172a")]},
                    ]
                )
            )
            if change_cols:
                if hasattr(styled_chain, "map"):
                    styled_chain = styled_chain.map(style_change, subset=change_cols)
                else:
                    styled_chain = styled_chain.applymap(style_change, subset=change_cols)

            st.dataframe(styled_chain, use_container_width=True, height=520)
            st.download_button(
                "Download Option Chain CSV",
                data=table_df.to_csv(index=False).encode("utf-8"),
                file_name=f"banknifty_option_chain_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
            )

if auto_refresh:
    time.sleep(refresh_seconds)
    st.rerun()
