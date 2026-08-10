# -*- coding: utf-8 -*-
"""
IRBANKの年度PBR高値・安値（bot/irbank_pbr_range.pyで取得）とyfinanceの日次株価から、
「その時点でのPBRが過去10年レンジのどこに位置するか」を日次で算出する。

判定指標の定義（tasks/backtest_design_core16_dividend_range_strategy.md「判定指標の定義」）:
    レンジ位置(t) = (PBR(t) - 過去10年最安値) / (過去10年最高値 - 過去10年最安値) × 100(%)

先読みバイアス回避の設計（2026-08-07パネルレビュー・CRO指摘への対応）:
- 各期のBPSは、期末日そのものではなく「期末日 + 開示ラグ」以降にのみ市場に知られていた
  ものとして扱う（日本の決算短信は期末後45日以内の開示が一般的なため、保守的にラグ日数を
  DISCLOSURE_LAG_DAYSとして仮定する。実際の開示日をIRBANKページから取得できるかは
  未確認のため暫定値。tasks/lessons.md「マルチタイムフレームのアラインは先読みバイアスに
  注意」の原則を、日次×年次のアラインにも適用したもの）。
- 「過去10年レンジ」は、その時点までに開示済みの期のみを対象にする（未来の期のPBR高値・
  安値を混ぜない）。

BPSはIRBANKが直接公開していないため、期中の株価高値・安値とPBR高値・安値から
逆算する（BPS ≈ 株価高値 ÷ PBR高値、株価安値 ÷ PBR安値の平均）。両者の乖離は
実データで検証済み（2026-08-07、16銘柄・直近3期で最大0.8%、ほぼ全て0.5%未満）。
"""
from __future__ import annotations

import bisect
import calendar

import pandas as pd

DISCLOSURE_LAG_DAYS = 45  # 決算短信の開示ラグの保守的な仮定（要検証、上記docstring参照）
MIN_PERIODS_FOR_SIGNAL = 5  # レンジ計算に最低限必要な開示済み期数（これ未満はシグナルを出さない）
ROLLING_WINDOW_PERIODS = 10  # 「過去10年」に対応する期数（開示済みの中から直近N期を使う）

# 業績正常化チェック（tasks/backtest_design_core16_dividend_range_strategy.md「業績正常化チェック」）
EARNINGS_TRAILING_AVG_PERIODS = 5  # 「過去5年平均」の期数
EARNINGS_ANOMALY_DEVIATION_THRESHOLD = 0.30  # ±30%超の乖離で異常フラグ


def period_end_date(end_year: int, end_month: int) -> pd.Timestamp:
    last_day = calendar.monthrange(end_year, end_month)[1]
    return pd.Timestamp(end_year, end_month, last_day)


def build_period_records(periods_raw: list[dict]) -> pd.DataFrame:
    """fetch_pbr_range_data.pyが保存したJSONの periods (dictのlist) から、
    開示日・逆算BPS・PBR高安を持つDataFrameを作る（期末日の昇順）。

    PBRまたは株価のいずれかが欠損(None)の期は bps が NaN になる
    （0扱いにしてレンジ計算を汚染しないため）。
    """
    rows = []
    for p in periods_raw:
        end_date = period_end_date(p["end_year"], p["end_month"])
        disclosure_date = end_date + pd.Timedelta(days=DISCLOSURE_LAG_DAYS)
        bps = None
        if p.get("pbr_high") and p.get("pbr_low") and p.get("price_high") and p.get("price_low"):
            bps_from_high = p["price_high"] / p["pbr_high"]
            bps_from_low = p["price_low"] / p["pbr_low"]
            bps = (bps_from_high + bps_from_low) / 2

        # EPS ≈ 株価 ÷ PER（BPS逆算と同じ手法）。純利益ではなく1株益ベースだが、
        # 「著しい業績変動」の検出という目的には同じ情報量を持つ。PERが「-」（赤字等で
        # IRBANKが算出していない）期はNoneのままにする。
        eps = None
        if p.get("per_high") and p.get("per_low") and p.get("price_high") and p.get("price_low"):
            eps_from_high = p["price_high"] / p["per_high"]
            eps_from_low = p["price_low"] / p["per_low"]
            eps = (eps_from_high + eps_from_low) / 2

        rows.append(
            {
                "fiscal_year_label": p["fiscal_year_label"],
                "end_date": end_date,
                "disclosure_date": disclosure_date,
                "pbr_high": p.get("pbr_high"),
                "pbr_low": p.get("pbr_low"),
                "bps": bps,
                "eps": eps,
            }
        )
    df = pd.DataFrame(rows).sort_values("end_date").reset_index(drop=True)

    # 業績正常化チェック用フラグ: 各期のEPSが、その期を除く直近5期平均から±30%超
    # 乖離しているか。乖離した期の見かけの低PBRは「一時的な好業績による分母肥大」の
    # 可能性があるとみなす。トレイリング平均に使う過去期が5期に満たない、またはEPS欠損の
    # 場合は判定不能としてFalse（=正常扱い、除外しない）にする。
    anomaly_flags = [False] * len(df)
    for i in range(len(df)):
        history = df["eps"].iloc[max(0, i - EARNINGS_TRAILING_AVG_PERIODS) : i].dropna()
        current_eps = df["eps"].iloc[i]
        if len(history) < EARNINGS_TRAILING_AVG_PERIODS or pd.isna(current_eps):
            continue
        trailing_avg = history.mean()
        if trailing_avg == 0:
            continue
        deviation = abs(current_eps - trailing_avg) / abs(trailing_avg)
        anomaly_flags[i] = bool(deviation > EARNINGS_ANOMALY_DEVIATION_THRESHOLD)
    df["earnings_anomaly"] = anomaly_flags

    return df


