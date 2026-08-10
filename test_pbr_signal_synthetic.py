# -*- coding: utf-8 -*-
"""
core16_dividend_bot: bot/pbr_signal.pyのbuild_period_records()/compute_daily_signal()を
合成データで検証する自己テスト。ネットワークアクセス不要。

実行前提: test_irbank_pbr_range_synthetic.py が全件合格していること。

使い方:
    core16_dividend_botディレクトリで `python test_pbr_signal_synthetic.py` を実行する。
"""
from __future__ import annotations

import pandas as pd

from bot.pbr_signal import (
    DISCLOSURE_LAG_DAYS,
    EARNINGS_TRAILING_AVG_PERIODS,
    MIN_PERIODS_FOR_SIGNAL,
    build_period_records,
    compute_daily_signal,
    compute_expanding_percentile_flag,
    period_end_date,
)


def _synthetic_periods_raw(n_periods: int = 6) -> list[dict]:
    """PBRが毎期1.0ずつ増える単調な合成データ（レンジ計算の検算がしやすいように）。
    株価1000・PBR(高値=安値=期の値)としてBPS逆算がちょうど1000/期の値になるようにする。"""
    periods = []
    for i in range(n_periods):
        year = 2015 + i
        pbr = 1.0 + i  # 1.0, 2.0, 3.0, ...
        periods.append(
            {
                "fiscal_year_label": f"{year}年3月期",
                "end_year": year,
                "end_month": 3,
                "pbr_high": pbr,
                "pbr_low": pbr,
                "price_high": 1000.0 * pbr,
                "price_low": 1000.0 * pbr,
            }
        )
    return periods


def test_build_period_records_derives_bps_correctly():
    df = build_period_records(_synthetic_periods_raw())
    # price=1000*pbr, pbr_high=pbr_low=pbr なので BPS = 1000*pbr / pbr = 1000 ちょうど
    assert (df["bps"] == 1000.0).all(), df["bps"].tolist()


def test_build_period_records_disclosure_date_has_lag():
    df = build_period_records(_synthetic_periods_raw())
    first = df.iloc[0]
    expected_end = period_end_date(2015, 3)
    assert first["end_date"] == expected_end
    assert first["disclosure_date"] == expected_end + pd.Timedelta(days=DISCLOSURE_LAG_DAYS)


def test_missing_pbr_or_price_yields_nan_bps_not_zero():
    raw = _synthetic_periods_raw(3)
    raw[1]["pbr_high"] = None
    raw[1]["pbr_low"] = None
    df = build_period_records(raw)
    assert pd.isna(df.iloc[1]["bps"]), "PBR欠損期のBPSはNaNであるべき（0扱いはレンジを汚染する）"
    assert not pd.isna(df.iloc[0]["bps"])
    assert not pd.isna(df.iloc[2]["bps"])


def _synthetic_periods_with_eps(eps_values: list[float]) -> list[dict]:
    """業績正常化チェック検証用: PBR・株価は固定(1.0倍・1000円)にし、PERだけを
    eps_valuesが厳密に逆算できるよう調整した合成データ。"""
    periods = []
    for i, eps in enumerate(eps_values):
        year = 2015 + i
        price = 1000.0
        per = price / eps
        periods.append(
            {
                "fiscal_year_label": f"{year}年3月期",
                "end_year": year,
                "end_month": 3,
                "pbr_high": 1.0,
                "pbr_low": 1.0,
                "price_high": price,
                "price_low": price,
                "per_high": per,
                "per_low": per,
            }
        )
    return periods


def test_eps_derived_correctly():
    df = build_period_records(_synthetic_periods_with_eps([100.0, 110.0, 90.0]))
    got = list(df["eps"])
    expected = [100.0, 110.0, 90.0]
    assert all(abs(g - e) < 1e-6 for g, e in zip(got, expected)), got


def test_earnings_anomaly_flag_requires_full_trailing_history():
    """直近5期平均を計算するのに十分な過去期数(5期)が無いうちは、
    どれだけ乖離していても異常フラグを立てないこと（誤検知の防止）。"""
    eps_values = [100.0, 100.0, 100.0, 100.0, 500.0]  # 5期目でいきなり乖離
    df = build_period_records(_synthetic_periods_with_eps(eps_values))
    assert list(df["earnings_anomaly"]) == [False, False, False, False, False]


def test_earnings_anomaly_flag_detects_deviation_over_threshold():
    """過去5期平均(=100)から+30%超乖離(200)した6期目に異常フラグが立つこと。
    5期目までは乖離なし(全て100)なのでフラグは立たない。"""
    eps_values = [100.0, 100.0, 100.0, 100.0, 100.0, 200.0]
    df = build_period_records(_synthetic_periods_with_eps(eps_values))
    assert list(df["earnings_anomaly"]) == [False, False, False, False, False, True]


