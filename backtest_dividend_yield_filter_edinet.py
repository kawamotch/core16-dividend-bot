# -*- coding: utf-8 -*-
"""
backtest_dividend_yield_filter.py のEDINET新方式版（2026-08-17、review-panelフルレビューで
Phase C着手前の必須確認事項として指定。「本番が実際に使う条件[レンジ30%以下×利回り3%以上]の
新方式での挙動を確認しないままPhase Cに進むのは検証の抜け穴」というQA指摘に対応）。

分析ロジック（単独貢献度→条件剥ぎ取り→交互作用→ランダムベースライン→ウォークフォワードOOS）は
元スクリプトと完全に同一。データ源のみEDINET新方式に差し替える。

【配当利回りの算出方法・株式分割調整について】
EDINETのdps_total（各有報が開示する当期の年間配当合計の実額）は、BVPSと全く同じ理由で
株式分割調整が必要（分割前の株数ベースの配当額をそのままyfinanceの分割調整済み終値と
組み合わせると、利回りが分割比率倍にずれる）。`compute_edinet_pbr_range.py`でBVPSの分割調整に
使い実データで検算済みの`_forward_split_adjustment_factor`（分割日から
SPLIT_REFLECTION_LAG_DAYS=45日以内の期末日は「未反映」とみなす安全側判定込み）をそのまま
流用する。開示日の推定（"CurrentYear"の期のみ実測submitDateTime、"Prior1〜4Year"は
期末+45日仮定）も同モジュールのBVPSローダーと同じロジックを踏襲する。

【分割またぎ根本修正・2026-08-17実装済み】
IRBANK版が抱えていた「期の途中で分割が起きるケースを中間・期末別々に補正できない」という
既知の制約（分割またぎ配当異常値、tasks/handoff_archive.md 2026-08-10参照）は、EDINET版では
dps_interim（中間配当、16銘柄全件で取得済み）を使って解消済み。詳細は
`load_edinet_dps_series()`のdocstring参照。YIELD_OUTLIER_CEILING_PCTガードは多重防御として
引き続き維持する。

【Phase C（2026-08-17）で本番採用済み】
この関数群は`check_signal.py`（本番、USE_EDINET_METHOD=True時）から直接import・使用される。
既存のbacktest_dividend_yield_filter.py（IRBANK版、ロールバック用）・bot/pbr_signal.pyは
変更しない。

使い方:
    core16_dividend_botディレクトリで `python backtest_dividend_yield_filter_edinet.py` を実行する。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pandas as pd

from bot.pbr_signal import DISCLOSURE_LAG_DAYS
from compute_edinet_pbr_range import EDINET_DATA_PATH, _forward_split_adjustment_factor, _load_price_df
from edinet_signal_adapter import compute_daily_signal_edinet
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
OUTPUT_PATH = DATA_CACHE / "dividend_yield_filter_edinet_result.json"

HORIZONS_YEARS = [1, 3, 5]
FORWARD_DATE_TOLERANCE_DAYS = 10
RANGE_THRESHOLD_PCT = 30
YIELD_THRESHOLD_PCT = 3.0  # IRBANK版・長期株式投資さん原典と同じ目安（配当利回り3%以上）
YIELD_OUTLIER_CEILING_PCT = 8.0  # IRBANK版と同じ保険ガード（分割またぎ等の残存異常値対策）
INTERIM_PAYMENT_APPROX_DAYS_BEFORE_PERIOD_END = 182  # 中間配当記録日の近似（load_edinet_dps_series参照）
RANDOM_BASELINE_TRIALS = 20
RANDOM_SEED = 42

# ウォークフォワード用の暦年ウィンドウ（backtest_dividend_yield_filter.pyと同じ非重複ウィンドウ方式）。
# EDINETデータは銘柄により2016〜2019年頃からしか蓄積が無いため、2011〜2015年のウィンドウは
# サンプル僅少により_walkforward()内のガード（n<5で除外）で自動的にスキップされる想定。
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


def load_edinet_dps_series(code: str, edinet_data: dict, price_df: pd.DataFrame) -> pd.DataFrame:
    """1銘柄分の年間配当合計(dps_total)を全書類から集約し、株式分割調整・開示日推定を行った
    DataFrameを返す（列: period_end, dps_total, disclosure_date）。
    ロジックは compute_edinet_pbr_range.load_edinet_bvps_series と同一（BVPSをdps_totalに
    置き換えただけ、両者とも「1株当たり実額を最新株数基準へ揃える」という同じ問題を抱えるため）。

    分割またぎ対応（2026-08-17、review-panel実装レビュー承認・実測監査で6銘柄9期分の該当を
    確認済み）: 年間配当合計(dps_total)をそのまま単一の分割調整係数で補正すると、期の途中で
    分割が起きた期（中間配当は旧株数ベース・期末配当は新株数ベースで支払われるが、両者は
    「合計」でしか保持していなかった旧IRBANK版と同じ問題）で誤った調整になる。dps_interim
    （中間配当、EDINETで別タグとして取得済み、16銘柄全件で確認済み）を使い、中間分・
    期末分(dps_total-dps_interim)を別々の基準日で`_forward_split_adjustment_factor`に
    かけてから合算する。
    - 期末分の基準日: period_end（既存ロジックそのまま）
    - 中間分の基準日: period_end - INTERIM_PAYMENT_APPROX_DAYS_BEFORE_PERIOD_END日
      （日本の中間配当の一般的な記録日[3月期→9月末、12月期→6月末]の近似。EDINETのXBRLタグは
      中間配当の実際の記録日を持たないため近似せざるを得ない）
    - dps_interimが取得できない期は、旧来通りdps_total全体を期末基準で一括調整するロジックに
      フォールバックする
    - dps_final(=dps_total-dps_interim)が負になる期はデータ不整合（修正配当等）としてその期を
      スキップする（欠損扱い、NaN化）
    """
    ticker = edinet_data["tickers"].get(code)
    if ticker is None:
        return pd.DataFrame(columns=["period_end", "dps_total", "disclosure_date"])

    records: dict[str, dict] = {}
    for doc in ticker["documents"].values():
        if "error" in doc:
            continue
        submit_dt = pd.to_datetime(doc["meta"]["submitDateTime"]).normalize()

        interim_by_context: dict[str, float] = {
            row["context_ref"]: row["value"] for row in doc.get("dps_interim", []) if row["value"] is not None
        }

        for row in doc.get("dps_total", []):
            period_end = row["period_end"]
            value = row["value"]
            if value is None:
                continue
            if row["context_ref"].startswith("CurrentYear"):
                candidate_disclosure_date = submit_dt
            else:
                candidate_disclosure_date = pd.Timestamp(period_end) + pd.Timedelta(days=DISCLOSURE_LAG_DAYS)

            existing = records.get(period_end)
            if existing is None or candidate_disclosure_date < existing["disclosure_date"]:
                records[period_end] = {
                    "period_end": pd.Timestamp(period_end),
                    "dps_total": value,
                    "dps_interim": interim_by_context.get(row["context_ref"]),
                    "disclosure_date": candidate_disclosure_date,
                }

    if not records:
        return pd.DataFrame(columns=["period_end", "dps_total", "disclosure_date"])
    dps_df = pd.DataFrame(records.values()).sort_values("period_end").reset_index(drop=True)

    if "Stock Splits" in price_df.columns:
        def _split_adjusted_total(r: pd.Series) -> float:
            interim = r["dps_interim"]
            total = r["dps_total"]
            if interim is None:
                # フォールバック: 中間配当が取得できない期は旧来通り一括調整
                return total / _forward_split_adjustment_factor(price_df["Stock Splits"], r["period_end"])
            final = total - interim
            if final < 0:
                return float("nan")  # データ不整合（修正配当等）、この期はスキップ扱い
            interim_asof = r["period_end"] - pd.Timedelta(days=INTERIM_PAYMENT_APPROX_DAYS_BEFORE_PERIOD_END)
            interim_adj = interim / _forward_split_adjustment_factor(price_df["Stock Splits"], interim_asof)
            final_adj = final / _forward_split_adjustment_factor(price_df["Stock Splits"], r["period_end"])
            return interim_adj + final_adj

        dps_df["dps_total"] = dps_df.apply(_split_adjusted_total, axis=1)
        dps_df = dps_df.dropna(subset=["dps_total"])

    return dps_df[["period_end", "dps_total", "disclosure_date"]]


def build_dividend_yield_series_edinet(code: str, edinet_data: dict, price_df: pd.DataFrame) -> pd.Series:
    """各日の配当利回り(%)を返す（先読み回避のため開示日以降ffill、分割調整済みdps_total ÷
    Close）。IRBANK版build_dividend_yield_series()と同じ設計思想（欠損はNaN、異常値ガードあり）。
    """
    dps_df = load_edinet_dps_series(code, edinet_data, price_df)
    if dps_df.empty:
        return pd.Series(float("nan"), index=price_df.index)

    dps_sorted = dps_df.sort_values("disclosure_date")
    merged = pd.merge_asof(
        price_df.reset_index().rename(columns={"index": "Date"})[["Date"]],
        dps_sorted[["disclosure_date", "dps_total"]].rename(columns={"disclosure_date": "Date"}),
        on="Date",
        direction="backward",
    ).set_index("Date")
    dps_series = merged["dps_total"].reindex(price_df.index)
    yield_pct = dps_series / price_df["Close"] * 100
    yield_pct[yield_pct > YIELD_OUTLIER_CEILING_PCT] = float("nan")  # 分割またぎ等の残存異常値ガード
    return yield_pct


def _load_all_samples() -> pd.DataFrame:
    edinet_data = json.loads(EDINET_DATA_PATH.read_text(encoding="utf-8"))
    all_rows = []
    print("全銘柄のレンジ位置・配当利回り(EDINET新方式)・フォワードリターンを計算中...")
    for i, ticker in enumerate(CORE16_UNIVERSE):
        code = ticker["code"]
        price_path = DATA_CACHE / "yfinance_prices" / f"{code}.csv"
        if not price_path.exists():
            continue
        sig = compute_daily_signal_edinet(code)
        if sig is None:
            print(f"  [{i + 1}/16] {code} {ticker['name']}: データ不足によりスキップ")
            continue

        raw_price_df = _load_price_df(code)  # Close + Stock Splits（分割調整用、"Adj Close"は無い）
        sig["dividend_yield_pct"] = build_dividend_yield_series_edinet(code, edinet_data, raw_price_df).reindex(sig.index)

        valid = sig.dropna(subset=["range_position_pct"]).copy()
        if valid.empty:
            continue
        adj_close = sig["Adj Close"]  # 配当込み総リターン（IRBANK版と条件を揃える）
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
    プールド平均リターンを比較し、勝敗を集計する（backtest_dividend_yield_filter.pyと同じ
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
