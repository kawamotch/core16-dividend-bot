# -*- coding: utf-8 -*-
"""
backtest_walkforward_threshold.py のEDINET新方式版（2026-08-17、Phase B結論確認用）。

分析ロジック（暦年ウィンドウごとの同一母集団内比較）は元スクリプトと完全に同一。
唯一の違いは`_load_all_samples()`のデータ源で、IRBANK(bot.pbr_signal)ではなく
EDINETベースの新方式（edinet_signal_adapter）を使う。既存ファイルは変更しない。

使い方:
    core16_dividend_botディレクトリで `python backtest_walkforward_threshold_edinet.py` を実行する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from edinet_signal_adapter import compute_daily_signal_edinet
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
OUTPUT_PATH = DATA_CACHE / "walkforward_threshold_edinet_result.json"

HORIZONS_YEARS = [1, 3, 5]
FORWARD_DATE_TOLERANCE_DAYS = 10
THRESHOLD = 30
WINDOW_YEARS = list(range(2014, 2027))


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
    all_rows = []
    for ticker in CORE16_UNIVERSE:
        code = ticker["code"]
        price_path = DATA_CACHE / "yfinance_prices" / f"{code}.csv"
        if not price_path.exists():
            continue
        sig = compute_daily_signal_edinet(code)
        if sig is None:
            continue

        valid = sig.dropna(subset=["range_position_pct"]).copy()
        if valid.empty:
            continue
        adj_close = sig["Adj Close"]  # 配当込み総リターン（IRBANK版と条件を揃える）
        for years in HORIZONS_YEARS:
            valid[f"fwd_return_{years}y"] = [
                _forward_return(adj_close, d, years) for d in valid.index
            ]
        valid["code"] = code
        all_rows.append(valid)

    return pd.concat(all_rows).sort_index()


def _mean(series: pd.Series) -> float | None:
    s = series.dropna()
    return round(float(s.mean()) * 100, 2) if len(s) else None


def main() -> int:
    print("全銘柄のシグナル(EDINET新方式)・前方リターンを計算中...")
    combined = _load_all_samples()
    entry_years = combined.index.year

    result: dict = {"threshold_pct": THRESHOLD, "windows": {}}

    print(f"\n=== ウィンドウ別（同ウィンドウ内で 閾値{THRESHOLD}%以下 vs 全シグナル日 を比較） ===")
    win_counts = {f"{y}y": {"win": 0, "lose": 0, "tie_or_na": 0} for y in HORIZONS_YEARS}

    for year in WINDOW_YEARS:
        window_all = combined[entry_years == year]
        if len(window_all) == 0:
            continue
        window_thresh = window_all[window_all["range_position_pct"] <= THRESHOLD]

        row = {"n_all": int(len(window_all)), "n_threshold": int(len(window_thresh))}
        for y in HORIZONS_YEARS:
            all_mean = _mean(window_all[f"fwd_return_{y}y"])
            thresh_mean = _mean(window_thresh[f"fwd_return_{y}y"])
            row[f"{y}y_all_mean_pct"] = all_mean
            row[f"{y}y_threshold_mean_pct"] = thresh_mean
            if all_mean is None or thresh_mean is None:
                win_counts[f"{y}y"]["tie_or_na"] += 1
                row[f"{y}y_threshold_wins"] = None
            elif thresh_mean > all_mean:
                win_counts[f"{y}y"]["win"] += 1
                row[f"{y}y_threshold_wins"] = True
            else:
                win_counts[f"{y}y"]["lose"] += 1
                row[f"{y}y_threshold_wins"] = False

        result["windows"][str(year)] = row
        print(
            f"  {year}: n(全)={row['n_all']:5} n(閾値)={row['n_threshold']:5}  "
            f"1y: 全={row['1y_all_mean_pct']}% 閾値={row['1y_threshold_mean_pct']}%  "
            f"3y: 全={row['3y_all_mean_pct']}% 閾値={row['3y_threshold_mean_pct']}%  "
            f"5y: 全={row['5y_all_mean_pct']}% 閾値={row['5y_threshold_mean_pct']}%"
        )

    print("\n=== ウィンドウ勝敗集計（閾値グループが同ウィンドウの全シグナル日平均を上回った回数） ===")
    result["win_counts"] = win_counts
    for y in HORIZONS_YEARS:
        wc = win_counts[f"{y}y"]
        print(f"  {y}年後: {wc['win']}勝{wc['lose']}敗（データ不足{wc['tie_or_na']}窓）")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n結果を保存: {OUTPUT_PATH}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
