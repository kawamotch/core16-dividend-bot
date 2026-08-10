# -*- coding: utf-8 -*-
"""
core16_dividend_bot: bot/irbank_pbr_range.pyのparse_pbr_table()を合成HTMLで検証する自己テスト。

ネットワークアクセス不要。実行前にこのテストが全件合格することを確認してから、
実データでのfetch_pbr_range_data.py本実行に進む
（CLAUDE.md「合成データでのロジック自己テスト→実データでの本実行」の原則）。

使い方:
    core16_dividend_botディレクトリで `python test_irbank_pbr_range_synthetic.py` を実行する。
"""
from __future__ import annotations

from bot.irbank_pbr_range import IrbankPageStructureError, parse_pbr_table

# 実際のIRBANK個別銘柄ページ(/{code}/pbr)の年度テーブル部分を模した合成HTML。
# 2026-08-07にJT(2914)の実ページで確認した実際の構造を踏襲する:
# - テーブル数2、対象は2番目、ヘッダー2行＋データ行11列＋末尾に列数の異なる「最新」行
# - 株価高値・安値セルは<span class="text">の中に「株価／出来高／日付」が入れ子で
#   詰め込まれており、株価は最初の子<span>のみに入っている
#   （例: <span class="text"><span class="co_red">1,790</span><span class="co_sm"><br/>358,000<br/>1/13</span></span>）
_SYNTHETIC_PAGE_HTML = """
<html><body>
<table class="bar">
<tr><th>日付</th><th>始値</th><th>高値</th><th>安値</th><th>終値</th></tr>
<tr><td>2026/08/06</td><td>6,943</td><td>7,063</td><td>6,893</td><td>7,030</td></tr>
</table>
<table class="bar">
<tr><th>年度</th><th>株価</th><th>出来高</th><th>PER</th><th>PBR</th><th>時価総額</th><th>期末</th></tr>
<tr><th>高値</th><th>安値</th><th>大商い</th><th>高値</th><th>安値</th><th>高値</th><th>安値</th><th>高値</th><th>安値</th><th>PBR</th></tr>
<tr>
  <td>2010年<br/>3月期</td>
  <td><span class="text"><span class="co_red">1,790</span><span class="co_sm"><br/>358,000<br/>1/13</span></span></td>
  <td><span class="text"><span class="co_br">1,135</span><span class="co_sm"><br/>227,000<br/>5/7</span></span></td>
  <td>23,815,800</td>
  <td>25.86</td><td>16.4</td><td>1.73</td><td>1.1</td><td>-</td><td>-</td><td>1.68倍3/31</td>
</tr>
<tr>
  <td>2011年<br/>3月期</td>
  <td><span class="text"><span class="co_red">1,760</span><span class="co_sm"><br/>352,000<br/>2/17</span></span></td>
  <td><span class="text"><span class="co_br">1,220</span><span class="co_sm"><br/>243,900<br/>10/29</span></span></td>
  <td>14,375,400</td>
  <td>14.47</td><td>10.02</td><td>1.65</td><td>1.14</td><td>3兆5200億</td><td>2兆4390億</td><td>1.41倍3/31</td>
</tr>
<tr>
  <td>2012年<br/>3月期</td>
  <td><span class="text"><span class="co_red">2,373</span><span class="co_sm"><br/>474,500<br/>3/27</span></span></td>
  <td><span class="text"><span class="co_br">1,413</span><span class="co_sm"><br/>282,600<br/>6/20</span></span></td>
  <td>19,881,200</td>
  <td>14.79</td><td>8.81</td><td>-</td><td>-</td><td>4兆7450億</td><td>2兆8260億</td><td>-</td>
</tr>
<tr>
  <td>2014年<br/>12月期</td>
  <td><span class="text"><span class="co_red">2,500</span><span class="co_sm"><br/>500,000<br/>1/1</span></span></td>
  <td><span class="text"><span class="co_br">1,800</span><span class="co_sm"><br/>400,000<br/>12/1</span></span></td>
  <td>10,000,000</td>
  <td>20.0</td><td>15.0</td><td>3.0</td><td>2.04</td><td>5兆</td><td>4兆</td><td>2.5倍12/31</td>
</tr>
<tr>
  <td>最新</td><td>7,0302026/8/6</td><td>4,858,200</td><td>19.38予想</td><td>2.84実績</td><td>14兆600億</td><td>-</td>
</tr>
</table>
</body></html>
"""


def test_parses_expected_period_count():
    periods = parse_pbr_table(_SYNTHETIC_PAGE_HTML)
    # 「最新」行(7列)は列数不一致のため除外され、4期分のみパースされるはず
    assert len(periods) == 4, f"期待した期数(4)と不一致: {len(periods)}"