def compute_daily_signal(price_df: pd.DataFrame, period_df: pd.DataFrame) -> pd.DataFrame:
    """日次のレンジ位置(%)シリーズを計算する。

    price_df: index=日付、列に少なくとも "Close" を含む（分割調整済み・配当未調整の終値。
              fetch_yfinance_price_data.pyが保存したCSVをそのまま読み込む想定）
    period_df: build_period_records()の出力

    戻り値: price_dfに以下の列を追加したDataFrame
        bps, current_pbr, range_high, range_low, range_position_pct
    有効なシグナルが出せない日（開示済み期数不足・レンジ幅ゼロ等）は range_position_pct が NaN。
    """
    df = price_df.copy()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    valid_periods = period_df.dropna(subset=["bps"]).reset_index(drop=True)

    bps_vals = [None] * len(df)
    range_high_vals = [None] * len(df)
    range_low_vals = [None] * len(df)
    earnings_anomaly_vals = [False] * len(df)

    for i, date in enumerate(df.index):
        disclosed = valid_periods[valid_periods["disclosure_date"] <= date]
        if len(disclosed) == 0:
            continue
        latest = disclosed.iloc[-1]
        bps_vals[i] = latest["bps"]
        earnings_anomaly_vals[i] = bool(latest.get("earnings_anomaly", False))

        window = disclosed.tail(ROLLING_WINDOW_PERIODS)
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
    df["earnings_anomaly"] = earnings_anomaly_vals

    span = df["range_high"] - df["range_low"]
    range_position_pct = (df["current_pbr"] - df["range_low"]) / span * 100
    range_position_pct[span <= 0] = None  # レンジ幅0以下(異常値)は無効化。ZeroDivisionは出さずNaN化
    df["range_position_pct"] = range_position_pct

    return df


EXPANDING_PERCENTILE_MIN_HISTORY = 250  # 拡張パーセンタイル判定に必要な最低観測日数（約1年分）


def compute_expanding_percentile_flag(
    range_position: pd.Series,
    percentile: float = 20.0,
    min_history: int = EXPANDING_PERCENTILE_MIN_HISTORY,
) -> pd.Series:
    """銘柄ごとに固定の絶対閾値（例:「30%以下」）を全銘柄一律に適用する代わりに、
    「その銘柄自身のこれまでの観測値の中で下位◯パーセンタイルに入っているか」を判定する。

    背景（2026-08-07、ユーザー提案「増配率も考慮して閾値は個別に設定した方が良いのでは」への
    パネルレビュー対応）: NTT・KDDIのように構造的にPBRが切り上がり続ける銘柄は、
    自身の過去10年レンジの中でも「相対的に安い」水準（絶対値30%以下）まで滅多に
    下がらない（実測: NTTのrange_position_pct最小値は44.0%）。銘柄ごとに16個の
    絶対閾値を個別最適化すると、シグナル数が薄い銘柄（例: 東京海上HD 25件）では
    過学習リスクが高い（パネルレビュー・ドメインエキスパート/QA指摘）。本関数は
    自由パラメータを「percentile」1個だけに抑えつつ、銘柄ごとに自己校正される
    閾値判定を実現する。

    先読みバイアス回避: 判定に使うパーセンタイルは、その日までに観測済みの値のみから
    計算する（未来の値は一切使わない、拡張ウィンドウ）。min_history日分の有効な
    観測が無いうちはNone（判定不能）を返す。

    range_position: compute_daily_signal()が返す "range_position_pct" 列（1銘柄分）。
    戻り値: 同じindexを持つSeries。True=下位percentile%以内（買い候補）、
            False=それ以外、None=判定に必要な観測数がまだ無い。
    """
    sorted_history: list[float] = []
    result = []
    for val in range_position:
        if pd.notna(val):
            bisect.insort(sorted_history, val)
        if len(sorted_history) < min_history or pd.isna(val):
            result.append(None)
            continue
        idx = min(int(len(sorted_history) * percentile / 100), len(sorted_history) - 1)
        threshold_value = sorted_history[idx]
        result.append(bool(val <= threshold_value))
    return pd.Series(result, index=range_position.index)
