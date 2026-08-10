# -*- coding: utf-8 -*-
"""
PBRレンジ位置の計算窓（現行: 直近10年のローリングウィンドウ）は短すぎるのではという
ユーザー指摘の検証（2026-08-10、review-panel進行中に着手）。

追加のデータ取得なしで検証できる範囲として、「拡張ウィンドウ（開示済みの全期間を使う、
tail(10)で切り詰めない）」を現行のローリング10年と比較する。bot/pbr_signal.py
（共有モジュール）は変更せず、この検証スクリプト内に独立した計算関数を持つ
（既存のcompute_daily_signalと同じロジックだが、window=disclosed.tail(N)の代わりに
window=disclosed（全期間）を使う点だけが異なる）。

手計算での予備確認（NTT）: PBRが構造的に切り上がり続けている銘柄（2010年代前半0.5倍台→
現在1.5倍台）では、窓を延ばして古い安値を含めると現在のレンジ位置(%)はむしろ悪化
（より「割高」寄りに）する可能性が高いと分かった。これを全16銘柄で系統的に検証する。

使い方:
    core16_dividend_botディレクトリで `python backtest_range_window_length.py` を実行する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from bot.pbr_signal import build_period_records, DISCLOSURE_LAG_DAYS, MIN_PERIODS_FOR_SIGNAL
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
OUTPUT_PATH = DATA_CACHE / "range_window_length_result.json"

HORIZONS_YEARS = [1, 3, 5]
FORWARD_DATE_TOLERANCE_DAYS = 10
THRESHOLD_PCT = 30
WALKFORWARD_YEARS = list(range(2011, 2024))


def compute_daily_signal_expanding(price_df: pd.DataFrame, period_df: pd.DataFrame) -> pd.DataFrame:
    """bot/pbr_signal.compute_daily_signal()と同じロジックだが、レンジ計算に使う期間を
    「直近10期」ではなく「開示済みの全期間（拡張ウィンドウ）」にする。共有モジュールは変更せず、
    このスクリプト内に独立した実装を持つ（既存のROLLING_WINDOW_PERIODS=10による.tail(10)を
    外しただけの差分）。"""
    df = price_df.copy()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    valid_periods = period_df.dropna(subset=["bps"]).reset_index(drop=True)

    bps_vals = [None] * len(df)
    range_high_vals = [None] * len(df)
    range_low_vals = [None] * len(df)

    for i, date in enumerate(df.index):
        disclosed = valid_periods[valid_periods["disclosure_date"] <= date]
        if len(disclosed) == 0:
            continue
        latest = disclosed.iloc[-1]
        bps_vals[i] = latest["bps"]

        window = disclosed  # ここが唯一の差分（tail(10)しない＝拡張ウィンドウ）
        if len(window) < MIN_PERIODS_FOR_SIGNAL:
            continue
        highs = window["pbr_high"].dropna()
        lows = window["pbr_low"].dropna()
        if len(highs) == 0 or len(lows) == 0:
            continue
        range_high_vals[i] = highs.max()
        range_low_vals[i] = lows.min()

    df["bps"] = bps_vals
    df["current_pbr"] = df["Close"] / df["bps"]
    df["range_high"] = range_high_vals
    df["range_low"] = range_low_vals

    span = df["range_high"] - df["range_low"]
    range_position_pct = (df["current_pbr"] - df["range_low"]) / span * 100
    range_position_pct[span <= 0] = None
    df["range_position_pct"] = range_position_pct

    return df


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
    print("全銘柄で現行(10年ローリング) vs 拡張ウィンドウ(全期間)のレンジ位置を計算中...")
    for i, ticker in enumerate(CORE16_UNIVERSE):
        code = ticker["code"]
        price_path = DATA_CACHE / "yfinance_prices" / f"{code}.csv"
        if not price_path.exists():
            continue
        price_df = pd.read_csv(price_path, index_col="Date", parse_dates=True)
        if price_df.index.tz is not None:
            price_df.index = price_df.index.tz_localize(None)
        period_df = build_period_records(pbr_data["tickers"][code]["periods"])

        from bot.pbr_signal import compute_daily_signal
        sig_current = compute_daily_signal(price_df[["Close"]], period_df)
        sig_expanding = compute_daily_signal_expanding(price_df[["Close"]], period_df)

        combined = pd.DataFrame(index=price_df.index)
        combined["range_position_pct_10y"] = sig_current["range_position_pct"]
        combined["range_position_pct_expanding"] = sig_expanding["range_position_pct"]
        combined["Adj Close"] = price_df["Adj Close"].values

        valid = combined.dropna(subset=["range_position_pct_10y", "range_position_pct_expanding"]).copy()
        if valid.empty:
            continue
        adj_close = combined["Adj Close"]
        for years in HORIZONS_YEARS:
            valid[f"fwd_return_{years}y"] = [_forward_return(adj_close, d, years) for d in valid.index]
        valid["code"] = code

        # 今日時点(最終行)のレンジ位置を比較用に記録
        last_10y = valid["range_position_pct_10y"].iloc[-1]
        last_exp = valid["range_position_pct_expanding"].iloc[-1]
        all_rows.append(valid)
        print(f"  [{i + 1}/16] {code} {ticker['name']}: 今日のレンジ位置 10年={last_10y:.1f}%  拡張={last_exp:.1f}%  差={last_exp - last_10y:+.1f}pt")

    return pd.concat(all_rows).sort_index()


def _stats(sub: pd.DataFrame, label: str) -> dict:
    print(f"\n=== {label}（n={len(sub)}） ===")
    out: dict = {"n": int(len(sub))}
    for y in HORIZONS_YEARS:
        s = sub[f"fwd_return_{y}y"].dropna()
        mean_pct = round(float(s.mean()) * 100, 2) if len(s) else None
        win_rate = round(float((s > 0).mean()) * 100, 1) if len(s) else None
        out[f"{y}y"] = {"n": int(len(s)), "mean_return_pct": mean_pct, "win_rate_pct": win_rate}
        print(f"  {y}年後: n={len(s):6}  平均={mean_pct}%  勝率={win_rate}%")
    return out


def _walkforward(combined: pd.DataFrame) -> dict:
    print("\n=== ウォークフォワードOOS（暦年ウィンドウ、拡張ウィンドウ閾値30% vs 現行10年閾値30%） ===")
    current_flag = combined["range_position_pct_10y"] <= THRESHOLD_PCT
    expanding_flag = combined["range_position_pct_expanding"] <= THRESHOLD_PCT
    current_group = combined[current_flag]
    expanding_group = combined[expanding_flag]

    results = {}
    for y in HORIZONS_YEARS:
        wins = 0
        total_windows = 0
        window_detail = []
        for year in WALKFORWARD_YEARS:
            win_start = pd.Timestamp(f"{year}-01-01")
            win_end = pd.Timestamp(f"{year}-12-31")
            base_sub = current_group[(current_group.index >= win_start) & (current_group.index <= win_end)][f"fwd_return_{y}y"].dropna()
            cand_sub = expanding_group[(expanding_group.index >= win_start) & (expanding_group.index <= win_end)][f"fwd_return_{y}y"].dropna()
            if len(base_sub) < 5 or len(cand_sub) < 5:
                continue
            base_mean = float(base_sub.mean())
            cand_mean = float(cand_sub.mean())
            total_windows += 1
            win = cand_mean > base_mean
            if win:
                wins += 1
            window_detail.append(
                {"year": year, "n_base": int(len(base_sub)), "n_cand": int(len(cand_sub)),
                 "base_mean_pct": round(base_mean * 100, 2), "cand_mean_pct": round(cand_mean * 100, 2), "cand_wins": win}
            )
        print(f"  {y}年後ホライズン: 拡張ウィンドウが上回ったウィンドウ {wins}/{total_windows}")
        results[f"{y}y"] = {"wins": wins, "total_windows": total_windows, "windows": window_detail}
    return results


def main() -> int:
    combined = _load_all_samples()
    print(f"\n総サンプル数: {len(combined)}")

    result: dict = {"threshold_pct": THRESHOLD_PCT}

    current_flag = combined["range_position_pct_10y"] <= THRESHOLD_PCT
    expanding_flag = combined["range_position_pct_expanding"] <= THRESHOLD_PCT

    result["current_10y_rolling"] = _stats(combined[current_flag], "現行: レンジ位置(10年ローリング)30%以下")
    result["expanding_all_history"] = _stats(combined[expanding_flag], "候補: レンジ位置(拡張ウィンドウ・全期間)30%以下")

    # 今日時点で両者の判定が食い違う銘柄（現行では拾えないが拡張では拾える、またはその逆）
    both_valid = combined.dropna(subset=["range_position_pct_10y", "range_position_pct_expanding"])
    last_per_stock = both_valid.groupby("code").tail(1)
    disagreement = last_per_stock[
        (last_per_stock["range_position_pct_10y"] <= THRESHOLD_PCT) != (last_per_stock["range_position_pct_expanding"] <= THRESHOLD_PCT)
    ]
    result["today_disagreement_stocks"] = [
        {
            "code": row["code"],
            "range_10y": round(float(row["range_position_pct_10y"]), 1),
            "range_expanding": round(float(row["range_position_pct_expanding"]), 1),
        }
        for _, row in disagreement.iterrows()
    ]
    print(f"\n今日時点で現行/拡張ウィンドウの判定が食い違う銘柄: {len(disagreement)}件")
    for item in result["today_disagreement_stocks"]:
        print(f"  {item['code']}: 10年={item['range_10y']}%  拡張={item['range_expanding']}%")

    result["walkforward"] = _walkforward(combined)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n結果を保存: {OUTPUT_PATH}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
