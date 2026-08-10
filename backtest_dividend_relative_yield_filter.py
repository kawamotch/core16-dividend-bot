# -*- coding: utf-8 -*-
"""
配当利回りフィルターを「銘柄自身の過去水準との相対比較」に拡張して検証する
（2026-08-10、フルレビュー承認済み）。

背景（ユーザー指摘）: 現行のbacktest_dividend_yield_filter.py（固定閾値3%以上）は、
銘柄によって「正常な利回り水準」が異なることを考慮していない。NTTのように元々利回りが
低い体質の銘柄は絶対値3%に届きにくい可能性があり、「その銘柄にしては利回りが高め」という
相対的な目線こそ長期株式投資さんの本来の考え方に近いのではないか、という指摘を検証する。

先行事例（重要な参考）: PBR側では2026-08-07に同種の発想「適応的パーセンタイル閾値」を
検証済みで、これは優位性を大きく毀損し不採用となった（3年後超過リターン+23.1pt→-8.0pt）。
配当利回りは配当政策（会社の増配・維持判断）を反映する指標でありPBR（市場のセンチメント）
とは性質が異なるため、同じ結果になるとは限らないというドメインエキスパートの見立てを検証する。

相対基準の定義: yield(t) ≥ trailing_avg_yield(t)（その日までの過去5年間の配当利回りの
単純移動平均）。先読み回避のため、rolling()は過去方向のみを見る（未来のデータを含まない）。
最低でも2年分の観測が無い期間はNone（判定不能）とし、Falseと混同しない。

検証パターン（標準5点セットに準拠、tasks/lessons.md「分析の標準手法」）:
- 絶対基準単体（既存、参考として再掲）
- 相対基準単体（①単独貢献度）
- 絶対×相対AND（②組み合わせ、ドメインエキスパート提案の二段構え）
- 現行の本番採用基準（レンジ30%×絶対利回り3%）との比較（④ランダムベースライン相当の基準比較）
- レンジ30%×絶対3%×相対基準の三重AND
- ウォークフォワードOOS: 三重AND vs 現行本番基準（レンジ30%×絶対3%）

使い方:
    core16_dividend_botディレクトリで `python backtest_dividend_relative_yield_filter.py` を実行する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from bot.pbr_signal import build_period_records, compute_daily_signal
from backtest_dividend_yield_filter import build_dividend_yield_series, YIELD_THRESHOLD_PCT
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
OUTPUT_PATH = DATA_CACHE / "dividend_relative_yield_filter_result.json"

HORIZONS_YEARS = [1, 3, 5]
FORWARD_DATE_TOLERANCE_DAYS = 10
RANGE_THRESHOLD_PCT = 30
ROLLING_YEARS = 5
ROLLING_WINDOW_DAYS = ROLLING_YEARS * 252  # 5年分の営業日換算
MIN_PERIODS_FOR_RELATIVE = 2 * 252  # 最低2年分の利回り観測が無い期間は判定不能(None)扱い
WALKFORWARD_YEARS = list(range(2011, 2024))


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


def build_relative_yield_flag(yield_pct: pd.Series) -> pd.Series:
    """各日について、配当利回りが「その銘柄自身の過去5年平均」以上かを判定する。
    先読み回避: pandas.rolling()はデフォルトで過去方向のみを見る（当日を含む過去window日）ため、
    追加のshiftは不要（当日時点で確定済みの過去のみを使う）。観測不足の期間はNone。
    """
    trailing_avg = yield_pct.rolling(window=ROLLING_WINDOW_DAYS, min_periods=MIN_PERIODS_FOR_RELATIVE).mean()
    result = []
    for y, avg in zip(yield_pct, trailing_avg):
        if pd.isna(y) or pd.isna(avg):
            result.append(None)
        else:
            result.append(bool(y >= avg))
    return pd.Series(result, index=yield_pct.index)


def _load_all_samples() -> pd.DataFrame:
    with open(DATA_CACHE / "irbank_pbr_range.json", encoding="utf-8") as f:
        pbr_data = json.load(f)
    with open(DATA_CACHE / "irbank_dividend_history.json", encoding="utf-8") as f:
        div_data = json.load(f)

    all_rows = []
    print("全銘柄のレンジ位置・配当利回り(絶対/相対)・フォワードリターンを計算中...")
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
        sig["yield_above_own_5y_avg"] = build_relative_yield_flag(sig["dividend_yield_pct"])

        valid = sig.dropna(subset=["range_position_pct"]).copy()
        if valid.empty:
            continue
        adj_close = sig["Adj Close"]
        for years in HORIZONS_YEARS:
            valid[f"fwd_return_{years}y"] = [_forward_return(adj_close, d, years) for d in valid.index]
        valid["code"] = code
        n_relative_valid = (valid["yield_above_own_5y_avg"].notna()).sum()
        all_rows.append(valid)
        print(f"  [{i + 1}/16] {code} {ticker['name']}: 完了（相対判定可能日数 {n_relative_valid}日）")

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


def _walkforward(combined: pd.DataFrame, candidate_mask: pd.Series, candidate_label: str) -> dict:
    """暦年ウィンドウで候補条件 vs 現行本番基準(レンジ30%×絶対利回り3%)のプールド平均リターンを
    比較し、勝敗を集計する。"""
    print(f"\n=== ウォークフォワードOOS（暦年ウィンドウ、{candidate_label} vs 現行本番基準） ===")
    production_baseline = combined[
        (combined["range_position_pct"] <= RANGE_THRESHOLD_PCT) & (combined["dividend_yield_pct"] >= YIELD_THRESHOLD_PCT)
    ]
    candidate = combined[candidate_mask]

    results = {}
    for y in HORIZONS_YEARS:
        wins = 0
        total_windows = 0
        window_detail = []
        for year in WALKFORWARD_YEARS:
            win_start = pd.Timestamp(f"{year}-01-01")
            win_end = pd.Timestamp(f"{year}-12-31")
            base_sub = production_baseline[(production_baseline.index >= win_start) & (production_baseline.index <= win_end)][f"fwd_return_{y}y"].dropna()
            cand_sub = candidate[(candidate.index >= win_start) & (candidate.index <= win_end)][f"fwd_return_{y}y"].dropna()
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
        print(f"  {y}年後ホライズン: 候補が上回ったウィンドウ {wins}/{total_windows}")
        results[f"{y}y"] = {"wins": wins, "total_windows": total_windows, "windows": window_detail}
    return results


def main() -> int:
    combined = _load_all_samples()
    print(f"\n総サンプル数: {len(combined)}")

    result: dict = {
        "range_threshold_pct": RANGE_THRESHOLD_PCT,
        "yield_threshold_pct": YIELD_THRESHOLD_PCT,
        "rolling_years_for_relative": ROLLING_YEARS,
    }

    range_flag = combined["range_position_pct"] <= RANGE_THRESHOLD_PCT
    abs_yield_flag = combined["dividend_yield_pct"] >= YIELD_THRESHOLD_PCT
    relative_flag = combined["yield_above_own_5y_avg"] == True  # noqa: E712 - Noneと明示的に区別するため

    result["baseline_all_days"] = _stats(combined, "ベースライン(全シグナル日、フィルターなし)")
    result["absolute_yield_only"] = _stats(combined[abs_yield_flag], f"参考(既存): 絶対利回り{YIELD_THRESHOLD_PCT}%以上単体")
    result["relative_yield_only"] = _stats(combined[relative_flag], "①単独貢献度: 相対利回り(自身の過去5年平均以上)単体")
    result["absolute_and_relative"] = _stats(
        combined[abs_yield_flag & relative_flag], f"②二段構え: 絶対{YIELD_THRESHOLD_PCT}%以上 かつ 自身の過去5年平均以上"
    )
    result["production_baseline_range_and_absolute"] = _stats(
        combined[range_flag & abs_yield_flag], "参考(現行本番基準): レンジ30%以下 × 絶対利回り3%以上"
    )
    result["triple_and"] = _stats(
        combined[range_flag & abs_yield_flag & relative_flag],
        "③三重AND: レンジ30%以下 × 絶対利回り3%以上 × 自身の過去5年平均以上",
    )

    n_relative_valid = int((combined["yield_above_own_5y_avg"].notna()).sum())
    print(f"\n相対利回り判定が可能だった日数: {n_relative_valid} / {len(combined)}"
          f"（{n_relative_valid / len(combined) * 100:.1f}%、残りは観測期間不足で判定不能）")
    result["n_relative_judgeable"] = n_relative_valid

    result["walkforward_triple_vs_production"] = _walkforward(
        combined, range_flag & abs_yield_flag & relative_flag, "三重AND"
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n結果を保存: {OUTPUT_PATH}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
