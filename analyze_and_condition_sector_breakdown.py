# -*- coding: utf-8 -*-
"""
長期株式投資家ペルソナの宿題（2026-08-10、配当利回りフィルター採用時の積み残し）:
配当利回り≥3%フィルターを重ねたAND条件（レンジ位置30%以下 × 利回り3%以上）が、
レンジ位置単体条件（analyze_per_stock_breakdown.py、閾値30%以下のみ）と比べて
金融・通信等の高利回りセクターへの偏りをさらに強めていないかを、銘柄別・セクター別の
内訳で確認する（読み取り専用の分析スクリプト）。

比較対象:
- レンジ位置30%以下単体（analyze_per_stock_breakdown.py の結果、
  data_cache/per_stock_breakdown_result.json）
- 本スクリプト: レンジ30%以下 × 利回り3%以上のAND条件（本番check_signal.pyと同じ基準）

配当利回りの算出ロジック（開示ラグ・異常値ガード込み）はbacktest_dividend_yield_filter.py
のbuild_dividend_yield_series()をそのまま踏襲する（発生源一本化の教訓、tasks/lessons.md参照）。

使い方:
    core16_dividend_botディレクトリで `python analyze_and_condition_sector_breakdown.py` を実行する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from backtest_dividend_yield_filter import (
    RANGE_THRESHOLD_PCT,
    YIELD_THRESHOLD_PCT,
    build_dividend_yield_series,
)
from bot.pbr_signal import build_period_records, compute_daily_signal
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
OUTPUT_PATH = DATA_CACHE / "and_condition_sector_breakdown_result.json"
RANGE_ONLY_BASELINE_PATH = DATA_CACHE / "per_stock_breakdown_result.json"

HORIZONS_YEARS = [3, 5]
FORWARD_DATE_TOLERANCE_DAYS = 10

# analyze_per_stock_breakdown.py と同じ分類（比較可能性のため一致させる。
# 厳密な業種分類ではなく概観目的の大まかな分類）
SECTOR_MAP = {
    "2914": "generic_defensive",  # JT
    "8306": "financial",  # MUFG
    "8316": "financial",  # SMFG
    "8058": "trading_house",  # 三菱商事
    "8031": "trading_house",  # 三井物産
    "8001": "trading_house",  # 伊藤忠
    "9432": "telecom",  # NTT
    "9433": "telecom",  # KDDI
    "8766": "financial",  # 東京海上HD(保険)
    "4503": "pharma",  # アステラス製薬
    "4578": "pharma",  # 大塚HD
    "6301": "machinery",  # コマツ
    "6326": "machinery",  # クボタ
    "8697": "financial",  # 日本取引所G
    "4452": "consumer",  # 花王
    "5108": "consumer",  # ブリヂストン
}


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


def main() -> int:
    with open(DATA_CACHE / "irbank_pbr_range.json", encoding="utf-8") as f:
        pbr_data = json.load(f)
    with open(DATA_CACHE / "irbank_dividend_history.json", encoding="utf-8") as f:
        div_data = json.load(f)

    rows = []
    for ticker in CORE16_UNIVERSE:
        code = ticker["code"]
        price_path = DATA_CACHE / "yfinance_prices" / f"{code}.csv"
        if not price_path.exists():
            continue
        price_df = pd.read_csv(price_path, index_col="Date", parse_dates=True)
        if price_df.index.tz is not None:
            price_df.index = price_df.index.tz_localize(None)
        period_df = build_period_records(pbr_data["tickers"][code]["periods"])
        sig = compute_daily_signal(price_df[["Close"]], period_df)
        sig["Adj Close"] = price_df["Adj Close"].values

        div_periods_raw = div_data["tickers"][code]["periods"]
        sig["dividend_yield_pct"] = build_dividend_yield_series(div_periods_raw, price_df)

        valid = sig.dropna(subset=["range_position_pct"]).copy()
        and_combo = valid[
            (valid["range_position_pct"] <= RANGE_THRESHOLD_PCT)
            & (valid["dividend_yield_pct"] >= YIELD_THRESHOLD_PCT)
        ]
        if and_combo.empty:
            rows.append({"code": code, "name": ticker["name"], "sector": SECTOR_MAP[code], "n": 0})
            continue

        adj_close = sig["Adj Close"]
        row = {"code": code, "name": ticker["name"], "sector": SECTOR_MAP[code], "n": int(len(and_combo))}
        for years in HORIZONS_YEARS:
            fwd = [_forward_return(adj_close, d, years) for d in and_combo.index]
            fwd_s = pd.Series(fwd).dropna()
            row[f"{years}y_mean_return_pct"] = round(float(fwd_s.mean()) * 100, 2) if len(fwd_s) else None
            row[f"{years}y_n"] = int(len(fwd_s))
        rows.append(row)

    result_df = pd.DataFrame(rows).sort_values("n", ascending=False)

    print(f"=== 銘柄別内訳（AND条件: レンジ{RANGE_THRESHOLD_PCT}%以下 × 利回り{YIELD_THRESHOLD_PCT}%以上） ===")
    print(f"{'code':6}{'name':10}{'sector':16}{'n':6}{'3y_mean%':10}{'5y_mean%':10}")
    for _, r in result_df.iterrows():
        print(f"{r['code']:6}{r['name']:10}{r['sector']:16}{r['n']:<6}{r.get('3y_mean_return_pct', ''):<10}{r.get('5y_mean_return_pct', ''):<10}")

    total_n = int(result_df["n"].sum())
    print(f"\n合計シグナル日数: {total_n}")
    if total_n > 0:
        print("上位3銘柄の該当日数シェア:", round(result_df["n"].head(3).sum() / total_n * 100, 1), "%")
        zero_n_stocks = result_df[result_df["n"] == 0]
        print(f"該当ゼロ銘柄数: {len(zero_n_stocks)} / 16", zero_n_stocks["name"].tolist())

    print("\n=== セクター別集計（AND条件） ===")
    sector_agg = result_df.groupby("sector")["n"].sum().sort_values(ascending=False)
    sector_shares = {}
    for sector, n in sector_agg.items():
        share = round(n / total_n * 100, 1) if total_n > 0 else None
        sector_shares[sector] = {"n": int(n), "share_pct": share}
        print(f"  {sector:20}: n={n:6}  シェア={share}%")

    n_negative_5y = int((result_df["5y_mean_return_pct"].dropna() < 0).sum())
    print(f"\n5年後平均リターンがマイナスの銘柄数: {n_negative_5y} / {result_df['5y_mean_return_pct'].notna().sum()}")

    # レンジ位置単体条件（既存結果ファイル）との比較
    comparison = None
    if RANGE_ONLY_BASELINE_PATH.exists():
        with open(RANGE_ONLY_BASELINE_PATH, encoding="utf-8") as f:
            range_only_rows = json.load(f)
        range_only_df = pd.DataFrame(range_only_rows)
        range_only_total = int(range_only_df["n"].sum())
        range_only_sector_agg = range_only_df.groupby("sector")["n"].sum()

        print("\n=== セクターシェア比較（レンジ単体 → AND条件） ===")
        comparison = {}
        all_sectors = sorted(set(range_only_sector_agg.index) | set(sector_agg.index))
        for sector in all_sectors:
            before_n = int(range_only_sector_agg.get(sector, 0))
            before_share = round(before_n / range_only_total * 100, 1) if range_only_total > 0 else None
            after = sector_shares.get(sector, {"n": 0, "share_pct": 0.0 if total_n > 0 else None})
            delta = None
            if before_share is not None and after["share_pct"] is not None:
                delta = round(after["share_pct"] - before_share, 1)
            comparison[sector] = {
                "before_range_only_share_pct": before_share,
                "after_and_combo_share_pct": after["share_pct"],
                "delta_pct_pt": delta,
            }
            print(f"  {sector:20}: {before_share}% → {after['share_pct']}%  (差分 {delta:+}pt)" if delta is not None
                  else f"  {sector:20}: {before_share}% → {after['share_pct']}%")
    else:
        print(f"\n警告: 比較対象の{RANGE_ONLY_BASELINE_PATH}が見つからないため、before/after比較はスキップ"
              "（先にanalyze_per_stock_breakdown.pyを実行してください）")

    output = {
        "range_threshold_pct": RANGE_THRESHOLD_PCT,
        "yield_threshold_pct": YIELD_THRESHOLD_PCT,
        "per_stock": result_df.to_dict("records"),
        "total_n": total_n,
        "sector_breakdown": sector_shares,
        "sector_share_comparison_range_only_vs_and_combo": comparison,
        "n_negative_5y": n_negative_5y,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n結果を保存: {OUTPUT_PATH}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
