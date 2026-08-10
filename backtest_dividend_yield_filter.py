# -*- coding: utf-8 -*-
"""
長期株式投資さんの原典が必須条件とする「配当利回り≥3%」を、現行のレンジ位置閾値30%と
組み合わせて検証する（2026-08-10、フルレビュー承認済み）。過去の「配当クリーンフィルター」
（減配歴の有無、backtest_dividend_quality_filter.py）とは別物で、利回りの水準そのものを
初めて検証する。

標準手法（tasks/lessons.md「分析の標準手法」）に従い5段階で実施:
1. 単独貢献度: 利回り≥3%単体（レンジ位置は問わない）のフォワードリターン
2. 条件剥ぎ取り: レンジ位置30%×利回り3%のAND条件 vs 各単体条件
3. 交互作用確認: AND条件が両単体条件より優れているか
4. ランダムベースライン: AND条件と同じ該当日数をランダム抽出した場合との比較（20試行平均）
5. ウォークフォワードOOS: 暦年ウィンドウでAND条件 vs レンジ位置30%単体（現行採用中の基準）の勝敗集計
   （backtest_walkforward_threshold.pyと同じ「同一母集団内比較」の方法論）

配当利回りの算出: yield(t) = 直近開示済みの年間配当(dps_adjusted, 分割調整済み) ÷ Close(t) × 100
開示ラグは既存のDIVIDEND_DISCLOSURE_LAG_DAYS(45日)をbacktest_dividend_quality_filter.pyから
踏襲し、先読みバイアスを回避する（未来の配当実績を過去に遡って使わない）。

使い方:
    core16_dividend_botディレクトリで `python backtest_dividend_yield_filter.py` を実行する。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pandas as pd

from bot.pbr_signal import build_period_records, compute_daily_signal
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
OUTPUT_PATH = DATA_CACHE / "dividend_yield_filter_result.json"

HORIZONS_YEARS = [1, 3, 5]
FORWARD_DATE_TOLERANCE_DAYS = 10
RANGE_THRESHOLD_PCT = 30
YIELD_THRESHOLD_PCT = 3.0  # 長期株式投資さん原典の目安（配当利回り3%以上）
DIVIDEND_DISCLOSURE_LAG_DAYS = 45  # bot/pbr_signal.DISCLOSURE_LAG_DAYSと同じ保守的仮定
RANDOM_BASELINE_TRIALS = 20
RANDOM_SEED = 42

# ウォークフォワード用の暦年ウィンドウ（backtest_walkforward_threshold.pyと同じ非重複ウィンドウ方式）
WALKFORWARD_YEARS = list(range(2011, 2024))  # 5年後リターンが計算可能な範囲を考慮し2023年までに限定


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


def build_dividend_yield_series(div_periods_raw: list[dict], price_df: pd.DataFrame) -> pd.Series:
    """各日の配当利回り(%)を返す（先読み回避のため開示ラグ付きffill、Closeは分割調整済み・
    配当未調整の終値を使う。dps_adjustedも分割調整済みのため単位が整合する）。

    配当データが無い/ゼロ配当の期間はNaN（0%として扱わない。「無配のためフィルターに
    通らない」ことと「データ欠損で判定不能」を区別する設計上の理由は、business上は
    どちらも「利回り基準を満たさない」で同じ扱いになるためNaNのままでも実用上問題ないが、
    QA向けに明示的に区別できるようNaNを採用する）。
    """
    raw_df = pd.DataFrame(div_periods_raw)
    if raw_df.empty or "dps_adjusted" not in raw_df.columns:
        return pd.Series(float("nan"), index=price_df.index)
    df = raw_df.dropna(subset=["dps_adjusted"]).copy()
    if df.empty:
        return pd.Series(float("nan"), index=price_df.index)
    df["end_date"] = pd.to_datetime(df["end_date"])
    df = df.sort_values("end_date").reset_index(drop=True)
    df["disclosure_date"] = df["end_date"] + pd.Timedelta(days=DIVIDEND_DISCLOSURE_LAG_DAYS)

    dps_vals = []
    for date in price_df.index:
        disclosed = df[df["disclosure_date"] <= date]
        dps_vals.append(disclosed.iloc[-1]["dps_adjusted"] if len(disclosed) else None)

    dps_series = pd.Series(dps_vals, index=price_df.index, dtype="float64")
    yield_pct = dps_series / price_df["Close"] * 100
    return yield_pct


def _load_all_samples() -> pd.DataFrame:
    with open(DATA_CACHE / "irbank_pbr_range.json", encoding="utf-8") as f:
        pbr_data = json.load(f)
    with open(DATA_CACHE / "irbank_dividend_history.json", encoding="utf-8") as f:
        div_data = json.load(f)

    all_rows = []
    print("全銘柄のレンジ位置・配当利回り・フォワードリターンを計算中...")
    for i, ticker in enumerate(CORE16_UNIVERSE):
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
        if valid.empty:
            continue
        adj_close = sig["Adj Close"]
        for years in HORIZONS_YEARS:
            valid[f"fwd_return_{years}y"] = [_forward_return(adj_close, d, years) for d in valid.index]
        valid["code"] = code
        all_rows.append(valid)
        print(f"  [{i + 1}/16] {code} {ticker['name']}: 完了（有効利回りデータ {valid['dividend_yield_pct'].notna().sum()}日）")

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


def _random_baseline(combined: pd.DataFrame, n_target: int, trials: int = RANDOM_BASELINE_TRIALS) -> dict:
    rng = random.Random(RANDOM_SEED)
    print(f"\n=== ランダムベースライン(n={n_target}を{trials}回抽出・平均) ===")
    out: dict = {"n": n_target, "trials": trials}
    pool = combined.reset_index(drop=True)
    if n_target <= 0 or n_target > len(pool):
        print("  該当日数が不正なためスキップ")
        return out
    for y in HORIZONS_YEARS:
        means = []
        for _ in range(trials):
            idx = rng.sample(range(len(pool)), n_target)
            s = pool.iloc[idx][f"fwd_return_{y}y"].dropna()
            if len(s):
                means.append(float(s.mean()))
        avg_of_means = round(sum(means) / len(means) * 100, 2) if means else None
        out[f"{y}y"] = {"mean_return_pct_avg": avg_of_means}
        print(f"  {y}年後: 平均リターンの{trials}試行平均={avg_of_means}%")
    return out


def _walkforward(combined: pd.DataFrame) -> dict:
    """暦年ウィンドウで「AND条件(レンジ30%×利回り3%)」vs「レンジ30%単体(現行基準)」の
    プールド平均リターンを比較し、勝敗を集計する（backtest_walkforward_threshold.pyと同じ
    同一母集団内比較の方法論）。"""
    print("\n=== ウォークフォワードOOS（暦年ウィンドウ、AND条件 vs レンジ30%単体） ===")
    range_only = combined[combined["range_position_pct"] <= RANGE_THRESHOLD_PCT]
    and_combo = range_only[range_only["dividend_yield_pct"] >= YIELD_THRESHOLD_PCT]

    results = {}
    for y in HORIZONS_YEARS:
        wins = 0
        total_windows = 0
        window_detail = []
        for year in WALKFORWARD_YEARS:
            win_start = pd.Timestamp(f"{year}-01-01")
            win_end = pd.Timestamp(f"{year}-12-31")
            base_sub = range_only[(range_only.index >= win_start) & (range_only.index <= win_end)][f"fwd_return_{y}y"].dropna()
            and_sub = and_combo[(and_combo.index >= win_start) & (and_combo.index <= win_end)][f"fwd_return_{y}y"].dropna()
            if len(base_sub) < 5 or len(and_sub) < 5:
                continue  # サンプル数僅少ウィンドウはPF極端化を避けるため除外
            base_mean = float(base_sub.mean())
            and_mean = float(and_sub.mean())
            total_windows += 1
            win = and_mean > base_mean
            if win:
                wins += 1
            window_detail.append(
                {"year": year, "n_base": int(len(base_sub)), "n_and": int(len(and_sub)),
                 "base_mean_pct": round(base_mean * 100, 2), "and_mean_pct": round(and_mean * 100, 2), "and_wins": win}
            )
        print(f"  {y}年後ホライズン: AND条件が上回ったウィンドウ {wins}/{total_windows}")
        results[f"{y}y"] = {"wins": wins, "total_windows": total_windows, "windows": window_detail}
    return results


def main() -> int:
    combined = _load_all_samples()
    print(f"\n総サンプル数: {len(combined)}")

    result: dict = {
        "range_threshold_pct": RANGE_THRESHOLD_PCT,
        "yield_threshold_pct": YIELD_THRESHOLD_PCT,
    }

    baseline_all = combined
    yield_only = combined[combined["dividend_yield_pct"] >= YIELD_THRESHOLD_PCT]
    range_only = combined[combined["range_position_pct"] <= RANGE_THRESHOLD_PCT]
    and_combo = range_only[range_only["dividend_yield_pct"] >= YIELD_THRESHOLD_PCT]

    result["baseline_all_days"] = _stats(baseline_all, "ベースライン(全シグナル日、フィルターなし)")
    result["yield_only"] = _stats(yield_only, f"①単独貢献度: 利回り{YIELD_THRESHOLD_PCT}%以上単体")
    result["range_only_current"] = _stats(range_only, f"②現行基準: レンジ位置{RANGE_THRESHOLD_PCT}%以下単体")
    result["and_combo"] = _stats(and_combo, f"②AND条件: レンジ{RANGE_THRESHOLD_PCT}%以下 × 利回り{YIELD_THRESHOLD_PCT}%以上")

    result["random_baseline_matched_to_and_combo"] = _random_baseline(combined, len(and_combo))
    result["walkforward"] = _walkforward(combined)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n結果を保存: {OUTPUT_PATH}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
