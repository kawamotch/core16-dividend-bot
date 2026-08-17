# -*- coding: utf-8 -*-
"""
2026-08-17、ユーザー提案「最近の日本企業は業績が変わっているから計算窓は10年より5年の方が
良いのでは」の検証。review-panel（フルレビュー）の結論に基づき、以下3候補を比較する。

    A) 現行: 直近10年ローリングウィンドウでのレンジ位置%（bot/pbr_signal.py、変更なし）
    B) 候補: 直近5年ローリングウィンドウでのレンジ位置%（窓の長さだけを5年に短縮）
    C) 候補: 原典（実在のインフルエンサー「長期株式投資さん」@budoukamail、一次情報確認済み）
       の実際の手法をそのまま再現——現在のPBRが「過去5年平均PBR」を下回っていたら買い、
       という単純な絶対基準（レンジの高安ではなく平均との比較である点がA/Bとの本質的な違い。
       会話の中でユーザーから「なぜ本人と同じやり方にしないのか」と指摘され追加した候補）。

review-panelで出た2つの懸念を直接検証する項目を追加する:
    1. CRO指摘: 5年ウィンドウは直近の強気相場だけでレンジが形成され、下限（安値）が
       「本当の安値」でなく「たまたま最近見た中で一番低かった値」になり、レンジが浅く
       （実態より割安判定が甘く）なっていないか → 今日時点のrange_lowをA/Bで直接比較する。
    2. ドメインエキスパート/ファンドマネージャー指摘: 候補が良く見えても、それが直近の
       強気相場だけによるものでないか → ウォークフォワードを2020年より前/以後で分けて
       集計し、優位性が古い期間でも残るかを確認する。

bot/pbr_signal.py（共有モジュール）は変更しない。このスクリプト内に独立した計算関数を持つ
（既存compute_daily_signalと同じロジックの窓長パラメータ化版、および新規の平均比較版）。

使い方:
    core16_dividend_botディレクトリで `python backtest_5y_window_and_original_method.py` を実行する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from bot.pbr_signal import (
    DISCLOSURE_LAG_DAYS,
    MIN_PERIODS_FOR_SIGNAL,
    build_period_records,
    compute_daily_signal,
)
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
OUTPUT_PATH = DATA_CACHE / "5y_window_and_original_method_result.json"

HORIZONS_YEARS = [1, 3, 5]
FORWARD_DATE_TOLERANCE_DAYS = 10
THRESHOLD_PCT = 30  # 本番のレンジ位置%閾値（A・Bで使用）
AVG_METHOD_WINDOW_PERIODS = 5  # 原典の「過去5年平均」
ERA_SPLIT_YEAR = 2020  # ウォークフォワードの年代分割（この年より前/以後）


def compute_daily_signal_window(price_df: pd.DataFrame, period_df: pd.DataFrame, window_periods: int) -> pd.DataFrame:
    """bot.pbr_signal.compute_daily_signal と同じロジックだが、レンジ計算に使う直近期数を
    ROLLING_WINDOW_PERIODS=10固定ではなくwindow_periodsで指定できるようにした版。"""
    df = price_df.copy()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    valid_periods = period_df.dropna(subset=["bps"]).reset_index(drop=True)
    bps_vals: list = [None] * len(df)
    range_high_vals: list = [None] * len(df)
    range_low_vals: list = [None] * len(df)

    for i, date in enumerate(df.index):
        disclosed = valid_periods[valid_periods["disclosure_date"] <= date]
        if len(disclosed) == 0:
            continue
        latest = disclosed.iloc[-1]
        bps_vals[i] = latest["bps"]

        window = disclosed.tail(window_periods)
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


def compute_daily_signal_avg_method(price_df: pd.DataFrame, period_df: pd.DataFrame, trailing_periods: int) -> pd.DataFrame:
    """原典（@budoukamail）の実際の手法を再現: 現在のPBRが「過去trailing_periods期の
    平均PBR」を下回っていたら買い候補、という単純な絶対基準。レンジ(高安)ではなく
    平均との比較である点がA/Bとの本質的な違い。"""
    df = price_df.copy()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    valid_periods = period_df.dropna(subset=["bps"]).reset_index(drop=True)
    bps_vals: list = [None] * len(df)
    avg_pbr_vals: list = [None] * len(df)

    for i, date in enumerate(df.index):
        disclosed = valid_periods[valid_periods["disclosure_date"] <= date]
        if len(disclosed) == 0:
            continue
        latest = disclosed.iloc[-1]
        bps_vals[i] = latest["bps"]

        window = disclosed.tail(trailing_periods)
        if len(window) < min(trailing_periods, MIN_PERIODS_FOR_SIGNAL):
            continue
        mid = ((window["pbr_high"] + window["pbr_low"]) / 2).dropna()
        if len(mid) == 0:
            continue
        avg_pbr_vals[i] = mid.mean()

    df["bps"] = bps_vals
    df["current_pbr"] = df["Close"] / df["bps"]
    df["avg_pbr"] = avg_pbr_vals
    df["avg_deviation_pct"] = (df["current_pbr"] - df["avg_pbr"]) / df["avg_pbr"] * 100
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


def _load_all_samples() -> tuple[pd.DataFrame, list[dict]]:
    with open(DATA_CACHE / "irbank_pbr_range.json", encoding="utf-8") as f:
        pbr_data = json.load(f)

    all_rows = []
    today_snapshot = []
    print("全銘柄で 10年レンジ(A) / 5年レンジ(B) / 5年平均比較(C・原典再現) を計算中...")
    for i, ticker in enumerate(CORE16_UNIVERSE):
        code = ticker["code"]
        price_path = DATA_CACHE / "yfinance_prices" / f"{code}.csv"
        if not price_path.exists():
            continue
        price_df = pd.read_csv(price_path, index_col="Date", parse_dates=True)
        if price_df.index.tz is not None:
            price_df.index = price_df.index.tz_localize(None)
        period_df = build_period_records(pbr_data["tickers"][code]["periods"])

        sig_a = compute_daily_signal(price_df[["Close"]], period_df)
        sig_b = compute_daily_signal_window(price_df[["Close"]], period_df, window_periods=5)
        sig_c = compute_daily_signal_avg_method(price_df[["Close"]], period_df, trailing_periods=AVG_METHOD_WINDOW_PERIODS)

        combined = pd.DataFrame(index=price_df.index)
        combined["range_position_pct_A_10y"] = sig_a["range_position_pct"]
        combined["range_position_pct_B_5y"] = sig_b["range_position_pct"]
        combined["avg_deviation_pct_C"] = sig_c["avg_deviation_pct"]
        combined["range_low_A_10y"] = sig_a["range_low"]
        combined["range_low_B_5y"] = sig_b["range_low"]
        combined["Adj Close"] = price_df["Adj Close"].values

        valid = combined.dropna(
            subset=["range_position_pct_A_10y", "range_position_pct_B_5y", "avg_deviation_pct_C"]
        ).copy()
        if valid.empty:
            continue
        adj_close = combined["Adj Close"]
        for years in HORIZONS_YEARS:
            valid[f"fwd_return_{years}y"] = [_forward_return(adj_close, d, years) for d in valid.index]
        valid["code"] = code
        all_rows.append(valid)

        last = valid.iloc[-1]
        today_snapshot.append({
            "code": code,
            "name": ticker["name"],
            "range_A_10y_pct": round(float(last["range_position_pct_A_10y"]), 1),
            "range_B_5y_pct": round(float(last["range_position_pct_B_5y"]), 1),
            "avg_deviation_C_pct": round(float(last["avg_deviation_pct_C"]), 1),
            "range_low_A_10y": round(float(last["range_low_A_10y"]), 2),
            "range_low_B_5y": round(float(last["range_low_B_5y"]), 2),
            "range_low_shallower_pct": round(
                (float(last["range_low_B_5y"]) / float(last["range_low_A_10y"]) - 1) * 100, 1
            ),
            "signal_A": bool(last["range_position_pct_A_10y"] <= THRESHOLD_PCT),
            "signal_B": bool(last["range_position_pct_B_5y"] <= THRESHOLD_PCT),
            "signal_C": bool(last["avg_deviation_pct_C"] <= 0),
        })
        print(
            f"  [{i + 1}/16] {code} {ticker['name']}: "
            f"A(10y)={today_snapshot[-1]['range_A_10y_pct']}%  B(5y)={today_snapshot[-1]['range_B_5y_pct']}%  "
            f"C(平均比)={today_snapshot[-1]['avg_deviation_C_pct']:+.1f}%  "
            f"レンジ安値の浅さ(B/A-1)={today_snapshot[-1]['range_low_shallower_pct']:+.1f}%"
        )

    return pd.concat(all_rows).sort_index(), today_snapshot


def _stats(sub: pd.DataFrame, label: str) -> dict:
    print(f"\n=== {label}（n={len(sub)}） ===")
    out: dict = {"n": int(len(sub))}
    for y in HORIZONS_YEARS:
        s = sub[f"fwd_return_{y}y"].dropna()
        mean_pct = round(float(s.mean()) * 100, 2) if len(s) else None
        median_pct = round(float(s.median()) * 100, 2) if len(s) else None
        win_rate = round(float((s > 0).mean()) * 100, 1) if len(s) else None
        out[f"{y}y"] = {"n": int(len(s)), "mean_return_pct": mean_pct, "median_return_pct": median_pct, "win_rate_pct": win_rate}
        print(f"  {y}年後: n={len(s):6}  平均={mean_pct}%  中央値={median_pct}%  勝率={win_rate}%")
    return out


def _era_split(combined: pd.DataFrame, flag: pd.Series, label: str) -> dict:
    print(f"\n--- {label}: 年代別（{ERA_SPLIT_YEAR}年より前 / 以後）---")
    group = combined[flag]
    out = {}
    for era_label, era_mask in [
        (f"before_{ERA_SPLIT_YEAR}", group.index.year < ERA_SPLIT_YEAR),
        (f"from_{ERA_SPLIT_YEAR}", group.index.year >= ERA_SPLIT_YEAR),
    ]:
        era_sub = group[era_mask]
        row = {"n": int(len(era_sub))}
        for y in HORIZONS_YEARS:
            s = era_sub[f"fwd_return_{y}y"].dropna()
            row[f"{y}y_mean_pct"] = round(float(s.mean()) * 100, 2) if len(s) else None
        out[era_label] = row
        print(f"  {era_label}: n={row['n']}  1y={row.get('1y_mean_pct')}%  3y={row.get('3y_mean_pct')}%  5y={row.get('5y_mean_pct')}%")
    return out


def main() -> int:
    combined, today_snapshot = _load_all_samples()
    print(f"\n総サンプル数（3方式全て有効な日のみ）: {len(combined)}")

    result: dict = {
        "threshold_pct": THRESHOLD_PCT,
        "avg_method_window_periods": AVG_METHOD_WINDOW_PERIODS,
        "era_split_year": ERA_SPLIT_YEAR,
        "today_snapshot": today_snapshot,
    }

    baseline_mean = {y: round(float(combined[f"fwd_return_{y}y"].dropna().mean()) * 100, 2) for y in HORIZONS_YEARS}
    result["baseline_all_days_mean_return_pct"] = baseline_mean
    print(f"\nベースライン（シグナル無視で全日均等）: {baseline_mean}")

    flag_a = combined["range_position_pct_A_10y"] <= THRESHOLD_PCT
    flag_b = combined["range_position_pct_B_5y"] <= THRESHOLD_PCT
    flag_c = combined["avg_deviation_pct_C"] <= 0

    result["A_10y_range"] = _stats(combined[flag_a], "A) 現行: 10年レンジ位置 <= 30%")
    result["B_5y_range"] = _stats(combined[flag_b], "B) 候補: 5年レンジ位置 <= 30%")
    result["C_5y_avg_original"] = _stats(combined[flag_c], "C) 候補: 5年平均比較（原典再現、平均を下回ったら買い）")

    print("\n=== CRO懸念の直接検証: 5年ウィンドウのレンジ安値は10年ウィンドウより浅いか ===")
    shallower_pcts = [row["range_low_shallower_pct"] for row in today_snapshot]
    result["range_low_shallower_pct_stats"] = {
        "mean": round(sum(shallower_pcts) / len(shallower_pcts), 1),
        "n_shallower_positive": sum(1 for v in shallower_pcts if v > 0),
        "n_total": len(shallower_pcts),
        "detail": today_snapshot,
    }
    print(
        f"  16銘柄中 {result['range_low_shallower_pct_stats']['n_shallower_positive']}銘柄で "
        f"5年ウィンドウの安値が10年ウィンドウより高い(=浅い)。平均差={result['range_low_shallower_pct_stats']['mean']:+.1f}%"
    )

    result["era_split"] = {
        "A_10y_range": _era_split(combined, flag_a, "A) 10年レンジ"),
        "B_5y_range": _era_split(combined, flag_b, "B) 5年レンジ"),
        "C_5y_avg_original": _era_split(combined, flag_c, "C) 5年平均比較"),
    }

    print("\n=== 今日時点の3方式のシグナル一致状況 ===")
    agree_all = sum(1 for r in today_snapshot if r["signal_A"] == r["signal_B"] == r["signal_C"])
    result["today_agreement_all_3"] = agree_all
    print(f"  16銘柄中{agree_all}銘柄で3方式とも判定一致")
    for r in today_snapshot:
        if not (r["signal_A"] == r["signal_B"] == r["signal_C"]):
            print(f"    不一致: {r['code']} {r['name']}  A={r['signal_A']}  B={r['signal_B']}  C={r['signal_C']}")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = OUTPUT_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(OUTPUT_PATH)
    print(f"\n結果を保存: {OUTPUT_PATH}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