def test_earnings_anomaly_flag_not_triggered_within_threshold():
    """乖離が±30%以内(例: +20%)なら異常フラグは立たないこと。"""
    eps_values = [100.0, 100.0, 100.0, 100.0, 100.0, 120.0]
    df = build_period_records(_synthetic_periods_with_eps(eps_values))
    assert list(df["earnings_anomaly"]) == [False, False, False, False, False, False]


def test_daily_signal_carries_earnings_anomaly_flag_with_same_lag():
    """compute_daily_signal()が、BPSと同じ開示日ラグでearnings_anomalyフラグを
    ffillすること（異常フラグだけ先読みしてしまうバグの防止）。"""
    eps_values = [100.0, 100.0, 100.0, 100.0, 100.0, 200.0]
    period_df = build_period_records(_synthetic_periods_with_eps(eps_values))
    last_disclosure = period_df.iloc[-1]["disclosure_date"]
    dates = pd.DatetimeIndex(
        [last_disclosure - pd.Timedelta(days=1), last_disclosure]
    )
    price_df = _make_price_df(dates, close_value=1000.0)
    result = compute_daily_signal(price_df, period_df)
    assert result.iloc[0]["earnings_anomaly"] == False  # 前日はまだ前期(異常なし)の情報
    assert result.iloc[1]["earnings_anomaly"] == True  # 開示日当日から異常フラグに切り替わる


def _make_price_df(dates: pd.DatetimeIndex, close_value: float) -> pd.DataFrame:
    return pd.DataFrame({"Close": [close_value] * len(dates)}, index=dates)


def test_no_lookahead_bps_before_disclosure_date():
    """開示日の前日は新しいBPSがまだ使われず、開示日当日から切り替わること
    （先読みバイアス回避、2026-08-07パネルレビュー・CRO指摘の中核）。"""
    raw = _synthetic_periods_raw(6)  # MIN_PERIODS_FOR_SIGNAL(5)を満たすよう6期用意
    period_df = build_period_records(raw)
    last_disclosure = period_df.iloc[-1]["disclosure_date"]

    dates = pd.DatetimeIndex(
        [last_disclosure - pd.Timedelta(days=1), last_disclosure, last_disclosure + pd.Timedelta(days=1)]
    )
    price_df = _make_price_df(dates, close_value=1000.0)
    result = compute_daily_signal(price_df, period_df)

    bps_before = result.iloc[0]["bps"]
    bps_on = result.iloc[1]["bps"]
    bps_after = result.iloc[2]["bps"]

    last_bps = period_df.iloc[-1]["bps"]
    prev_bps = period_df.iloc[-2]["bps"]

    assert bps_before == prev_bps, "開示日前日はまだ前期のBPSを使うべき（最新期を先読みしてはいけない）"
    assert bps_on == last_bps, "開示日当日から最新期のBPSに切り替わるべき"
    assert bps_after == last_bps


def test_range_position_pct_calculation():
    """レンジ位置%の算出式が正しいこと。合成データはPBRが1.0,2.0,...,6.0と単調増加なので、
    直近6期(MIN_PERIODS_FOR_SIGNAL=5以上)の時点でのレンジは[1.0, 6.0]になるはず。
    現在PBRがちょうどレンジ中央(3.5)なら位置は50%。"""
    raw = _synthetic_periods_raw(6)
    period_df = build_period_records(raw)
    last_disclosure = period_df.iloc[-1]["disclosure_date"]
    dates = pd.DatetimeIndex([last_disclosure])

    # BPS=1000（合成データの最新期）なので、current_pbr=3.5にしたければ株価3500にする
    price_df = _make_price_df(dates, close_value=3500.0)
    result = compute_daily_signal(price_df, period_df)

    row = result.iloc[0]
    assert row["range_high"] == 6.0, row["range_high"]
    assert row["range_low"] == 1.0, row["range_low"]
    assert row["current_pbr"] == 3.5, row["current_pbr"]
    assert abs(row["range_position_pct"] - 50.0) < 1e-9, row["range_position_pct"]


def test_insufficient_periods_gives_nan_signal():
    """MIN_PERIODS_FOR_SIGNAL未満の開示済み期数では、シグナルを出さず(NaN)、
    誤った狭いレンジで判定してしまわないこと。"""
    raw = _synthetic_periods_raw(2)  # MIN_PERIODS_FOR_SIGNAL(5)未満
    period_df = build_period_records(raw)
    last_disclosure = period_df.iloc[-1]["disclosure_date"]
    dates = pd.DatetimeIndex([last_disclosure])
    price_df = _make_price_df(dates, close_value=2000.0)
    result = compute_daily_signal(price_df, period_df)
    assert pd.isna(result.iloc[0]["range_position_pct"])


