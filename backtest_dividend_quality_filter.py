# -*- coding: utf-8 -*-
"""
ユーザー提案の後半（増配率を考慮する）を検証する。適応的パーセンタイル方式が優位性を
壊してしまったため（backtest_adaptive_percentile_threshold.py参照）、個別閾値の代わりに
「固定閾値30%に、配当の質（直近5期減配なし）フィルターを追加で重ねる」方式を試す。

使い方:
    core16_dividend_botディレクトリで `python backtest_dividend_quality_filter.py` を実行する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from bot.pbr_signal import build_period_records, compute_daily_signal
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
OUTPUT_PATH = DATA_CACHE / "dividend_quality_filter_result.json"

HORIZONS_YEARS = [1, 3, 5]
FORWARD_DATE_TOLERANCE_DAYS = 10
THRESHOLD = 30
DIVIDEND_DISCLOSURE_LAG_DAYS = 45  # bot/pbr_signal.DISCLOSURE_LAG_DAYSと同じ保守的仮定
CLEAN_STREAK_LOOKBACK_PERIODS = 5


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


def _build_dividend_clean_flag_series(div_periods_raw: list[dict], dates: pd.DatetimeIndex) -> pd.Series:
    """各日について、「直近開示済みの配当実績を遡ってCLEAN_STREAK_LOOKBACK_PERIODS期の間、
    一度も減配していない」かを判定するSeriesを返す（先読み回避のため開示ラグ付きffill）。"""
    df = pd.DataFrame(div_periods_raw).dropna(subset=["dps_adjusted"]).copy()
    df["end_date"] = pd.to_datetime(df["end_date"])
    df = df.sort_values("end_date").reset_index(drop=True)
    df["disclosure_date"] = df["end_date"] + pd.Timedelta(days=DIVIDEND_DISCLOSURE_LAG_DAYS)

    # 各期について「直近5期減配なし」かを判定
    clean_flags = [False] * len(df)
    for i in range(len(df)):
        window = df["dps_adjusted"].iloc[max(0, i - CLEAN_STREAK_LOOKBACK_PERIODS) : i + 1]
        if len(window) < CLEAN_STREAK_LOOKBACK_PERIODS:
            continue
        cuts = sum(1 for j in range(1, len(window)) if window.iloc[j] < window.iloc[j - 1] * 0.999)
        clean_flags[i] = cuts == 0
    df["clean_streak"] = clean_flags

    result = pd.Series(False, index=dates)
    for date in dates:
        disclosed = df[df["disclosure_date"] <= date]
        if len(disclosed) == 0:
            continue
        result.loc[date] = bool(disclosed.iloc[-1]["clean_streak"])
    return result


def main() -> int:
    with open(DATA_CACHE / "irbank_pbr_range.json", encoding="utf-8") as f:
        pbr_data = json.load(f)
    with open(DATA_CACHE / "irbank_dividend_history.json", encoding="utf-8") as f:
        div_data = json.load(f)

    all_rows = []
    print("全銘柄のシグナル・配当クリーンストリーク判定を計算中...")
    for i, ticker in enumerate(CORE16_UNIVERSE):
        code = ticker["code"]
        price_path = DATA_CACHE / "yfinance_prices" / f"{code}.csv"
        if not price_path.exists():
            continue
        price_df = pd.read_csv(price_path, index_col="Date", parse_dates=True)
        period_df = build_period_records(pbr_data["tickers"][code]["periods"])
        sig = compute_daily_signal(price_df[["Close"]], period_df)
        sig["Adj Close"] = price_df["Adj Close"].values

        div_periods_raw = div_data["tickers"][code]["periods"]
        sig["dividend_clean_streak"] = _build_dividend_clean_flag_series(div_periods_raw, sig.index)

        valid = sig.dropna(subset=["range_position_pct"]).copy()
        if valid.empty:
            continue
        adj_close = sig["Adj Close"]
        for years in HORIZONS_YEARS:
            valid[f"fwd_return_{years}y"] = [_forward_return(adj_close, d, years) for d in valid.index]
        valid["code"] = code
        all_rows.append(valid)
        print(f"  [{i + 1}/16] {code} {ticker['name']}: 完了")

    combined = pd.concat(all_rows).sort_index()
    thresholded = combined[combined["range_position_pct"] <= THRESHOLD]
    with_dividend_filter = thresholded[thresholded["dividend_clean_streak"]]
    without_filter_but_dirty = thresholded[~thresholded["dividend_clean_streak"]]

    print(f"\n閾値{THRESHOLD}%以下の該当日数: {len(thresholded)}")
    print(f"  うち配当クリーン(直近5期減配なし): {len(with_dividend_filter)}件"
          f"（{len(with_dividend_filter) / len(thresholded) * 100:.1f}%）")

    result = {"threshold_pct": THRESHOLD, "n_total": int(len(thresholded)), "n_clean": int(len(with_dividend_filter))}

    def _stats(sub, label):
        print(f"\n=== {label}（n={len(sub)}） ===")
        out = {}
        for y in HORIZONS_YEARS:
            s = sub[f"fwd_return_{y}y"].dropna()
            mean_pct = round(float(s.mean()) * 100, 2) if len(s) else None
            out[f"{y}y"] = {"n": int(len(s)), "mean_return_pct": mean_pct}
            print(f"  {y}年後: n={len(s)}  平均={mean_pct}%")
        return out

    result["without_filter"] = _stats(thresholded, "フィルターなし(閾値30%以下すべて)")
    result["with_clean_filter"] = _stats(with_dividend_filter, "配当クリーンのみ(直近5期減配なし)")
    result["dirty_only"] = _stats(without_filter_but_dirty, "参考: 減配歴ありのみ")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n結果を保存: {OUTPUT_PATH}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
