# -*- coding: utf-8 -*-
"""
EDINET移行 Phase B: 新方式（BVPS自前計算+yfinance日次終値）でのPBRレンジ位置%を算出し、
旧IRBANK方式（bot/pbr_signal.py、期次PBR高安ベース）との差分を銘柄別に比較する。

【新方式の定義】（tasks/handoff_next_session.md 2026-08-16「Phase B進捗」参照）
旧方式はIRBANKが期次で公開する「PBR高値・安値」（期中の株価高値・安値÷BPSから逆算）を
そのままレンジの上限・下限として使っていた。新方式はEDINETから取得したBVPS（1株純資産、
実測値）とyfinance日次終値から、日次のPBR自体（Close(t) / BVPS_asof(t)）を計算し、
その日次PBR系列自体の過去10年ローリング高値・安値をレンジとする（期中の高安ではなく、
日次終値ベースの高安になる点が旧方式との定義上の違い。値が完全一致しないのは想定内）。

先読みバイアス回避・開示日の扱い（重要な設計変更、実装時に発覚したバグの修正）:
1件の有報には「当期＋過去4期」が収録されるが、Phase Bでは銘柄あたり2件（直近・5年前）
しか取得していないため、1件の書類に含まれる「過去4期」分の値は、実際にはその書類の
提出よりずっと前（それぞれの期の年次report時点）に公知だったはずのものである。
当初、全ての期にその書類のsubmitDateTimeをそのまま開示日として割り当てた結果、
中間の4年分が「次の書類が出るまでの数年間、直前の値のまま凍結される」という深刻な
バグを生み、レンジがほぼ計算不能になった（実装時に発覚、mean_abs_diffが40〜50pt超・
有効オーバーラップ日数が3年程度に激減する異常値で発覚）。
対処: context_ref が"CurrentYear"で始まる期（＝その書類自身の当期分、実際の提出日が
真の開示日と一致する）だけは実測submitDateTimeを使う。それ以外（"Prior1〜4Year"、
当書類での提出より前に別途公知だったはずだが実際の開示日は未取得）は、旧方式と同じ
「期末+45日」の仮定（bot.pbr_signal.DISCLOSURE_LAG_DAYS）にフォールバックする。
同じ期が複数書類に重複収録されている場合は、それぞれの推定開示日のうち早い方を採用する。

株式分割の調整（2026-08-16発覚・修正）:
EDINETの発行済株式数・自前計算BVPSは各有報の提出時点における実際の株数ベースであり、
分割調整されていない。一方yfinanceのClose終値は常に最新の株数基準に遡って調整済みのため、
分割をまたぐ期間をそのまま組み合わせると、分割比率の分だけBVPS/PBRが実勢から乖離する
（三菱商事で新方式BVPSが旧IRBANK方式の約3倍になる異常で発覚。原因は2023-12-28の
1:3株式分割。10/16銘柄が2016年以降に分割を経験している）。yfinanceの価格CSVに含まれる
"Stock Splits"列を使い、各BVPS値をその期末日より後に発生した分割比率の累積積で割ることで
最新株数基準に揃える（`_forward_split_adjustment_factor`）。

【実データ取得なし、config変更なし】
既存のcheck_signal.py・bot/pbr_signal.py・bot/irbank_pbr_range.pyは一切変更しない
（読み取り専用の差分比較スクリプト。本番切り替えはPhase C、要ユーザー許可）。

使い方:
    core16_dividend_botディレクトリで `python compute_edinet_pbr_range.py` を実行する。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

from bot.pbr_signal import DISCLOSURE_LAG_DAYS, build_period_records, compute_daily_signal
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
EDINET_DATA_PATH = DATA_CACHE / "edinet_financial_data.json"
IRBANK_DATA_PATH = DATA_CACHE / "irbank_pbr_range.json"
PRICE_DIR = DATA_CACHE / "yfinance_prices"
OUTPUT_PATH = DATA_CACHE / "edinet_vs_irbank_comparison_result.json"

ROLLING_WINDOW_DAYS = 3650  # 「過去10年」を暦日ベースで近似（旧方式のROLLING_WINDOW_PERIODS=10期に対応）
MIN_HISTORY_DAYS_FOR_RANGE = 500  # レンジ算出に最低限必要な日次観測数の目安（約2年分）
SIGNAL_THRESHOLD_PCT = 30.0  # 本番のレンジ30%以下シグナルと同じ閾値で判定一致率を見る

# 株式分割の「市場での権利落ち日」(yfinanceのStock Splits記録日)と「有報の発行済株式数への反映」
# の間には行政上のタイムラグがありうる（2026-08-16、ブリヂストンで発見: 2025-12-29分割にも
# かかわらず2025-12-31期末の発行済株式数が前年と同一のまま＝分割が未反映だった。この状態で
# 単純に「period_end < 分割日」を「反映済み」と判定すると、期末から分割日までわずか2日でも
# 「反映済み」扱いになり分割調整が誤って外れ、BVPSが約2倍の異常値になっていた）。
# bot/pbr_signal.pyのDISCLOSURE_LAG_DAYSと同じ考え方で、分割日から一定日数以内に期末日が
# 来る場合は「まだ反映されていない」とみなし、安全側に倒して調整を適用する。
SPLIT_REFLECTION_LAG_DAYS = 45


def _load_price_df(code: str) -> pd.DataFrame | None:
    price_path = PRICE_DIR / f"{code}.csv"
    if not price_path.exists():
        return None
    df = pd.read_csv(price_path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    cols = ["Close"] + (["Stock Splits"] if "Stock Splits" in df.columns else [])
    return df[cols].sort_index()


def _forward_split_adjustment_factor(splits: pd.Series, as_of_date: pd.Timestamp) -> float:
    """as_of_dateの発行済株式数にまだ反映されていないと考えられる株式分割の比率の
    累積積を返す（無ければ1.0）。

    EDINETの発行済株式数ベースのBVPSを、yfinanceの分割調整済み終値と同じ「最新株数基準」に
    揃えるために使う（上記モジュールdocstring「株式分割の調整」参照）。splitsは
    yfinance価格CSVの"Stock Splits"列（分割の無い日は0）を想定する。

    「反映済み」の判定は分割日そのものではなく`SPLIT_REFLECTION_LAG_DAYS`分の余裕を持たせる
    （分割日からas_of_dateまでの日数がこのラグ未満の場合、有報の発行済株式数にはまだ
    分割が反映されていない可能性が高いとみなし、安全側に倒して調整を適用する）。
    """
    cutoff = as_of_date - pd.Timedelta(days=SPLIT_REFLECTION_LAG_DAYS)
    future_splits = splits[(splits.index > cutoff) & (splits != 0)]
    if future_splits.empty:
        return 1.0
    return float(future_splits.prod())


def load_edinet_bvps_series(code: str, edinet_data: dict) -> pd.DataFrame:
    """1銘柄分のBVPSを全書類から集約し、period_end昇順・重複排除したDataFrameを返す
    （列: period_end, bvps, disclosure_date）。"""
    ticker = edinet_data["tickers"].get(code)
    if ticker is None:
        return pd.DataFrame(columns=["period_end", "bvps", "disclosure_date"])

    records: dict[str, dict] = {}
    for doc in ticker["documents"].values():
        if "error" in doc:
            continue
        submit_dt = pd.to_datetime(doc["meta"]["submitDateTime"]).normalize()
        for row in doc["bvps"]:
            period_end = row["period_end"]
            value = row["value"]
            if value is None:
                continue
            # "CurrentYear"の期のみ、この書類の提出日=真の開示日と一致する。
            # "Prior1〜4Year"は当書類より前に別の書類で公知だったはずだが、そちらは未取得
            # のため、旧方式と同じ「期末+45日」の仮定にフォールバックする（上記docstring参照）。
            if row["context_ref"].startswith("CurrentYear"):
                candidate_disclosure_date = submit_dt
            else:
                candidate_disclosure_date = pd.Timestamp(period_end) + pd.Timedelta(days=DISCLOSURE_LAG_DAYS)

            existing = records.get(period_end)
            if existing is None or candidate_disclosure_date < existing["disclosure_date"]:
                records[period_end] = {
                    "period_end": pd.Timestamp(period_end),
                    "bvps": value,
                    "disclosure_date": candidate_disclosure_date,
                }

    if not records:
        return pd.DataFrame(columns=["period_end", "bvps", "disclosure_date"])
    return pd.DataFrame(records.values()).sort_values("period_end").reset_index(drop=True)


def compute_new_method_daily(code: str, edinet_data: dict) -> pd.DataFrame | None:
    bvps_df = load_edinet_bvps_series(code, edinet_data)
    if bvps_df.empty:
        return None
    price_df = _load_price_df(code)
    if price_df is None:
        return None

    # 株式分割調整: 各BVPSをその期末日より後の分割比率の累積積で割り、yfinance終値と
    # 同じ「最新株数基準」に揃える（上記モジュールdocstring「株式分割の調整」参照）。
    if "Stock Splits" in price_df.columns:
        bvps_df = bvps_df.copy()
        bvps_df["bvps"] = bvps_df.apply(
            lambda row: row["bvps"] / _forward_split_adjustment_factor(price_df["Stock Splits"], row["period_end"]),
            axis=1,
        )

    df = price_df[["Close"]].copy()
    bvps_sorted = bvps_df.sort_values("disclosure_date")
    merged = pd.merge_asof(
        df.reset_index().rename(columns={"index": "Date", "Date": "Date"}),
        bvps_sorted[["disclosure_date", "bvps"]].rename(columns={"disclosure_date": "Date"}),
        on="Date",
        direction="backward",
    ).set_index("Date")
    df["bvps"] = merged["bvps"]
    df["current_pbr"] = df["Close"] / df["bvps"]

    # 新方式のレンジ: 期次PBR高安ではなく、日次で自前計算したcurrent_pbr自体のローリング高安
    # （過去10年=3650暦日ウィンドウ、その日までのデータのみ使用し先読みバイアスを避ける）
    rolling = df["current_pbr"].rolling(f"{ROLLING_WINDOW_DAYS}D", min_periods=MIN_HISTORY_DAYS_FOR_RANGE)
    df["range_high"] = rolling.max()
    df["range_low"] = rolling.min()
    span = df["range_high"] - df["range_low"]
    range_position_pct = (df["current_pbr"] - df["range_low"]) / span * 100
    range_position_pct[span <= 0] = None
    df["range_position_pct"] = range_position_pct
    return df


def compute_old_method_daily(code: str, irbank_data: dict) -> pd.DataFrame | None:
    ticker = irbank_data["tickers"].get(code)
    if ticker is None:
        return None
    price_df = _load_price_df(code)
    if price_df is None:
        return None
    period_df = build_period_records(ticker["periods"])
    return compute_daily_signal(price_df, period_df)


def compare_ticker(name: str, new_df: pd.DataFrame, old_df: pd.DataFrame) -> dict:
    merged = pd.DataFrame(
        {"new": new_df["range_position_pct"], "old": old_df["range_position_pct"]}
    ).dropna()

    if merged.empty:
        return {"name": name, "overlap_days": 0, "note": "両方式が重なる有効日が無い"}

    diff = merged["new"] - merged["old"]
    new_signal = merged["new"] <= SIGNAL_THRESHOLD_PCT
    old_signal = merged["old"] <= SIGNAL_THRESHOLD_PCT
    agree = new_signal == old_signal
    latest = merged.iloc[-1]

    return {
        "name": name,
        "overlap_days": int(len(merged)),
        "overlap_start": str(merged.index[0].date()),
        "overlap_end": str(merged.index[-1].date()),
        "mean_abs_diff_pct": float(diff.abs().mean()),
        "max_abs_diff_pct": float(diff.abs().max()),
        "correlation": float(merged["new"].corr(merged["old"])),
        "signal_agreement_rate": float(agree.mean()),
        "signal_disagreement_days": int((~agree).sum()),
        "latest_date": str(merged.index[-1].date()),
        "latest_new_pct": float(latest["new"]),
        "latest_old_pct": float(latest["old"]),
        "latest_signal_new": bool(new_signal.iloc[-1]),
        "latest_signal_old": bool(old_signal.iloc[-1]),
    }


def main() -> int:
    edinet_data = json.loads(EDINET_DATA_PATH.read_text(encoding="utf-8"))
    irbank_data = json.loads(IRBANK_DATA_PATH.read_text(encoding="utf-8"))

    results: dict[str, dict] = {}
    for t in CORE16_UNIVERSE:
        code, name = t["code"], t["name"]
        new_df = compute_new_method_daily(code, edinet_data)
        old_df = compute_old_method_daily(code, irbank_data)
        if new_df is None or old_df is None:
            results[code] = {"name": name, "note": "データ不足によりスキップ"}
            print(f"  {code} {name}: データ不足によりスキップ")
            continue
        r = compare_ticker(name, new_df, old_df)
        results[code] = r
        if r.get("overlap_days"):
            print(
                f"  {code} {name}: overlap={r['overlap_days']}日 "
                f"mean_abs_diff={r['mean_abs_diff_pct']:.2f}pt corr={r['correlation']:.3f} "
                f"agree_rate={r['signal_agreement_rate']:.3f} "
                f"latest(new={r['latest_new_pct']:.1f}%/old={r['latest_old_pct']:.1f}%)"
            )
        else:
            print(f"  {code} {name}: {r.get('note')}")

    output = {
        "threshold_pct": SIGNAL_THRESHOLD_PCT,
        "rolling_window_days": ROLLING_WINDOW_DAYS,
        "min_history_days_for_range": MIN_HISTORY_DAYS_FOR_RANGE,
        "tickers": results,
    }
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    tmp_path = OUTPUT_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(OUTPUT_PATH)  # atomic write
    print(f"\n完了。結果を {OUTPUT_PATH} へ保存しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
