# -*- coding: utf-8 -*-
"""
【診断専用、読み取り専用の新規スクリプト】

backtest_threshold_comparison_edinet.py の結果（閾値30%のベースライン超過リターンが
+68.29pt(IRBANK) → +2.41pt(EDINET)に激減）が、EDINET方式そのものの問題なのか、
単にEDINETデータの対象期間がIRBANKより大幅に短く直近の強気相場に偏っているせいなのかを
切り分ける（2026-08-17）。

IRBANK方式(bot.pbr_signal)のサンプルを、EDINET方式のサンプルと同じ日付範囲に絞り込んでから
同じ集計をかけ、「同じ期間で見た場合」に両方式が近い結果になるか確認する。
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
        adj_close = sig["Adj Close"]  # 配当込み総リターン（IRBANK版と条件を揃える）
        for years in HORIZONS_YEARS:
            valid[f"fwd_return_{years}y"] = [_forward_return(adj_close, d, years) for d in valid.index]
        valid["code"] = code
        all_rows.append(valid)
    return pd.concat(all_rows).sort_index()


def _mean(series: pd.Series) -> float | None:
    s = series.dropna()
    return round(float(s.mean()) * 100, 2) if len(s) else None


def _report(label: str, combined: pd.DataFrame) -> dict:
    baseline_mean = {y: _mean(combined[f"fwd_return_{y}y"]) for y in HORIZONS_YEARS}
    sub = combined[combined["range_position_pct"] <= THRESHOLD]
    sub_mean = {y: _mean(sub[f"fwd_return_{y}y"]) for y in HORIZONS_YEARS}
    excess = {
        y: (round(sub_mean[y] - baseline_mean[y], 2) if sub_mean[y] is not None and baseline_mean[y] is not None else None)
        for y in HORIZONS_YEARS
    }
    print(f"\n=== {label} (n_total={len(combined)}, n_signal={len(sub)}, 期間={combined.index.min().date()}〜{combined.index.max().date()}) ===")
    for y in HORIZONS_YEARS:
        print(f"  {y}年後: ベースライン={baseline_mean[y]}%  閾値30%={sub_mean[y]}%  超過={excess[y]}pt")
    return {"n_total": len(combined), "n_signal": len(sub), "baseline": baseline_mean, "threshold_30": sub_mean, "excess": excess,
            "period_start": str(combined.index.min().date()), "period_end": str(combined.index.max().date())}


def main() -> int:
    print("IRBANK方式・EDINET方式それぞれのサンプルを計算中...")
    irbank = _load_irbank_samples()
    edinet = _load_edinet_samples()

    common_start = edinet.index.min()
    common_end = min(irbank.index.max(), edinet.index.max())
    print(f"\nEDINET方式のデータ開始日: {common_start.date()}（この日以降でIRBANK方式も絞り込んで比較する）")

    irbank_restricted = irbank[(irbank.index >= common_start) & (irbank.index <= common_end)]

    result = {
        "irbank_full_period": _report("IRBANK方式・全期間（従来の集計と同じ）", irbank),
        "irbank_restricted_to_edinet_period": _report("IRBANK方式・EDINET方式と同じ期間に絞り込み", irbank_restricted),
        "edinet_full_period": _report("EDINET方式・全期間", edinet),
    }

    out_path = DATA_CACHE / "diagnose_date_range_confound_result.json"
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)
    print(f"\n結果を保存: {out_path}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
