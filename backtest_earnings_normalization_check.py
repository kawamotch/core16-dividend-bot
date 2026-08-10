# -*- coding: utf-8 -*-
"""
検証方法5（tasks/backtest_design_core16_dividend_range_strategy.md）:
業績正常化チェック（純利益[EPS代理]が過去5年平均から±30%超乖離している期を除外）の
有無で、閾値30%以下戦略の成績がどう変わるかを比較する。

前提: 他のbacktest_*.pyと同じデータキャッシュ・bot/pbr_signal.pyのロジックを使う。

使い方:
    core16_dividend_botディレクトリで `python backtest_earnings_normalization_check.py` を実行する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from bot.pbr_signal import build_period_records, compute_daily_signal
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
OUTPUT_PATH = DATA_CACHE / "earnings_normalization_check_result.json"

HORIZONS_YEARS = [1, 3, 5]
FORWARD_DATE_TOLERANCE_DAYS = 10
THRESHOLD = 30  # 検証方法4の推奨閾値で比較する


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
    print("全銘柄のシグナル・前方リターン・業績正常化フラグを計算中...")
    combined = _load_all_samples()
    thresholded = combined[combined["range_position_pct"] <= THRESHOLD]
    print(f"閾値{THRESHOLD}%以下の該当日数: {len(thresholded)}")
    print(f"うち業績異常フラグ該当: {int(thresholded['earnings_anomaly'].sum())}件"
          f"（{thresholded['earnings_anomaly'].mean() * 100:.1f}%）")

    without_check = thresholded
    with_check = thresholded[~thresholded["earnings_anomaly"]]

    result = {
        "threshold_pct": THRESHOLD,
        "n_without_check": int(len(without_check)),
        "n_with_check": int(len(with_check)),
        "n_excluded_by_check": int(len(without_check) - len(with_check)),
        "without_check": {},
        "with_check": {},
    }

    print(f"\n=== 閾値{THRESHOLD}%以下・業績正常化チェックなし (n={len(without_check)}) ===")
    for y in HORIZONS_YEARS:
        r = _mean_median_win(without_check[f"fwd_return_{y}y"])
        result["without_check"][f"{y}y"] = r
        print(f"  {y}年後: n={r['n']}  平均={r['mean_return_pct']}%  中央値={r['median_return_pct']}%  勝率={r['win_rate_pct']}%")

    print(f"\n=== 閾値{THRESHOLD}%以下・業績正常化チェックあり(異常期を除外、n={len(with_check)}) ===")
    for y in HORIZONS_YEARS:
        r = _mean_median_win(with_check[f"fwd_return_{y}y"])
        without_mean = result["without_check"][f"{y}y"]["mean_return_pct"]
        r["diff_vs_without_check_pct"] = (
            round(r["mean_return_pct"] - without_mean, 2)
            if r["mean_return_pct"] is not None and without_mean is not None
            else None
        )
        result["with_check"][f"{y}y"] = r
        print(
            f"  {y}年後: n={r['n']}  平均={r['mean_return_pct']}%  中央値={r['median_return_pct']}%  "
            f"勝率={r['win_rate_pct']}%  チェック無しとの差={r['diff_vs_without_check_pct']}%pt"
        )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n結果を保存: {OUTPUT_PATH}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
