# -*- coding: utf-8 -*-
"""
core16_dividend_bot: backtest_dividend_yield_filter_edinet.pyのload_edinet_dps_series()/
build_dividend_yield_series_edinet()を合成データで検証する自己テスト。ネットワーク不要。

特に「分割またぎ」根本修正（2026-08-17、review-panel実装レビュー承認）の中核ロジック
（中間配当・期末配当を別々の基準日で株式分割調整してから合算する）を対象とする。
セッション中にワンオフの検証スクリプトで確認した4パターンを恒久的な回帰テストとして
定着させたもの（他のtest_*_synthetic.pyと同じ慣習に合わせる）。

使い方:
    core16_dividend_botディレクトリで `python test_dividend_yield_filter_edinet_synthetic.py` を実行する。
"""
from __future__ import annotations

import pandas as pd

from backtest_dividend_yield_filter_edinet import (
    build_dividend_yield_series_edinet,
    load_edinet_dps_series,
)

_PRICE_INDEX = pd.date_range("2024-01-01", "2026-06-01", freq="D")


def _make_price_df(split_date: str | None = None, split_ratio: float = 2.0) -> pd.DataFrame:
    df = pd.DataFrame(index=_PRICE_INDEX)
    df["Close"] = 1000.0
    df["Stock Splits"] = 0.0
    if split_date is not None:
        df.loc[pd.Timestamp(split_date), "Stock Splits"] = split_ratio
    return df


def _make_edinet_data(dps_total: float, dps_interim: list[dict] | None) -> dict:
    doc = {
        "meta": {"submitDateTime": "2026-03-01 09:00"},
        "dps_total": [{"context_ref": "CurrentYearDuration_NonConsolidatedMember", "period_end": "2025-12-31", "value": dps_total}],
    }
    if dps_interim is not None:
        doc["dps_interim"] = dps_interim
    return {"tickers": {"T": {"documents": {"D1": doc}}}}


def test_no_split_regression_matches_simple_sum():
    """期中に分割が無い場合、中間+期末の単純合算と一致する（既存挙動の回帰確認）。"""
    edinet_data = _make_edinet_data(
        dps_total=100.0,
        dps_interim=[{"context_ref": "CurrentYearDuration_NonConsolidatedMember", "period_end": "2025-12-31", "value": 40.0}],
    )
    price_df = _make_price_df(split_date=None)
    dps = load_edinet_dps_series("T", edinet_data, price_df)
    assert abs(dps.iloc[0]["dps_total"] - 100.0) < 1e-9, dps.iloc[0]["dps_total"]


def test_mid_period_split_adjusts_interim_and_final_separately():
    """期中に分割がある場合、中間・期末で異なる調整係数がかかる
    （単純な100/2=50円ではなく、中間分のみ分割調整された80円になるはず）。"""
    edinet_data = _make_edinet_data(
        dps_total=100.0,
        dps_interim=[{"context_ref": "CurrentYearDuration_NonConsolidatedMember", "period_end": "2025-12-31", "value": 40.0}],
    )
    # 中間支払い想定日(2025-12-31 - 182日 ≒ 2025-07-02)より後、期末より前の2025-09-01に1:2分割
    price_df = _make_price_df(split_date="2025-09-01", split_ratio=2.0)
    dps = load_edinet_dps_series("T", edinet_data, price_df)
    # 中間40円は分割前支払い→分割後基準で40/2=20円。期末60円(=100-40)は分割後の期末日なので無調整のまま60円。
    expected = 40.0 / 2.0 + 60.0
    assert abs(dps.iloc[0]["dps_total"] - expected) < 1e-9, (dps.iloc[0]["dps_total"], expected)
    # 単純な一括調整(100/2=50)とは異なることも明示的に確認する
    assert abs(dps.iloc[0]["dps_total"] - 50.0) > 1e-6


def test_missing_interim_falls_back_to_lump_sum_adjustment():
    """dps_interimが取得できない期は、旧来通りdps_total全体を期末基準で一括調整する
    フォールバックに切り替わる。"""
    edinet_data = _make_edinet_data(dps_total=100.0, dps_interim=[])
    # 分割日(2025-09-01)はperiod_end(2025-12-31)より前 → 期末基準では調整不要(1.0倍)
    price_df = _make_price_df(split_date="2025-09-01", split_ratio=2.0)
    dps = load_edinet_dps_series("T", edinet_data, price_df)
    assert abs(dps.iloc[0]["dps_total"] - 100.0) < 1e-9, dps.iloc[0]["dps_total"]


def test_negative_final_portion_is_treated_as_anomaly_and_dropped():
    """dps_interimがdps_totalを上回る（dps_final<0）データ不整合は、その期を除外する。"""
    edinet_data = _make_edinet_data(
        dps_total=30.0,
        dps_interim=[{"context_ref": "CurrentYearDuration_NonConsolidatedMember", "period_end": "2025-12-31", "value": 40.0}],
    )
    price_df = _make_price_df(split_date=None)
    dps = load_edinet_dps_series("T", edinet_data, price_df)
    assert len(dps) == 0, dps


def test_daily_yield_series_forward_fills_after_disclosure_with_split_adjustment():
    """build_dividend_yield_series_edinet()が、分割調整済みdps_totalを開示日以降に
    先読み無くffillし、日次利回り(%)として組み立てることを確認する。"""
    edinet_data = _make_edinet_data(
        dps_total=100.0,
        dps_interim=[{"context_ref": "CurrentYearDuration_NonConsolidatedMember", "period_end": "2025-12-31", "value": 40.0}],
    )
    price_df = _make_price_df(split_date="2025-09-01", split_ratio=2.0)
    yield_series = build_dividend_yield_series_edinet("T", edinet_data, price_df)
    # 開示日(submitDateTime=2026-03-01)以降のみ値が入る（先読み回避）
    assert pd.isna(yield_series.loc["2026-02-28"]), yield_series.loc["2026-02-28"]
    expected_pct = (40.0 / 2.0 + 60.0) / 1000.0 * 100
    assert abs(yield_series.loc["2026-03-01"] - expected_pct) < 1e-9, yield_series.loc["2026-03-01"]


def _run_all() -> bool:
    tests = [
        test_no_split_regression_matches_simple_sum,
        test_mid_period_split_adjusts_interim_and_final_separately,
        test_missing_interim_falls_back_to_lump_sum_adjustment,
        test_negative_final_portion_is_treated_as_anomaly_and_dropped,
        test_daily_yield_series_forward_fills_after_disclosure_with_split_adjustment,
    ]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL: {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001 - 予期せぬ例外もFAILとして可視化する
            print(f"FAIL (unexpected exception): {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    import sys

    ok = _run_all()
    sys.exit(0 if ok else 1)
