# -*- coding: utf-8 -*-
"""
core16_dividend_bot: bot/irbank_dividend.pyのparse_dividend_table()/
adjust_dividends_for_splits()を合成データで検証する自己テスト。ネットワーク不要。

使い方:
    core16_dividend_botディレクトリで `python test_irbank_dividend_synthetic.py` を実行する。
"""
from __future__ import annotations

import pandas as pd

from bot.irbank_dividend import (
    DividendPeriod,
    IrbankPageStructureError,
    adjust_dividends_for_splits,
    parse_dividend_table,
)

# JT(2914)の実ページ構造を模した合成HTML（2026-08-07確認: 1テーブルのみ、
# ヘッダーは1行、区分列(実績/予想/修正)は判別に使わない設計）
_SYNTHETIC_DIVIDEND_HTML = """
<html><body>
<table class="bar">
<tr><th>年度</th><th>区分</th><th>中間</th><th>期末</th><th>合計</th><th>配当利回り</th><th>備考</th></tr>
<tr><td>2010年3月</td><td>実績</td><td>2,800</td><td>-</td><td>5,800</td><td>1.67%</td><td>-</td></tr>
<tr><td>2011年3月</td><td>実績</td><td>2,800</td><td>-</td><td>6,800</td><td>2.26%</td><td>-</td></tr>
<tr><td>2012年3月</td><td>実績</td><td>4,000</td><td>-</td><td>10,000</td><td>2.15%</td><td>-</td></tr>
<tr><td>2013年3月</td><td>実績</td><td>30</td><td>38</td><td>68</td><td>2.27%</td><td>-</td></tr>
<tr><td>2014年12月</td><td>予想</td><td>50</td><td>50</td><td>100</td><td>3%</td><td>-</td></tr>
</table>
</body></html>
"""


def test_parses_expected_period_count():
    periods = parse_dividend_table(_SYNTHETIC_DIVIDEND_HTML)
    assert len(periods) == 5, len(periods)


def test_fiscal_year_label_has_no_trailing_ki_suffix():
    """配当テーブルの年度ラベルはPBRテーブルと違い「期」が付かない（"2010年3月"であり
    "2010年3月期"ではない）。正規表現がこの形式に対応していること。"""
    periods = parse_dividend_table(_SYNTHETIC_DIVIDEND_HTML)
    assert periods[0].fiscal_year_label == "2010年3月"
    assert periods[0].end_year == 2010
    assert periods[0].end_month == 3


def test_dps_total_parsed_correctly_ignoring_classification_label():
    """「実績」「予想」の区分に関わらず、合計配当額が同じロジックでパースされること
    （区分ラベルの意味が不明瞭なため、このモジュールはラベルに依存しない設計）。"""
    periods = parse_dividend_table(_SYNTHETIC_DIVIDEND_HTML)
    assert periods[0].dps_total == 5800.0
    assert periods[-1].dps_total == 100.0  # 「予想」ラベルでも同様に取得される


_SYNTHETIC_DIVIDEND_HTML_WITH_EXTRA_COLUMN = """
<html><body>
<table class="bar">
<tr><th>年度</th><th>区分</th><th>中間</th><th>期末</th><th>合計</th><th>分割調整</th><th>配当利回り</th><th>備考</th></tr>
<tr><td>2009年3月</td><td>実績</td><td>28</td><td>-</td><td>56</td><td>28</td><td>2.92%</td><td>#1</td></tr>
<tr><td>2010年3月</td><td>実績</td><td>28</td><td>-</td><td>57</td><td>28.5</td><td>2.41%</td><td>#1</td></tr>
</table>
</body></html>
"""


def test_tolerates_extra_columns_after_gokei_e_g_bunkatsu_chosei():
    """花王(4452)等、"合計"の後に「分割調整」列が追加される銘柄があることを2026-08-07に
    確認済み。ヘッダーの完全一致ではなく先頭5列の一致で判定し、末尾列数の違いに
    影響されず年度・合計配当が正しく取れること。"""
    periods = parse_dividend_table(_SYNTHETIC_DIVIDEND_HTML_WITH_EXTRA_COLUMN)
    assert len(periods) == 2, len(periods)
    assert periods[0].dps_total == 56.0
    assert periods[1].dps_total == 57.0


