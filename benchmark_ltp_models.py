import argparse
import json
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import tensorflow as tf
from sklearn.metrics import mean_absolute_error, mean_squared_error

from src.nhits_model import NHITSForecaster
from src.options_lstm_forecaster import OptionsLSTMForecaster
from src.options_microstructure_forecaster import OptionsMicrostructureForecaster


IST = "Asia/Kolkata"
FORECAST_STEPS = 24


def _load_option_series(expiry: str, option_type: str, strike: int) -> pd.Series:
    path = Path("data/option_ltp") / f"BANKNIFTY_{expiry}_{option_type.upper()}" / f"strike_{int(strike)}.pkl"
    with path.open("rb") as handle:
        obj = pickle.load(handle)
    series = pd.to_numeric(pd.Series(obj), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    index = pd.to_datetime(series.index, errors="coerce")
    valid = ~pd.isna(index)
    series = series.loc[valid]
    index = index[valid]
    if getattr(index, "tz", None) is None:
        index = index.tz_localize(IST)
    else:
        index = index.tz_convert(IST)
    series.index = index
    series = series[~series.index.duplicated(keep="last")].sort_index()
    return series[series > 0]


def _load_underlying() -> pd.Series:
    df = pd.read_parquet("data/historical_prices/NSEBANK_5m.parquet")
    if "Datetime" in df.columns:
        idx = pd.to_datetime(df["Datetime"], errors="coerce")
    elif "Date" in df.columns:
        idx = pd.to_datetime(df["Date"], errors="coerce")
    else:
        idx = pd.to_datetime(df.index, errors="coerce")
    valid = ~pd.isna(idx)
    df = df.loc[valid].copy()
    idx = idx[valid]
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize(IST)
    else:
        idx = idx.tz_convert(IST)
    df.index = idx
    close_col = "Close" if "Close" in df.columns else "close"
    series = pd.to_numeric(df[close_col], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    return series[~series.index.duplicated(keep="last")].sort_index()


def _series_to_frame(series: pd.Series) -> pd.DataFrame:
    series = pd.to_numeric(pd.Series(series), errors="coerce").dropna()
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


def _align(series: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    if series is None:
        return None
    out = pd.to_numeric(pd.Series(series), errors="coerce")
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index, errors="coerce")
    if getattr(out.index, "tz", None) is None:
        out.index = out.index.tz_localize(IST)
    else:
        out.index = out.index.tz_convert(IST)
    return out.reindex(index).ffill().bfill()


def _metrics(actual: pd.Series, forecast) -> dict:
    pred = pd.to_numeric(pd.Series(forecast), errors="coerce").dropna().iloc[: len(actual)]
    actual = pd.to_numeric(pd.Series(actual), errors="coerce").dropna().iloc[: len(pred)]
    if len(actual) == 0 or len(pred) == 0:
        return {"mae": np.nan, "rmse": np.nan, "dir_acc": np.nan, "endpoint_abs_error": np.nan}
    actual_vals = actual.to_numpy(dtype=float)
    pred_vals = pred.to_numpy(dtype=float)
    act_dir = np.sign(np.diff(np.r_[actual_vals[0], actual_vals]))
    pred_dir = np.sign(np.diff(np.r_[actual_vals[0], pred_vals]))
    return {
        "mae": float(mean_absolute_error(actual_vals, pred_vals)),
        "rmse": float(np.sqrt(mean_squared_error(actual_vals, pred_vals))),
        "dir_acc": float((act_dir == pred_dir).mean() * 100.0),
        "endpoint_abs_error": float(abs(actual_vals[-1] - pred_vals[-1])),
    }


def _run_persistence(train: pd.Series, actual: pd.Series) -> dict:
    last_val = float(train.iloc[-1])
    forecast = [last_val] * len(actual)
    return {"forecast": forecast, "metrics": _metrics(actual, forecast)}


def _run_lstm(train: pd.Series, actual: pd.Series, strike: int, option_type: str, underlying: pd.Series) -> dict:
    max_feasible = max(24, len(train) - FORECAST_STEPS - 10)
    lookback = int(min(96, max_feasible))
    model = OptionsLSTMForecaster(
        lookback=lookback,
        forecast_steps=FORECAST_STEPS,
        strike=strike,
        option_type=option_type,
    )
    aligned_under = _align(underlying, train.index)
    ok, info = model.train(
        train,
        epochs=16 if len(train) >= 300 else 12,
        batch_size=16,
        validation_split=0.15,
        underlying_series=aligned_under,
    )
    if not ok:
        return {"error": info.get("error", "train failed")}
    pred = model.forecast(train, underlying_series=aligned_under)
    if not pred or pred.get("forecast") is None or len(pred.get("forecast")) == 0:
        return {"error": "forecast failed"}
    return {"forecast": list(pred["forecast"]), "metrics": _metrics(actual, pred["forecast"])}


def _run_nhits(train: pd.Series, actual: pd.Series) -> dict:
    min_buffer = FORECAST_STEPS + 20
    max_input = len(train) - min_buffer
    if max_input < 48:
        return {"error": f"insufficient data: {len(train)} rows"}
    preferred_inputs = (180, 144, 120, 96, 72, 48)
    input_size = next((val for val in preferred_inputs if val <= max_input), max(48, int(max_input)))
    df = pd.DataFrame({"Close": train.values}, index=train.index)
    model = NHITSForecaster(forecast_steps=FORECAST_STEPS, input_size=input_size)
    ok, info = model.train(df, max_steps=240, tune=False, profile="option_intraday")
    if not ok:
        return {"error": info.get("error", "train failed")}
    pred = model.predict_sequence(df)
    if pred is None or pred.get("median") is None or len(pred.get("median")) == 0:
        return {"error": "forecast failed"}
    return {"forecast": list(pred["median"]), "metrics": _metrics(actual, pred["median"])}


def _run_catboost(train: pd.Series, actual: pd.Series, strike: int, option_type: str, underlying: pd.Series) -> dict:
    frame = _series_to_frame(train)
    model = OptionsMicrostructureForecaster(
        strike=strike,
        option_type=option_type,
        forecast_steps=FORECAST_STEPS,
    )
    ok, info = model.train(
        frame,
        underlying_series=_align(underlying, train.index),
        iterations=150,
    )
    if not ok:
        return {"error": info.get("error", "train failed")}
    pred = model.predict_sequence(frame, underlying_series=_align(underlying, train.index))
    if pred is None or pred.get("median") is None or len(pred.get("median")) == 0:
        return {"error": "forecast failed"}
    return {"forecast": list(pred["median"]), "metrics": _metrics(actual, pred["median"])}


def benchmark_contract(expiry: str, option_type: str, strike: int, underlying: pd.Series) -> dict:
    series = _load_option_series(expiry, option_type, strike)
    if len(series) < 260:
        return {"error": f"insufficient series length: {len(series)}"}
    train = series.iloc[:-FORECAST_STEPS]
    actual = series.iloc[-FORECAST_STEPS:]
    return {
        "contract": f"{expiry}-{option_type.upper()}-{int(strike)}",
        "points": int(len(series)),
        "persistence": _run_persistence(train, actual),
        "lstm": _run_lstm(train, actual, strike, option_type.upper(), underlying),
        "nhits": _run_nhits(train, actual),
        "catboost_micro": _run_catboost(train, actual, strike, option_type.upper(), underlying),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--contracts",
        nargs="*",
        default=["2026-03-30:CE:57000", "2026-03-30:PE:57000"],
        help="Items formatted as expiry:option_type:strike",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a human table")
    args = parser.parse_args()

    np.random.seed(42)
    tf.random.set_seed(42)

    underlying = _load_underlying()
    results = []
    for item in args.contracts:
        expiry, option_type, strike = item.split(":")
        results.append(benchmark_contract(expiry, option_type, int(float(strike)), underlying))

    if args.json:
        print(json.dumps(results, indent=2))
        return

    for result in results:
        print(f"\n=== {result['contract']} | points={result.get('points')} ===")
        for model_name in ["persistence", "lstm", "nhits", "catboost_micro"]:
            payload = result.get(model_name, {})
            if payload.get("error"):
                print(f"{model_name:16s} ERROR  {payload['error']}")
                continue
            metric = payload.get("metrics", {})
            print(
                f"{model_name:16s} "
                f"MAE={metric.get('mae', np.nan):8.2f} "
                f"RMSE={metric.get('rmse', np.nan):8.2f} "
                f"DIR={metric.get('dir_acc', np.nan):6.2f}% "
                f"END={metric.get('endpoint_abs_error', np.nan):8.2f}"
            )

    aggregate = {}
    for model_name in ["persistence", "lstm", "nhits", "catboost_micro"]:
        metrics = [r.get(model_name, {}).get("metrics", {}) for r in results if r.get(model_name, {}).get("metrics")]
        if not metrics:
            continue
        aggregate[model_name] = {
            "mae": float(np.nanmean([m.get("mae", np.nan) for m in metrics])),
            "rmse": float(np.nanmean([m.get("rmse", np.nan) for m in metrics])),
            "dir_acc": float(np.nanmean([m.get("dir_acc", np.nan) for m in metrics])),
            "endpoint_abs_error": float(np.nanmean([m.get("endpoint_abs_error", np.nan) for m in metrics])),
        }
    if aggregate:
        print("\n=== aggregate ===")
        for model_name, metric in aggregate.items():
            print(
                f"{model_name:16s} "
                f"MAE={metric['mae']:8.2f} "
                f"RMSE={metric['rmse']:8.2f} "
                f"DIR={metric['dir_acc']:6.2f}% "
                f"END={metric['endpoint_abs_error']:8.2f}"
            )


if __name__ == "__main__":
    main()
