# -*- coding: utf-8 -*-
"""
長期株式投資家ペルソナ指摘の確認（2026-08-07パネルレビュー）:
①バリュートラップ: 特定の1〜2銘柄だけが閾値30%以下戦略の好成績を牽引していないか
②セクター集中: 閾値30%以下のシグナルが特定セクターに偏っていないか
を、銘柄別・セクター別の内訳で確認する（読み取り専用の分析スクリプト）。

使い方:
    core16_dividend_botディレクトリで `python analyze_per_stock_breakdown.py` を実行する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from bot.pbr_signal import build_period_records, compute_daily_signal
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
OUTPUT_PATH = DATA_CACHE / "per_stock_breakdown_result.json"

HORIZONS_YEARS = [3, 5]
FORWARD_DATE_TOLERANCE_DAYS = 10
THRESHOLD = 30

# 大まかなセクター分類（長期株式投資家指摘のセクター集中確認用。厳密な業種分類ではなく概観目的）
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

    rows = []
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
        thresh = valid[valid["range_position_pct"] <= THRESHOLD]
        if thresh.empty:
            rows.append({"code": code, "name": ticker["name"], "sector": SECTOR_MAP[code], "n": 0})
            continue

        adj_close = sig["Adj Close"]
        row = {"code": code, "name": ticker["name"], "sector": SECTOR_MAP[code], "n": int(len(thresh))}
        for years in HORIZONS_YEARS:
            fwd = [_forward_return(adj_close, d, years) for d in thresh.index]
            fwd_s = pd.Series(fwd).dropna()
            row[f"{years}y_mean_return_pct"] = round(float(fwd_s.mean()) * 100, 2) if len(fwd_s) else None
            row[f"{years}y_n"] = int(len(fwd_s))
        rows.append(row)

    result_df = pd.DataFrame(rows).sort_values("n", ascending=False)

    print("=== 銘柄別内訳（閾値30%以下） ===")
    print(f"{'code':6}{'name':10}{'sector':16}{'n':6}{'3y_mean%':10}{'5y_mean%':10}")
    for _, r in result_df.iterrows():
        print(f"{r['code']:6}{r['name']:10}{r['sector']:16}{r['n']:<6}{r.get('3y_mean_return_pct', ''):<10}{r.get('5y_mean_return_pct', ''):<10}")

    total_n = result_df["n"].sum()
    print(f"\n合計シグナル日数: {total_n}")
    print("上位3銘柄の該当日数シェア:", round(result_df["n"].head(3).sum() / total_n * 100, 1), "%")

    print("\n=== セクター別集計 ===")
    sector_agg = result_df.groupby("sector")["n"].sum().sort_values(ascending=False)
    for sector, n in sector_agg.items():
        print(f"  {sector:20}: n={n:6}  シェア={round(n / total_n * 100, 1)}%")

    n_negative_5y = int((result_df["5y_mean_return_pct"].dropna() < 0).sum())
    print(f"\n5年後平均リターンがマイナスの銘柄数: {n_negative_5y} / {result_df['5y_mean_return_pct'].notna().sum()}")
    if n_negative_5y > 0:
        print("該当銘柄:", result_df[result_df["5y_mean_return_pct"] < 0][["code", "name", "5y_mean_return_pct"]].to_dict("records"))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    result_df.to_json(OUTPUT_PATH, orient="records", force_ascii=False, indent=2)
    print(f"\n結果を保存: {OUTPUT_PATH}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
