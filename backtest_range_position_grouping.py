# -*- coding: utf-8 -*-
"""
検証方法1〜3（tasks/backtest_design_core16_dividend_range_strategy.md）:
「レンジ位置が低いほど、その後のリターンが高い」という仮説を検証する。

手順:
1. 16銘柄それぞれで日次のレンジ位置(%)を算出(bot/pbr_signal.py)
2. レンジ位置を5段階(0-20/20-40/40-60/60-80/80超)にグルーピング
3. 各グループに該当した時点で1株購入したと仮定し、その後1年/3年/5年の
   配当込みトータルリターン(Adj Close比)を集計し、グループ間で比較する

自己相関への配慮（2026-08-07既知の限界として追記）: 同じ銘柄が同じグループに何週間も
連続で該当し続けるため、日次サンプルは互いに独立でない（見かけのサンプル数ほど
統計的な情報量は無い）。この問題を緩和するため、日次サンプルに加えて「各月最初の
営業日のみ」を使った月次サンプルでも同じ集計を行い、結論が変わらないか確認する
（tasks/handoff_next_session.md「OOS逆転の警戒」と同じ精神で、単一の集計方法だけで
結論を出さない）。

前提: fetch_pbr_range_data.py / fetch_yfinance_price_data.py が実行済みで
data_cache/ にキャッシュがあること。test_pbr_signal_synthetic.py が全件合格していること。

使い方:
    core16_dividend_botディレクトリで `python backtest_range_position_grouping.py` を実行する。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from bot.pbr_signal import build_period_records, compute_daily_signal
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
OUTPUT_PATH = DATA_CACHE / "range_position_grouping_result.json"

HORIZONS_YEARS = [1, 3, 5]
BUCKET_EDGES = [-np.inf, 20, 40, 60, 80, np.inf]
BUCKET_LABELS = ["0-20%", "20-40%", "40-60%", "60-80%", "80%超"]

# 目標日から離れすぎた「未来データ」を誤って使わないための許容日数
# （休場日等でぴったりの日付が無い場合の許容誤差。これを超えたら「データ無し」扱いにする）
FORWARD_DATE_TOLERANCE_DAYS = 10


def _load_ticker_signal(ticker: dict) -> pd.DataFrame | None:
    code = ticker["code"]
    price_path = DATA_CACHE / "yfinance_prices" / f"{code}.csv"
    pbr_json_path = DATA_CACHE / "irbank_pbr_range.json"

    if not price_path.exists():
        print(f"  スキップ: 価格キャッシュが無い ({price_path})")
        return None

    price_df = pd.read_csv(price_path, index_col="Date", parse_dates=True)

    with open(pbr_json_path, encoding="utf-8") as f:
        pbr_data = json.load(f)
    periods_raw = pbr_data["tickers"][code]["periods"]
    period_df = build_period_records(periods_raw)

    signal_df = compute_daily_signal(price_df[["Close"]], period_df)
    signal_df["Adj Close"] = price_df["Adj Close"].values
    signal_df["code"] = code
    signal_df["name"] = ticker["name"]
    return signal_df


def _forward_return(adj_close: pd.Series, entry_date: pd.Timestamp, years: int) -> float | None:
    target_date = entry_date + pd.DateOffset(years=years)
    idx = adj_close.index.searchsorted(target_date)
    if idx >= len(adj_close):
        return None
    actual_date = adj_close.index[idx]
    if (actual_date - target_date).days > FORWARD_DATE_TOLERANCE_DAYS:
        return None
    entry_price = adj_close.loc[entry_date]
    exit_price = adj_close.iloc[idx]
    if entry_price <= 0:
        return None
    return exit_price / entry_price - 1.0


def _build_samples(signal_df: pd.DataFrame) -> pd.DataFrame:
    """1銘柄分のsignal_dfから、有効なレンジ位置を持つ日について
    1y/3y/5yの前方トータルリターンを付与したサンプル表を作る。"""
    valid = signal_df.dropna(subset=["range_position_pct"]).copy()
    if valid.empty:
        return valid

    adj_close = signal_df["Adj Close"]
    for years in HORIZONS_YEARS:
        col = f"fwd_return_{years}y"
        valid[col] = [
            _forward_return(adj_close, date, years) for date in valid.index
        ]
    return valid


def _aggregate_by_bucket(samples: pd.DataFrame, horizon_years: int) -> dict:
    col = f"fwd_return_{horizon_years}y"
    df = samples.dropna(subset=[col]).copy()
    df["bucket"] = pd.cut(df["range_position_pct"], bins=BUCKET_EDGES, labels=BUCKET_LABELS)

    out = {}
    for label in BUCKET_LABELS:
        sub = df[df["bucket"] == label][col]
        if len(sub) == 0:
            out[label] = {"n": 0, "mean_return_pct": None, "median_return_pct": None, "win_rate_pct": None}
            continue
        out[label] = {
            "n": int(len(sub)),
            "mean_return_pct": round(float(sub.mean()) * 100, 2),
            "median_return_pct": round(float(sub.median()) * 100, 2),
            "win_rate_pct": round(float((sub > 0).mean()) * 100, 1),
        }
    return out


def _monthly_resample(samples: pd.DataFrame) -> pd.DataFrame:
    """自己相関緩和のための月次サブサンプル（各暦月の最初のサンプルのみを残す）。"""
    if samples.empty:
        return samples
    month_key = samples.index.to_period("M")
    first_idx_per_month = samples.groupby(month_key, observed=True).apply(lambda g: g.index.min())
    return samples.loc[first_idx_per_month.values]


def main() -> int:
    all_samples = []
    for i, ticker in enumerate(CORE16_UNIVERSE):
        print(f"[{i + 1}/{len(CORE16_UNIVERSE)}] {ticker['code']} {ticker['name']} のシグナルを計算中...")
        signal_df = _load_ticker_signal(ticker)
        if signal_df is None:
            continue
        samples = _build_samples(signal_df)
        n_valid_signal = signal_df["range_position_pct"].notna().sum()
        print(f"  有効シグナル日数: {n_valid_signal} / {len(signal_df)}日、前方リターン付与サンプル: {len(samples)}件")
        all_samples.append(samples)

    combined = pd.concat(all_samples) if all_samples else pd.DataFrame()
    combined = combined.sort_index()

    monthly = _monthly_resample(combined)

    result = {
        "n_total_daily_samples": int(len(combined)),
        "n_total_monthly_samples": int(len(monthly)),
        "daily": {
            f"{y}y": _aggregate_by_bucket(combined, y) for y in HORIZONS_YEARS
        },
        "monthly_resampled": {
            f"{y}y": _aggregate_by_bucket(monthly, y) for y in HORIZONS_YEARS
        },
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n結果を保存: {OUTPUT_PATH}")
    print(f"\n=== 日次サンプル（自己相関あり注意） 全{result['n_total_daily_samples']}件 ===")
    for y in HORIZONS_YEARS:
        print(f"\n--- {y}年後リターン ---")
        for label in BUCKET_LABELS:
            r = result["daily"][f"{y}y"][label]
            print(f"  {label:8}: n={r['n']:6}  平均={r['mean_return_pct']}%  中央値={r['median_return_pct']}%  勝率={r['win_rate_pct']}%")

    print(f"\n=== 月次サブサンプル（自己相関緩和） 全{result['n_total_monthly_samples']}件 ===")
    for y in HORIZONS_YEARS:
        print(f"\n--- {y}年後リターン ---")
        for label in BUCKET_LABELS:
            r = result["monthly_resampled"][f"{y}y"][label]
            print(f"  {label:8}: n={r['n']:6}  平均={r['mean_return_pct']}%  中央値={r['median_return_pct']}%  勝率={r['win_rate_pct']}%")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
