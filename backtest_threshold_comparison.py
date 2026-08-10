# -*- coding: utf-8 -*-
"""
検証方法4（tasks/backtest_design_core16_dividend_range_strategy.md）:
候補閾値(20%/30%/40%)ごとに「レンジ位置が閾値以下の銘柄を買う」戦略の
その後1/3/5年トータルリターンを比較し、全日均等投資ベースラインとも比較する。

前提: backtest_range_position_grouping.py / backtest_benchmark_and_timing_check.py と
同じデータキャッシュ・シグナル計算ロジック(bot/pbr_signal.py)を使う。

使い方:
    core16_dividend_botディレクトリで `python backtest_threshold_comparison.py` を実行する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from bot.pbr_signal import build_period_records, compute_daily_signal
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
OUTPUT_PATH = DATA_CACHE / "threshold_comparison_result.json"

HORIZONS_YEARS = [1, 3, 5]
FORWARD_DATE_TOLERANCE_DAYS = 10
CANDIDATE_THRESHOLDS = [20, 30, 40]


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

    return pd.concat(all_rows).sort_index()


def _mean_median_win(series: pd.Series) -> dict:
    s = series.dropna()
    if len(s) == 0:
        return {"n": 0, "mean_return_pct": None, "median_return_pct": None, "win_rate_pct": None}
    return {
        "n": int(len(s)),
        "mean_return_pct": round(float(s.mean()) * 100, 2),
        "median_return_pct": round(float(s.median()) * 100, 2),
        "win_rate_pct": round(float((s > 0).mean()) * 100, 1),
    }


def main() -> int:
    print("全銘柄のシグナル・前方リターンを計算中...")
    combined = _load_all_samples()
    print(f"総サンプル数: {combined.shape[0]}")

    result: dict = {"n_total_samples": int(len(combined)), "thresholds": {}}

    print("\n=== ベースライン（閾値なし・全シグナル日均等投資） ===")
    baseline_stats = {f"{y}y": _mean_median_win(combined[f"fwd_return_{y}y"]) for y in HORIZONS_YEARS}
    result["baseline"] = baseline_stats
    for y in HORIZONS_YEARS:
        r = baseline_stats[f"{y}y"]
        print(f"  {y}年後: n={r['n']}  平均={r['mean_return_pct']}%  中央値={r['median_return_pct']}%  勝率={r['win_rate_pct']}%")

    for threshold in CANDIDATE_THRESHOLDS:
        sub = combined[combined["range_position_pct"] <= threshold]
        print(f"\n=== 閾値 {threshold}%以下（該当日数 n={len(sub)}） ===")
        stats = {}
        for y in HORIZONS_YEARS:
            r = _mean_median_win(sub[f"fwd_return_{y}y"])
            baseline_mean = baseline_stats[f"{y}y"]["mean_return_pct"]
            r["excess_vs_baseline_pct"] = (
                round(r["mean_return_pct"] - baseline_mean, 2)
                if r["mean_return_pct"] is not None and baseline_mean is not None
                else None
            )
            stats[f"{y}y"] = r
            print(
                f"  {y}年後: n={r['n']:6}  平均={r['mean_return_pct']}%  中央値={r['median_return_pct']}%  "
                f"勝率={r['win_rate_pct']}%  ベースライン超過={r['excess_vs_baseline_pct']}%pt"
            )
        result["thresholds"][f"{threshold}%"] = {
            "n_signal_days": int(len(sub)),
            "n_unique_stock_days_per_year_avg": round(len(sub) / max(1, sub.index.year.nunique()), 1),
            "stats": stats,
        }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n結果を保存: {OUTPUT_PATH}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
