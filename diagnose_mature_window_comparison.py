# -*- coding: utf-8 -*-
"""
【診断専用、読み取り専用の新規スクリプト】

2026-08-17、EDINET書類一覧APIが2016年以前を404で拒否する（実用上の取得限界が2017年）と
判明したことを受け、「EDINET側のデータがまだ蓄積不足だった初期の評価期間」を除外した場合、
超過リターンの縮小(+68.29pt→+7.76pt)がどこまで解消するかを確認する。

評価開始日を段階的に遅らせ(2019/2021/2023)、IRBANK方式・EDINET方式それぞれの
「閾値30%以下」グループの超過リターンがどう変化するかを比較する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from bot.pbr_signal import build_period_records, compute_daily_signal
from edinet_signal_adapter import compute_daily_signal_edinet
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
HORIZONS_YEARS = [1, 3, 5]
FORWARD_DATE_TOLERANCE_DAYS = 10
THRESHOLD = 30
EVAL_START_DATES = ["2019-01-01", "2021-01-01", "2023-01-01"]


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


def _load_irbank_samples() -> pd.DataFrame:
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
            valid[f"fwd_return_{years}y"] = [_forward_return(adj_close, d, years) for d in valid.index]
        valid["code"] = code
        all_rows.append(valid)
    return pd.concat(all_rows).sort_index()


def _load_edinet_samples() -> pd.DataFrame:
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
        adj_close = sig["Adj Close"]
        for years in HORIZONS_YEARS:
            valid[f"fwd_return_{years}y"] = [_forward_return(adj_close, d, years) for d in valid.index]
        valid["code"] = code
        all_rows.append(valid)
    return pd.concat(all_rows).sort_index()


def _mean(series: pd.Series) -> float | None:
    s = series.dropna()
    return round(float(s.mean()) * 100, 2) if len(s) else None


def _concentration(sub: pd.DataFrame, years: int) -> dict:
    """horizon年で実際にフォワードリターンが計算できた行についての集中度診断。"""
    col = f"fwd_return_{years}y"
    valid_rows = sub[sub[col].notna()]
    if valid_rows.empty:
        return {"n": 0, "n_unique_codes": 0, "date_min": None, "date_max": None, "top_code_share_pct": None}
    code_counts = valid_rows["code"].value_counts()
    top_code_share_pct = round(float(code_counts.iloc[0]) / len(valid_rows) * 100, 1)
    return {
        "n": len(valid_rows),
        "n_unique_codes": int(valid_rows["code"].nunique()),
        "date_min": str(valid_rows.index.min().date()),
        "date_max": str(valid_rows.index.max().date()),
        "top_code": str(code_counts.index[0]),
        "top_code_share_pct": top_code_share_pct,
    }


def _report(label: str, combined: pd.DataFrame) -> dict:
    baseline_mean = {y: _mean(combined[f"fwd_return_{y}y"]) for y in HORIZONS_YEARS}
    sub = combined[combined["range_position_pct"] <= THRESHOLD]
    sub_mean = {y: _mean(sub[f"fwd_return_{y}y"]) for y in HORIZONS_YEARS}
    excess = {
        y: (round(sub_mean[y] - baseline_mean[y], 2) if sub_mean[y] is not None and baseline_mean[y] is not None else None)
        for y in HORIZONS_YEARS
    }
    baseline_n = {y: int(combined[f"fwd_return_{y}y"].notna().sum()) for y in HORIZONS_YEARS}
    signal_concentration = {y: _concentration(sub, y) for y in HORIZONS_YEARS}
    print(f"  {label}: n_total={len(combined)} n_signal={len(sub)}")
    for y in HORIZONS_YEARS:
        conc = signal_concentration[y]
        print(f"    {y}年後: ベースライン={baseline_mean[y]}%(n={baseline_n[y]})  閾値30%={sub_mean[y]}%(n={conc['n']})  超過={excess[y]}pt")
        if conc["n"]:
            print(f"      └ signal内訳: 銘柄数={conc['n_unique_codes']} 最多銘柄={conc['top_code']}({conc['top_code_share_pct']}%) 期間={conc['date_min']}〜{conc['date_max']}")
    return {
        "n_total": len(combined),
        "n_signal": len(sub),
        "baseline": baseline_mean,
        "baseline_n": baseline_n,
        "threshold_30": sub_mean,
        "excess": excess,
        "signal_concentration": signal_concentration,
    }


def main() -> int:
    print("IRBANK方式・EDINET方式それぞれのサンプルを計算中...")
    irbank = _load_irbank_samples()
    edinet = _load_edinet_samples()

    result: dict = {}
    for start in EVAL_START_DATES:
        print(f"\n=== 評価開始日: {start} 以降 ===")
        irbank_sub = irbank[irbank.index >= pd.Timestamp(start)]
        edinet_sub = edinet[edinet.index >= pd.Timestamp(start)]
        result[start] = {
            "irbank": _report("IRBANK方式", irbank_sub),
            "edinet": _report("EDINET方式", edinet_sub),
        }

    out_path = DATA_CACHE / "diagnose_mature_window_comparison_result.json"
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)
    print(f"\n結果を保存: {out_path}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