def test_raises_on_missing_expected_table():
    broken_html = "<html><body><table><tr><th>不明な列</th></tr></table></body></html>"
    try:
        parse_dividend_table(broken_html)
        raised = False
    except IrbankPageStructureError:
        raised = True
    assert raised


def test_split_adjustment_scales_pre_split_dividends_down():
    """2012年7月の1:200分割を境に、分割前(2010-2012年3月期)の配当を200で割って
    現在の株式数ベースに揃えること。分割後(2013年3月期以降)はそのまま。"""
    periods = parse_dividend_table(_SYNTHETIC_DIVIDEND_HTML)
    splits = pd.Series(
        {pd.Timestamp("2012-06-27"): 200.0},
    )
    df = adjust_dividends_for_splits(periods, splits)

    row_2010 = df[df["fiscal_year_label"] == "2010年3月"].iloc[0]
    row_2013 = df[df["fiscal_year_label"] == "2013年3月"].iloc[0]

    assert abs(row_2010["dps_adjusted"] - 5800.0 / 200.0) < 1e-9, row_2010["dps_adjusted"]
    assert row_2013["dps_adjusted"] == 68.0  # 分割後なので調整不要


def test_split_adjustment_smooths_out_the_artificial_cliff():
    """調整前は5800→6800→10000→68円という激減に見えるが、調整後は
    連続的な推移（分割前の値を200で割った値が分割後の値と近い水準）になること。
    これが今回のダイビデンド分析の目的（見かけの減配アーティファクトの除去）。"""
    periods = parse_dividend_table(_SYNTHETIC_DIVIDEND_HTML)
    splits = pd.Series({pd.Timestamp("2012-06-27"): 200.0})
    df = adjust_dividends_for_splits(periods, splits)

    adjusted_2012 = df[df["fiscal_year_label"] == "2012年3月"].iloc[0]["dps_adjusted"]
    adjusted_2013 = df[df["fiscal_year_label"] == "2013年3月"].iloc[0]["dps_adjusted"]
    # 調整前は10000→68で激減(99%減)に見えるが、調整後は 10000/200=50 → 68 で連続的
    assert adjusted_2012 == 50.0
    assert adjusted_2013 == 68.0
    # 調整後の変化率が妥当な範囲(±50%以内)であること。調整前は-99.3%という壊滅的な値になる
    pct_change = abs(adjusted_2013 - adjusted_2012) / adjusted_2012
    assert pct_change < 0.5, f"分割調整後もまだ激変して見える: {pct_change:.1%}"


def test_no_split_events_leaves_values_unchanged():
    periods = parse_dividend_table(_SYNTHETIC_DIVIDEND_HTML)
    splits = pd.Series(dtype=float)
    df = adjust_dividends_for_splits(periods, splits)
    for p, (_, row) in zip(periods, df.iterrows()):
        assert row["dps_adjusted"] == p.dps_total


def test_missing_dividend_value_becomes_none_not_zero():
    html = """
    <table class="bar">
    <tr><th>年度</th><th>区分</th><th>中間</th><th>期末</th><th>合計</th><th>配当利回り</th><th>備考</th></tr>
    <tr><td>2020年3月</td><td>実績</td><td>-</td><td>-</td><td>-</td><td>-</td><td>-</td></tr>
    </table>
    """
    periods = parse_dividend_table(html)
    assert periods[0].dps_total is None


def _run_all():
    tests = [
        test_parses_expected_period_count,
        test_fiscal_year_label_has_no_trailing_ki_suffix,
        test_dps_total_parsed_correctly_ignoring_classification_label,
        test_tolerates_extra_columns_after_gokei_e_g_bunkatsu_chosei,
        test_raises_on_missing_expected_table,
        test_split_adjustment_scales_pre_split_dividends_down,
        test_split_adjustment_smooths_out_the_artificial_cliff,
        test_no_split_events_leaves_values_unchanged,
        test_missing_dividend_value_becomes_none_not_zero,
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