def test_no_disclosed_periods_yet_gives_nan_not_error():
    """最初の開示日より前の日付では、例外を出さずNaNを返すこと。"""
    raw = _synthetic_periods_raw(6)
    period_df = build_period_records(raw)
    first_disclosure = period_df.iloc[0]["disclosure_date"]
    dates = pd.DatetimeIndex([first_disclosure - pd.Timedelta(days=10)])
    price_df = _make_price_df(dates, close_value=1000.0)
    result = compute_daily_signal(price_df, period_df)
    assert pd.isna(result.iloc[0]["range_position_pct"])
    assert pd.isna(result.iloc[0]["bps"])


def test_expanding_percentile_flag_none_before_min_history():
    """min_history未満の観測数のうちは判定不能(None)を返すこと。"""
    values = pd.Series([50.0] * 10)
    flags = compute_expanding_percentile_flag(values, percentile=20.0, min_history=20)
    assert flags.isna().all() or (flags == None).all()  # noqa: E711


def test_expanding_percentile_flag_adapts_to_each_series_own_range():
    """ある銘柄の観測値が常に高水準(50〜90)にとどまる場合でも、その銘柄「自身の」
    下位20パーセンタイルに入った日はTrueになること（絶対値30%以下では一度も
    Trueにならないはずの水準でも、相対的な位置づけでは検出できる）。
    これがNTT/KDDI型の「絶対閾値では一度も拾えない銘柄」問題への対応。"""
    import numpy as np

    rng = np.random.default_rng(42)
    # 50〜90の範囲で分布する合成データ（絶対値30%以下には一度もならない）
    values = pd.Series(rng.uniform(50, 90, size=300))
    flags = compute_expanding_percentile_flag(values, percentile=20.0, min_history=100)

    evaluated = flags[flags.notna()]
    assert len(evaluated) > 0
    true_rate = evaluated.mean()
    # 下位20パーセンタイル判定なので、Trueになる日の割合はおおよそ20%前後になるはず
    # (拡張ウィンドウのため序盤はブレるが、全体としては15〜30%程度に収まるはず)
    assert 0.10 < true_rate < 0.35, f"True率が想定と乖離: {true_rate:.2%}"
    # 絶対値30%以下では一度もTrueにならないはずの合成データでも、Trueになる日が存在すること
    assert evaluated.sum() > 0


def test_expanding_percentile_flag_no_lookahead():
    """拡張ウィンドウのパーセンタイル計算が、未来の値を一切使っていないこと。
    系列の後半にだけ極端に低い値を混ぜても、それより前の日の判定が変わらないこと。"""
    base = pd.Series([50.0] * 300)
    flags_without_future_drop = compute_expanding_percentile_flag(base, percentile=20.0, min_history=100)

    with_future_drop = base.copy()
    with_future_drop.iloc[290:] = 1.0  # 系列の末尾だけ極端に安くする
    flags_with_future_drop = compute_expanding_percentile_flag(with_future_drop, percentile=20.0, min_history=100)

    # 未来の急落が発生する前(0-289日目)の判定は、未来の情報の有無に関わらず同じであるべき
    assert (
        flags_without_future_drop.iloc[:289].tolist() == flags_with_future_drop.iloc[:289].tolist()
    ), "未来の値が過去の判定に影響している（先読みバイアス）"


def _run_all():
    tests = [
        test_build_period_records_derives_bps_correctly,
        test_build_period_records_disclosure_date_has_lag,
        test_missing_pbr_or_price_yields_nan_bps_not_zero,
        test_no_lookahead_bps_before_disclosure_date,
        test_range_position_pct_calculation,
        test_insufficient_periods_gives_nan_signal,
        test_no_disclosed_periods_yet_gives_nan_not_error,
        test_eps_derived_correctly,
        test_earnings_anomaly_flag_requires_full_trailing_history,
        test_earnings_anomaly_flag_detects_deviation_over_threshold,
        test_earnings_anomaly_flag_not_triggered_within_threshold,
        test_daily_signal_carries_earnings_anomaly_flag_with_same_lag,
        test_expanding_percentile_flag_none_before_min_history,
        test_expanding_percentile_flag_adapts_to_each_series_own_range,
        test_expanding_percentile_flag_no_lookahead,
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
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    import sys

    ok = _run_all()
    sys.exit(0 if ok else 1)
