# -*- coding: utf-8 -*-
"""
検証方法6（ベンチマーク比較、2026-08-07パネルレビュー・ファンドマネージャー指摘）と、
0-20%バケットの好成績が特定年（時期集中）だけによるものでないかの頑健性確認
（同日パネルレビュー・QA/ドメインエキスパート指摘）を1本にまとめて実施する。

1. ベースライン比較: 「シグナルを無視して毎日均等に16銘柄すべてに投資し続けた場合」
   （プールした全サンプルの平均前方リターン）を基準とし、0-20%バケットがそれを
   上回る「超過リターン」を持つかを見る。
2. 時期集中の頑健性確認: 0-20%バケット該当日を年別に集計し、上位集中年（2016/2019/2020、
   backtest_range_position_grouping.py実行時に確認済み）を除外しても優位性が
   残るかを確認する。

前提: backtest_range_position_grouping.py と同じデータキャッシュを使う。

使い方:
    core16_dividend_botディレクトリで `python backtest_benchmark_and_timing_check.py` を実行する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from bot.pbr_signal import build_period_records, compute_daily_signal
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
OUTPUT_PATH = DATA_CACHE / "benchmark_and_timing_check_result.json"

HORIZONS_YEARS = [1, 3, 5]
FORWARD_DATE_TOLERANCE_DAYS = 10
LOW_BUCKET_UPPER = 20.0

# backtest_range_position_grouping.py実行結果で確認済みの、0-20%バケット該当日が
# 特に集中していた年（合計で全体の53%）。この年を除いても優位性が残るかを確認する。
CONCENTRATED_YEARS = {2016, 2019, 2020}


def _forward_return(adj_close: pd.Series, entry_date: pd.Timestamp, years: int) -> float | None:
    target_date = entry_date + pd.DateOffset(years=years)
    idx = adj_close.index.searchsorted(target_date)
    if idx >= len(adj_close):
        return None
    actual_date = adj_close.index[idx]
    if (actual_date - target_date).days > FORWARD_DATE_TOLERANCE_DAYS:
        return None
    entry_price = adj_close.loc[entry_date]
    if entry_price <= 0:
        return None
    return adj_close.iloc[idx] / entry_price - 1.0


def _load_all_samples() -> pd.DataFrame:
    with open(DATA_CACHE / "irbank_pbr_range.json", encoding="utf-8") as f:
        pbr_data = json.load(f)

    all_rows = []
    for ticker in CORE16_UNIVERSE:
        code = ticker["code"]
        price_path = DATA_CACHE / "yfinance_prices" / f"{code}.csv"
        if not price_path.exists():
            continue
        price_df = pd.read_csv(price_path, index_col="Date", parse_dates=True)
        period_df = build_period_records(pbr_data["tickers"][code]["periods"])
        sig = compute_daily_signal(price_df[["Close"]], period_df)
        sig["Adj Close"] = price_df["Adj Close"].values

        valid = sig.dropna(subset=["range_position_pct"]).copy()
        if valid.empty:
            continue
        adj_close = sig["Adj Close"]
        for years in HORIZONS_YEARS:
            valid[f"fwd_return_{years}y"] = [
                _forward_return(adj_close, d, years) for d in valid.index
            ]
        valid["code"] = code
        all_rows.append(valid)

    combined = pd.concat(all_rows).sort_index()
    return combined


def _mean_pct(series: pd.Series) -> float | None:
    s = series.dropna()
    return round(float(s.mean()) * 100, 2) if len(s) else None


def main() -> int:
    print("全銘柄のシグナル・前方リターンを再計算中...")
    combined = _load_all_samples()
    print(f"総サンプル数: {len(combined)}")

    low_bucket = combined[combined["range_position_pct"] < LOW_BUCKET_UPPER]
    entry_years = low_bucket.index.year

    result: dict = {"n_total_samples": int(len(combined)), "n_low_bucket": int(len(low_bucket))}

    print("\n=== 1. ベースライン比較（シグナル無視で毎日均等投資 vs 0-20%バケットのみ） ===")
    result["baseline_vs_low_bucket"] = {}
    for years in HORIZONS_YEARS:
        col = f"fwd_return_{years}y"
        baseline_mean = _mean_pct(combined[col])
        low_bucket_mean = _mean_pct(low_bucket[col])
        excess = (
            round(low_bucket_mean - baseline_mean, 2)
            if baseline_mean is not None and low_bucket_mean is not None
            else None
        )
        result["baseline_vs_low_bucket"][f"{years}y"] = {
            "baseline_mean_return_pct": baseline_mean,
            "low_bucket_mean_return_pct": low_bucket_mean,
            "excess_return_pct": excess,
        }
        print(f"  {years}年後: ベースライン(全日均等)={baseline_mean}%  0-20%バケット={low_bucket_mean}%  超過={excess}%pt")

    print("\n=== 2. 時期集中の頑健性確認（2016/2019/2020を除外しても優位性が残るか） ===")
    excluded = low_bucket[~entry_years.isin(CONCENTRATED_YEARS)]
    print(f"  除外前n={len(low_bucket)}件 → 除外後n={len(excluded)}件")
    result["excluding_concentrated_years"] = {"n": int(len(excluded))}
    for years in HORIZONS_YEARS:
        col = f"fwd_return_{years}y"
        baseline_mean = result["baseline_vs_low_bucket"][f"{years}y"]["baseline_mean_return_pct"]
        excl_mean = _mean_pct(excluded[col])
        excess = round(excl_mean - baseline_mean, 2) if excl_mean is not None and baseline_mean is not None else None
        result["excluding_concentrated_years"][f"{years}y"] = {
            "mean_return_pct": excl_mean,
            "excess_vs_baseline_pct": excess,
        }
        print(f"  {years}年後: 集中年除外後の0-20%バケット={excl_mean}%  ベースライン比 超過={excess}%pt")

    print("\n=== 3. 0-20%バケット該当日の年別リターン内訳 ===")
    result["low_bucket_by_entry_year"] = {}
    for year in sorted(set(entry_years)):
        year_sub = low_bucket[entry_years == year]
        row = {"n": int(len(year_sub))}
        for years in HORIZONS_YEARS:
            row[f"{years}y_mean_return_pct"] = _mean_pct(year_sub[f"fwd_return_{years}y"])
        result["low_bucket_by_entry_year"][str(year)] = row
        print(
            f"  {year}: n={row['n']:5}  1y={row.get('1y_mean_return_pct')}%  "
            f"3y={row.get('3y_mean_return_pct')}%  5y={row.get('5y_mean_return_pct')}%"
        )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n結果を保存: {OUTPUT_PATH}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