def test_parses_fiscal_year_and_month_correctly():
    periods = parse_pbr_table(_SYNTHETIC_PAGE_HTML)
    labels = [(p.end_year, p.end_month) for p in periods]
    assert labels == [(2010, 3), (2011, 3), (2012, 3), (2014, 12)], labels


def test_parses_pbr_high_low_values_correctly():
    periods = parse_pbr_table(_SYNTHETIC_PAGE_HTML)
    p0 = periods[0]
    assert p0.pbr_high == 1.73, p0.pbr_high
    assert p0.pbr_low == 1.1, p0.pbr_low
    p_last = periods[-1]
    assert p_last.pbr_high == 3.0, p_last.pbr_high
    assert p_last.pbr_low == 2.04, p_last.pbr_low


def test_parses_price_high_low_without_volume_date_contamination():
    """株価高値・安値セルは同じ<span>内に出来高・日付も詰め込まれているため、
    単純にget_text()すると"1,790358,0001/13"のように連結されてしまう。
    株価の数値だけを正しく取り出せていること（カンマ除去も含む）。"""
    periods = parse_pbr_table(_SYNTHETIC_PAGE_HTML)
    p0 = periods[0]
    assert p0.price_high == 1790.0, p0.price_high
    assert p0.price_low == 1135.0, p0.price_low
    p_last = periods[-1]
    assert p_last.price_high == 2500.0, p_last.price_high
    assert p_last.price_low == 1800.0, p_last.price_low


def test_parses_per_high_low_for_eps_derivation():
    """PER高値・安値も正しく取れていること（業績正常化チェックのEPS逆算に使う）。"""
    periods = parse_pbr_table(_SYNTHETIC_PAGE_HTML)
    p0 = periods[0]
    assert p0.per_high == 25.86, p0.per_high
    assert p0.per_low == 16.4, p0.per_low


def test_missing_pbr_value_becomes_none_not_zero():
    """PBRが「-」(データ欠損、上場直後等)の期は、0扱いせずNoneにすること
    （0扱いするとレンジ計算で誤った最安値として紛れ込む）。"""
    periods = parse_pbr_table(_SYNTHETIC_PAGE_HTML)
    p2012 = next(p for p in periods if p.end_year == 2012)
    assert p2012.pbr_high is None, p2012.pbr_high
    assert p2012.pbr_low is None, p2012.pbr_low


def test_latest_row_excluded_not_misparsed():
    """列数の異なる「最新」行が、誤って期次データとして紛れ込んでいないこと。"""
    periods = parse_pbr_table(_SYNTHETIC_PAGE_HTML)
    assert all(p.fiscal_year_label != "最新" for p in periods)


def test_raises_on_structure_change_missing_header():
    """想定するヘッダー行を持つtableが無い場合、0件を静かに返さず例外を送出すること
    （2026-08-07パネルレビュー・SRE指摘: サイレント欠損の禁止）。"""
    broken_html = "<html><body><table><tr><th>不明な列</th></tr></table></body></html>"
    try:
        parse_pbr_table(broken_html)
        raised = False
    except IrbankPageStructureError:
        raised = True
    assert raised, "構造不一致で例外が送出されるべき"


def test_raises_when_table_found_but_no_data_rows_parsed():
    """ヘッダーは一致するが年度ラベルが1件も正規表現にマッチしない場合も例外を送出すること。"""
    html = """
    <table class="bar">
    <tr><th>年度</th><th>株価</th><th>出来高</th><th>PER</th><th>PBR</th><th>時価総額</th><th>期末</th></tr>
    <tr><th>高値</th><th>安値</th><th>大商い</th><th>高値</th><th>安値</th><th>高値</th><th>安値</th><th>高値</th><th>安値</th><th>PBR</th></tr>
    <tr><td>不明な年度</td><td>a</td><td>b</td><td>c</td><td>d</td><td>e</td><td>f</td><td>g</td><td>h</td><td>i</td><td>j</td></tr>
    </table>
    """
    try:
        parse_pbr_table(html)
        raised = False
    except IrbankPageStructureError:
        raised = True
    assert raised, "データ行0件で例外が送出されるべき"


def _run_all():
    tests = [
        test_parses_expected_period_count,
        test_parses_fiscal_year_and_month_correctly,
        test_parses_pbr_high_low_values_correctly,
        test_parses_price_high_low_without_volume_date_contamination,
        test_parses_per_high_low_for_eps_derivation,
        test_missing_pbr_value_becomes_none_not_zero,
        test_latest_row_excluded_not_misparsed,
        test_raises_on_structure_change_missing_header,
        test_raises_when_table_found_but_no_data_rows_parsed,
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
