# -*- coding: utf-8 -*-
"""
ユーザー提案（2026-08-07「増配率も考慮して閾値は個別に設定した方が良いのでは」）への
パネルレビュー対応: 16銘柄個別の絶対閾値を手動最適化する代わりに、「銘柄自身の過去の
レンジ位置分布の中で下位N%に入っているか」という自己校正型の判定（自由パラメータは
percentile1個のみ）を検証する。bot/pbr_signal.compute_expanding_percentile_flag参照。

固定閾値30%（backtest_threshold_comparison.pyの推奨値）と比較し、
①NTT/KDDI等「絶対閾値では拾えない銘柄」でシグナルが出るようになるか
②ベースライン・固定閾値と比べてパフォーマンスがどうなるか
を確認する。

使い方:
    core16_dividend_botディレクトリで `python backtest_adaptive_percentile_threshold.py` を実行する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from bot.pbr_signal import build_period_records, compute_daily_signal, compute_expanding_percentile_flag
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
OUTPUT_PATH = DATA_CACHE / "adaptive_percentile_threshold_result.json"

HORIZONS_YEARS = [1, 3, 5]
FORWARD_DATE_TOLERANCE_DAYS = 10
PERCENTILE = 20.0
FIXED_THRESHOLD = 30


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
    with open(DATA_CACHE / "irbank_pbr_range.json", encoding="utf-8") as f:
        pbr_data = json.load(f)

    all_rows = []
    per_ticker_n = {}
    print("全銘柄のシグナル・拡張パーセンタイル判定を計算中...")
    for i, ticker in enumerate(CORE16_UNIVERSE):
        code = ticker["code"]
        price_path = DATA_CACHE / "yfinance_prices" / f"{code}.csv"
        if not price_path.exists():
            continue
        price_df = pd.read_csv(price_path, index_col="Date", parse_dates=True)
        period_df = build_period_records(pbr_data["tickers"][code]["periods"])
        sig = compute_daily_signal(price_df[["Close"]], period_df)
        sig["Adj Close"] = price_df["Adj Close"].values

        sig["adaptive_flag"] = compute_expanding_percentile_flag(sig["range_position_pct"], percentile=PERCENTILE)

        valid = sig.dropna(subset=["range_position_pct"]).copy()
        if valid.empty:
            per_ticker_n[code] = {"name": ticker["name"], "n_fixed30": 0, "n_adaptive": 0}
            continue
        adj_close = sig["Adj Close"]
        for years in HORIZONS_YEARS:
            valid[f"fwd_return_{years}y"] = [_forward_return(adj_close, d, years) for d in valid.index]
        valid["code"] = code

        n_fixed = int((valid["range_position_pct"] <= FIXED_THRESHOLD).sum())
        n_adaptive = int((valid["adaptive_flag"] == True).sum())  # noqa: E712
        per_ticker_n[code] = {"name": ticker["name"], "n_fixed30": n_fixed, "n_adaptive": n_adaptive}
        print(f"  [{i + 1}/16] {code} {ticker['name']:10}: 固定30%閾値={n_fixed:5}件  適応的下位{int(PERCENTILE)}%={n_adaptive:5}件")

        all_rows.append(valid)

    combined = pd.concat(all_rows).sort_index()

    fixed_sub = combined[combined["range_position_pct"] <= FIXED_THRESHOLD]
    adaptive_sub = combined[combined["adaptive_flag"] == True]  # noqa: E712

    result = {
        "percentile": PERCENTILE,
        "fixed_threshold": FIXED_THRESHOLD,
        "per_ticker_signal_counts": per_ticker_n,
        "n_total_samples": int(len(combined)),
        "n_fixed30": int(len(fixed_sub)),
        "n_adaptive": int(len(adaptive_sub)),
    }

    print(f"\n=== ベースライン（全シグナル日、n={len(combined)}） ===")
    baseline_stats = {f"{y}y": _mean_median_win(combined[f"fwd_return_{y}y"]) for y in HORIZONS_YEARS}
    result["baseline"] = baseline_stats
    for y in HORIZONS_YEARS:
        r = baseline_stats[f"{y}y"]
        print(f"  {y}年後: n={r['n']}  平均={r['mean_return_pct']}%")

    print(f"\n=== 固定閾値{FIXED_THRESHOLD}%以下（n={len(fixed_sub)}） ===")
    result["fixed"] = {}
    for y in HORIZONS_YEARS:
        r = _mean_median_win(fixed_sub[f"fwd_return_{y}y"])
        r["excess_vs_baseline_pct"] = (
            round(r["mean_return_pct"] - baseline_stats[f"{y}y"]["mean_return_pct"], 2)
            if r["mean_return_pct"] is not None
            else None
        )
        result["fixed"][f"{y}y"] = r
        print(f"  {y}年後: n={r['n']:6}  平均={r['mean_return_pct']}%  超過={r['excess_vs_baseline_pct']}%pt")

    print(f"\n=== 適応的パーセンタイル(下位{int(PERCENTILE)}%、n={len(adaptive_sub)}) ===")
    result["adaptive"] = {}
    for y in HORIZONS_YEARS:
        r = _mean_median_win(adaptive_sub[f"fwd_return_{y}y"])
        r["excess_vs_baseline_pct"] = (
            round(r["mean_return_pct"] - baseline_stats[f"{y}y"]["mean_return_pct"], 2)
            if r["mean_return_pct"] is not None
            else None
        )
        result["adaptive"][f"{y}y"] = r
        print(f"  {y}年後: n={r['n']:6}  平均={r['mean_return_pct']}%  超過={r['excess_vs_baseline_pct']}%pt")

    n_tickers_never_fire_fixed = sum(1 for v in per_ticker_n.values() if v["n_fixed30"] == 0)
    n_tickers_never_fire_adaptive = sum(1 for v in per_ticker_n.values() if v["n_adaptive"] == 0)
    print(f"\n一度もシグナルが出ない銘柄数: 固定閾値={n_tickers_never_fire_fixed}  適応的パーセンタイル={n_tickers_never_fire_adaptive}")
    result["n_tickers_never_fire"] = {"fixed": n_tickers_never_fire_fixed, "adaptive": n_tickers_never_fire_adaptive}

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n結果を保存: {OUTPUT_PATH}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
