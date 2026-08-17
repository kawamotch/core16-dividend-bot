# -*- coding: utf-8 -*-
"""
core16_dividend_bot: bot/edinet_financials.pyのXBRLパース関数群を合成XBRLで検証する自己テスト。

ネットワークアクセス不要。実行前にこのテストが全件合格することを確認してから、
実データでのfetch_edinet_financial_data.py本実行（Phase B）に進む
（CLAUDE.md「合成データでのロジック自己テスト→実データでの本実行」の原則）。

合成XBRLの構造は、EDINET実現可能性検証（2026-08-14、JT/2914の実データ確認）で判明した
タグ名・context命名規則（CurrentYearInstant/Prior1YearInstant等）を踏襲する。

使い方:
    core16_dividend_botディレクトリで `python test_edinet_financials_synthetic.py` を実行する。
"""
from __future__ import annotations

import io
import zipfile
from datetime import date

from bot.edinet_financials import (
    CONSOLIDATION_METHOD_BY_CODE,
    CONSOLIDATION_METHOD_IFRS_COMPUTED,
    CONSOLIDATION_METHOD_JGAAP_BARE,
    CONSOLIDATION_METHOD_TRANSITIONS_BY_CODE,
    CONSOLIDATION_METHOD_USGAAP_BARE,
    PER_YEAR_FETCH_CODES,
    EdinetXbrlStructureError,
    extract_facts_by_tag,
    find_xbrl_instance_in_zip,
    parse_financial_periods,
    parse_xbrl_contexts,
)
from bot.edinet_financials import (
    _extract_issued_shares_from_html,
    _extract_parent_equity_from_html,
    _extract_treasury_shares_from_html,
    _find_table_row_value,
)

# テスト用に実銘柄コードを流用する（CONSOLIDATION_METHOD_BY_CODEに実在登録済みのコードでないと
# parse_financial_periods()が「未登録」エラーを出すため）。値は架空、実在企業の数値ではない。
_TEST_CODE_JGAAP = "8306"  # MUFG（JGAAP方式）
_TEST_CODE_JGAAP_PER_YEAR = "8316"  # SMFG（JGAAP方式だがPER_YEAR_FETCH_CODES対象）
_TEST_CODE_IFRS = "2914"  # JT（IFRS方式）
_TEST_CODE_USGAAP = "6301"  # コマツ（US GAAP方式）
_TEST_CODE_TRANSITION = "8766"  # 東京海上HD（2026年3月期からJGAAP→IFRSへ移行）

# JTのような12月決算・年2回配当（中間・期末）銘柄を模した、過去5期分(Current〜Prior4Year)の
# 合成XBRLインスタンス。実際のEDINET XBRLの命名規則（xbrli名前空間、instant/duration context、
# jpcrp_cor名前空間の要素）を踏襲する。値は架空のもの。
_SYNTHETIC_XBRL = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-11-01/jpcrp_cor">
  <xbrli:context id="CurrentYearInstant">
    <xbrli:period><xbrli:instant>2023-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="Prior1YearInstant">
    <xbrli:period><xbrli:instant>2022-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="Prior2YearInstant">
    <xbrli:period><xbrli:instant>2021-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="Prior3YearInstant">
    <xbrli:period><xbrli:instant>2020-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="Prior4YearInstant">
    <xbrli:period><xbrli:instant>2019-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>

  <xbrli:context id="CurrentYearDuration">
    <xbrli:period><xbrli:startDate>2023-01-01</xbrli:startDate><xbrli:endDate>2023-12-31</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <xbrli:context id="Prior1YearDuration">
    <xbrli:period><xbrli:startDate>2022-01-01</xbrli:startDate><xbrli:endDate>2022-12-31</xbrli:endDate></xbrli:period>
  </xbrli:context>

  <jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults contextRef="CurrentYearInstant" unitRef="JPYPerShares" decimals="2">3456.78</jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults>
  <jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults contextRef="Prior1YearInstant" unitRef="JPYPerShares" decimals="2">3200.00</jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults>
  <jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults contextRef="Prior2YearInstant" unitRef="JPYPerShares" decimals="2">3000.50</jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults>
  <jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults contextRef="Prior3YearInstant" unitRef="JPYPerShares" decimals="2">2800.00</jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults>
  <jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults contextRef="Prior4YearInstant" unitRef="JPYPerShares" decimals="2">2650.25</jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults>

  <jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPYPerShares" decimals="2">154.00</jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults>
  <jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults contextRef="Prior1YearDuration" unitRef="JPYPerShares" decimals="2">140.00</jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults>

  <jpcrp_cor:InterimDividendPaidPerShareSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPYPerShares" decimals="2">75.00</jpcrp_cor:InterimDividendPaidPerShareSummaryOfBusinessResults>
  <jpcrp_cor:InterimDividendPaidPerShareSummaryOfBusinessResults contextRef="Prior1YearDuration" unitRef="JPYPerShares" decimals="2">68.00</jpcrp_cor:InterimDividendPaidPerShareSummaryOfBusinessResults>
</xbrli:xbrl>
"""

# BVPSタグが存在しない（IFRS採用等でタグ名が異なるケースを模した）合成XBRL
_SYNTHETIC_XBRL_MISSING_BVPS = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-11-01/jpcrp_cor">
  <xbrli:context id="CurrentYearDuration">
    <xbrli:period><xbrli:startDate>2023-01-01</xbrli:startDate><xbrli:endDate>2023-12-31</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPYPerShares" decimals="2">154.00</jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults>
</xbrli:xbrl>
"""

# 2026-08-16発覚バグの再現用: NetAssetsPerShareSummaryOfBusinessResultsが
# 非連結(_NonConsolidatedMember)のcontextでしか存在しないケース（bare/連結contextが無い）。
# 実際にJT(2914)等13銘柄でこの状態だった（IFRS方式が必要な銘柄で、JGAAP方式のタグ自体は
# 非連結分しか収録されていなかった）。
_SYNTHETIC_XBRL_NONCONSOLIDATED_ONLY = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-11-01/jpcrp_cor">
  <xbrli:context id="CurrentYearInstant_NonConsolidatedMember">
    <xbrli:period><xbrli:instant>2023-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="CurrentYearDuration">
    <xbrli:period><xbrli:startDate>2023-01-01</xbrli:startDate><xbrli:endDate>2023-12-31</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults contextRef="CurrentYearInstant_NonConsolidatedMember" unitRef="JPYPerShares" decimals="2">753.52</jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults>
  <jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPYPerShares" decimals="2">154.00</jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults>
</xbrli:xbrl>
"""

# bare(連結)と非連結(_NonConsolidatedMember)の両方が同居するケース。bare側が選ばれることを確認する。
_SYNTHETIC_XBRL_BARE_AND_NONCONSOLIDATED_MIXED = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-11-01/jpcrp_cor">
  <xbrli:context id="CurrentYearInstant">
    <xbrli:period><xbrli:instant>2023-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="CurrentYearInstant_NonConsolidatedMember">
    <xbrli:period><xbrli:instant>2023-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="CurrentYearDuration">
    <xbrli:period><xbrli:startDate>2023-01-01</xbrli:startDate><xbrli:endDate>2023-12-31</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults contextRef="CurrentYearInstant" unitRef="JPYPerShares" decimals="2">1973.30</jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults>
  <jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults contextRef="CurrentYearInstant_NonConsolidatedMember" unitRef="JPYPerShares" decimals="2">764.77</jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults>
  <jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPYPerShares" decimals="2">154.00</jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults>
</xbrli:xbrl>
"""

# IFRS方式の合成XBRL（親会社株主帰属持分・発行済株式数・自己株式数から自前計算する経路）。
# 数値はJTの実データ規模感を模した架空値。期待されるBVPS = 4086933000000 / (2000000000 - 224199500)
# = 4086933000000 / 1775800500 ≈ 2301.59
_SYNTHETIC_XBRL_IFRS = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-11-01/jpcrp_cor">
  <xbrli:context id="CurrentYearInstant">
    <xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="CurrentYearInstant_NonConsolidatedMember">
    <xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="CurrentYearDuration">
    <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <jpcrp_cor:EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults contextRef="CurrentYearInstant" unitRef="JPY" decimals="-6">4086933000000</jpcrp_cor:EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults>
  <jpcrp_cor:NumberOfSharesIssuedSharesVotingRights contextRef="CurrentYearInstant" unitRef="shares" decimals="0">2000000000</jpcrp_cor:NumberOfSharesIssuedSharesVotingRights>
  <jpcrp_cor:TotalNumberOfSharesHeldTreasurySharesEtc contextRef="CurrentYearInstant" unitRef="shares" decimals="0">224199500</jpcrp_cor:TotalNumberOfSharesHeldTreasurySharesEtc>
  <!-- IFRS採用企業でも非連結(提出会社)のNetAssetsPerShareタグは別途存在しうる。
       これに誤って引っ張られないことを確認するためのダミー(値は明らかに異なるものにする)。 -->
  <jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults contextRef="CurrentYearInstant_NonConsolidatedMember" unitRef="JPYPerShares" decimals="2">753.52</jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults>
  <jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPYPerShares" decimals="2">234.00</jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults>
</xbrli:xbrl>
"""

# US GAAP方式の合成XBRL（コマツのように1株当たり値の完成品タグが直接存在し、
# Prior1〜4Yearの5期分時系列も持つケース。2026-08-16実データ取得で発覚した3つ目のパターン）。
_SYNTHETIC_XBRL_USGAAP = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-11-01/jpcrp_cor">
  <xbrli:context id="CurrentYearInstant">
    <xbrli:period><xbrli:instant>2023-03-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="Prior1YearInstant">
    <xbrli:period><xbrli:instant>2022-03-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="CurrentYearInstant_NonConsolidatedMember">
    <xbrli:period><xbrli:instant>2023-03-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="CurrentYearDuration">
    <xbrli:period><xbrli:startDate>2022-04-01</xbrli:startDate><xbrli:endDate>2023-03-31</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <jpcrp_cor:EquityAttributableToOwnersOfParentPerShareUSGAAPSummaryOfBusinessResults contextRef="CurrentYearInstant" unitRef="JPYPerShares" decimals="2">3896.10</jpcrp_cor:EquityAttributableToOwnersOfParentPerShareUSGAAPSummaryOfBusinessResults>
  <jpcrp_cor:EquityAttributableToOwnersOfParentPerShareUSGAAPSummaryOfBusinessResults contextRef="Prior1YearInstant" unitRef="JPYPerShares" decimals="2">3438.70</jpcrp_cor:EquityAttributableToOwnersOfParentPerShareUSGAAPSummaryOfBusinessResults>
  <!-- 単体(非連結)のNetAssetsPerShareタグが同居していても引っ張られないことを確認するダミー -->
  <jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults contextRef="CurrentYearInstant_NonConsolidatedMember" unitRef="JPYPerShares" decimals="2">937.51</jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults>
  <jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPYPerShares" decimals="2">156.00</jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults>
</xbrli:xbrl>
"""

# 会計基準の期中移行（東京海上HDのJGAAP→IFRS、2026-08-17発覚）を模した合成XBRL。
# 実データ(doc S100YLS8)を踏襲: 移行後もJGAAPのbareタグは値を持ち続けるが誤った値
# （2885.44円、実勢の-33%）で、同一書類に他12銘柄と同じIFRS方式タグも別途存在し、
# そちらから計算した値が正しい（実データで検算済み、Yahoo!ファイナンス逆算値との差-1.8%）。
_SYNTHETIC_XBRL_TRANSITION_AFTER = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-11-01/jpcrp_cor">
  <xbrli:context id="CurrentYearInstant">
    <xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="CurrentYearDuration">
    <xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <!-- 移行後も値を持ち続けるが、もはや正しい連結BVPSを表さない旧JGAAPタグ -->
  <jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults contextRef="CurrentYearInstant" unitRef="JPYPerShares" decimals="2">2885.44</jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults>
  <!-- 他12銘柄と同じIFRS方式タグ。こちらが正しい値の計算に使われるべき -->
  <jpcrp_cor:EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults contextRef="CurrentYearInstant" unitRef="JPY" decimals="-6">7955554000000</jpcrp_cor:EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults>
  <jpcrp_cor:NumberOfSharesIssuedSharesVotingRights contextRef="CurrentYearInstant" unitRef="shares" decimals="0">1934000000</jpcrp_cor:NumberOfSharesIssuedSharesVotingRights>
  <jpcrp_cor:TotalNumberOfSharesHeldTreasurySharesEtc contextRef="CurrentYearInstant" unitRef="shares" decimals="0">53937600</jpcrp_cor:TotalNumberOfSharesHeldTreasurySharesEtc>
  <jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPYPerShares" decimals="2">120.00</jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults>
</xbrli:xbrl>
"""

# 移行日より前（2025年3月期、まだJGAAPが主基準）の合成XBRL。IFRS方式タグは存在しない
# （移行前の実データ同様）。従来通りJGAAPのbare値がそのまま使われることを確認する。
_SYNTHETIC_XBRL_TRANSITION_BEFORE = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-11-01/jpcrp_cor">
  <xbrli:context id="CurrentYearInstant">
    <xbrli:period><xbrli:instant>2025-03-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="CurrentYearDuration">
    <xbrli:period><xbrli:startDate>2024-04-01</xbrli:startDate><xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults contextRef="CurrentYearInstant" unitRef="JPYPerShares" decimals="2">2640.27</jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults>
  <jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPYPerShares" decimals="2">115.00</jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults>
</xbrli:xbrl>
"""

# 2018年頃までの古い提出分を模した合成XBRL（構造化タグ無し、HTML(TextBlock)のみで
# 発行済株式数・自己株式数・親会社の所有者に帰属する持分を開示するケース。テーブル構造は
# 実データ（三菱商事2018年3月期・JT2018年12月期）で確認済みの形式を踏襲する。
# 実際のEDINET XBRLはTextBlock内のHTMLをXMLエンティティエスケープ（&lt;table&gt;等）した
# 文字列としてelem.textに格納する（生の子要素としては埋め込まない）ため、合成データも
# 同じ形式（エスケープ済みHTML文字列）で再現する。
_SYNTHETIC_XBRL_IFRS_OLD_FILING_HTML_ONLY = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2018-02-28/jpcrp_cor">
  <xbrli:context id="CurrentYearInstant">
    <xbrli:period><xbrli:instant>2018-03-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="CurrentYearDuration">
    <xbrli:period><xbrli:startDate>2017-04-01</xbrli:startDate><xbrli:endDate>2018-03-31</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <jpcrp_cor:IssuedSharesTotalNumberOfSharesEtcTextBlock contextRef="FilingDateInstant">&lt;table&gt;
      &lt;tr&gt;&lt;td&gt;種類&lt;/td&gt;&lt;td&gt;事業年度末現在発行数(株)(平成30年3月31日)&lt;/td&gt;&lt;td&gt;提出日現在発行数(株)&lt;/td&gt;&lt;/tr&gt;
      &lt;tr&gt;&lt;td&gt;普通株式&lt;/td&gt;&lt;td&gt;1,590,076,851&lt;/td&gt;&lt;td&gt;1,590,076,851&lt;/td&gt;&lt;/tr&gt;
      &lt;tr&gt;&lt;td&gt;計&lt;/td&gt;&lt;td&gt;1,590,076,851&lt;/td&gt;&lt;td&gt;1,590,076,851&lt;/td&gt;&lt;/tr&gt;
    &lt;/table&gt;</jpcrp_cor:IssuedSharesTotalNumberOfSharesEtcTextBlock>
  <jpcrp_cor:DisposalsOrHoldingOfAcquiredTreasurySharesTextBlock contextRef="FilingDateInstant">&lt;table&gt;
      &lt;tr&gt;&lt;td&gt;区分&lt;/td&gt;&lt;td&gt;当事業年度&lt;/td&gt;&lt;td&gt;当期間&lt;/td&gt;&lt;/tr&gt;
      &lt;tr&gt;&lt;td&gt;保有自己株式数&lt;/td&gt;&lt;td&gt;4,107,848&lt;/td&gt;&lt;td&gt;3,959,088&lt;/td&gt;&lt;/tr&gt;
    &lt;/table&gt;</jpcrp_cor:DisposalsOrHoldingOfAcquiredTreasurySharesTextBlock>
  <jpcrp_cor:ConsolidatedStatementOfFinancialPositionIFRSTextBlock contextRef="CurrentYearInstant">&lt;table&gt;
      &lt;tr&gt;&lt;td&gt;親会社の所有者に帰属する持分&lt;/td&gt;&lt;td&gt;&lt;/td&gt;&lt;td&gt;2,761,687&lt;/td&gt;&lt;td&gt;&lt;/td&gt;&lt;td&gt;2,630,594&lt;/td&gt;&lt;/tr&gt;
      &lt;tr&gt;&lt;td&gt;非支配持分&lt;/td&gt;&lt;td&gt;&lt;/td&gt;&lt;td&gt;80,340&lt;/td&gt;&lt;td&gt;&lt;/td&gt;&lt;td&gt;69,851&lt;/td&gt;&lt;/tr&gt;
    &lt;/table&gt;</jpcrp_cor:ConsolidatedStatementOfFinancialPositionIFRSTextBlock>
  <jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPYPerShares" decimals="2">140.00</jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults>
</xbrli:xbrl>
"""

# 自己株式数タグ自体が存在しない年（実質ゼロとみなすべきケース、2026-08-16 JPX(8697)で発見）。
_SYNTHETIC_XBRL_IFRS_NO_TREASURY_TAG = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-11-01/jpcrp_cor">
  <xbrli:context id="CurrentYearInstant">
    <xbrli:period><xbrli:instant>2024-03-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="CurrentYearDuration">
    <xbrli:period><xbrli:startDate>2023-04-01</xbrli:startDate><xbrli:endDate>2024-03-31</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <jpcrp_cor:EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults contextRef="CurrentYearInstant" unitRef="JPY" decimals="-6">345015000000</jpcrp_cor:EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults>
  <jpcrp_cor:NumberOfSharesIssuedSharesVotingRights contextRef="CurrentYearInstant" unitRef="shares" decimals="0">522289183</jpcrp_cor:NumberOfSharesIssuedSharesVotingRights>
  <!-- TotalNumberOfSharesHeldTreasurySharesEtc タグ自体が存在しない（省略） -->
  <jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPYPerShares" decimals="2">18.00</jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults>
</xbrli:xbrl>
"""

# IFRS方式・フォールバックタグ経路の合成XBRL（JT(2914)のように"...SummaryOfBusinessResults"版
# タグを持たず、サフィックス無しのEquityAttributableToOwnersOfParentIFRSタグしか無いケース。
# 2026-08-16、実データ取得でJTが全期間このパターンだったことが発覚）。
_SYNTHETIC_XBRL_IFRS_FALLBACK_TAG = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2024-11-01/jpcrp_cor">
  <xbrli:context id="CurrentYearInstant">
    <xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="CurrentYearDuration">
    <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <jpcrp_cor:EquityAttributableToOwnersOfParentIFRS contextRef="CurrentYearInstant" unitRef="JPY" decimals="-6">4086933000000</jpcrp_cor:EquityAttributableToOwnersOfParentIFRS>
  <jpcrp_cor:NumberOfSharesIssuedSharesVotingRights contextRef="CurrentYearInstant" unitRef="shares" decimals="0">2000000000</jpcrp_cor:NumberOfSharesIssuedSharesVotingRights>
  <jpcrp_cor:TotalNumberOfSharesHeldTreasurySharesEtc contextRef="CurrentYearInstant" unitRef="shares" decimals="0">224199500</jpcrp_cor:TotalNumberOfSharesHeldTreasurySharesEtc>
  <jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPYPerShares" decimals="2">234.00</jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults>
</xbrli:xbrl>
"""


def test_parses_contexts_instant_and_duration():
    contexts = parse_xbrl_contexts(_SYNTHETIC_XBRL)
    assert contexts["CurrentYearInstant"] == date(2023, 12, 31), contexts["CurrentYearInstant"]
    assert contexts["Prior4YearInstant"] == date(2019, 12, 31), contexts["Prior4YearInstant"]
    assert contexts["CurrentYearDuration"] == date(2023, 12, 31), contexts["CurrentYearDuration"]


def test_extracts_bvps_facts_sorted_by_period_end():
    contexts = parse_xbrl_contexts(_SYNTHETIC_XBRL)
    facts = extract_facts_by_tag(_SYNTHETIC_XBRL, "NetAssetsPerShareSummaryOfBusinessResults", contexts)
    assert len(facts) == 5, f"5期分のBVPSが取れるはず: {len(facts)}"
    # 昇順（最古が先頭）であること
    assert facts[0].period_end == date(2019, 12, 31), facts[0].period_end
    assert facts[-1].period_end == date(2023, 12, 31), facts[-1].period_end
    assert facts[-1].value == 3456.78, facts[-1].value


def test_parse_financial_periods_returns_three_series():
    result = parse_financial_periods(_SYNTHETIC_XBRL, _TEST_CODE_JGAAP)
    assert len(result["bvps"]) == 5, result["bvps"]
    assert len(result["dps_total"]) == 2, result["dps_total"]
    assert len(result["dps_interim"]) == 2, result["dps_interim"]


def test_dps_interim_values_correct():
    """中間配当が独立タグから正しく取れること（2026-08-14引き継ぎメモの、分割またぎ根本修正
    実装で必要になる中間・期末分離データの前提）。"""
    result = parse_financial_periods(_SYNTHETIC_XBRL, _TEST_CODE_JGAAP)
    current = next(f for f in result["dps_interim"] if f.period_end == date(2023, 12, 31))
    assert current.value == 75.00, current.value


def test_dps_total_is_full_year_not_interim():
    """DividendPaidPerShareSummaryOfBusinessResults（年間合計）とInterimDividendPaid...
    （中間のみ）が別物として区別され、混同されていないこと。"""
    result = parse_financial_periods(_SYNTHETIC_XBRL, _TEST_CODE_JGAAP)
    current_total = next(f for f in result["dps_total"] if f.period_end == date(2023, 12, 31))
    assert current_total.value == 154.00, current_total.value
    assert current_total.value != 75.00


def test_annual_dividend_only_company_has_empty_interim_list():
    """年1回配当のみの銘柄（中間配当タグが存在しない）でも、BVPS・年間合計配当さえ取れれば
    例外を出さずdps_interimが空リストで返ること（必須タグはBVPSとdps_totalのみ）。"""
    xbrl_no_interim = _SYNTHETIC_XBRL.replace(
        '<jpcrp_cor:InterimDividendPaidPerShareSummaryOfBusinessResults contextRef="CurrentYearDuration" unitRef="JPYPerShares" decimals="2">75.00</jpcrp_cor:InterimDividendPaidPerShareSummaryOfBusinessResults>',
        "",
    ).replace(
        '<jpcrp_cor:InterimDividendPaidPerShareSummaryOfBusinessResults contextRef="Prior1YearDuration" unitRef="JPYPerShares" decimals="2">68.00</jpcrp_cor:InterimDividendPaidPerShareSummaryOfBusinessResults>',
        "",
    )
    result = parse_financial_periods(xbrl_no_interim, _TEST_CODE_JGAAP)
    assert result["dps_interim"] == []
    assert len(result["bvps"]) == 5


def test_raises_when_bvps_tag_missing():
    """BVPSタグが1件も見つからない場合（IFRS採用企業等でタグ名が違う可能性）、サイレントに
    空リストを返さず例外を送出すること。"""
    try:
        parse_financial_periods(_SYNTHETIC_XBRL_MISSING_BVPS, _TEST_CODE_JGAAP)
        raised = False
    except EdinetXbrlStructureError:
        raised = True
    assert raised, "BVPSタグ欠損で例外が送出されるべき"


def test_jgaap_method_selects_bare_consolidated_when_nonconsolidated_also_present():
    """2026-08-16実装レビュー(QA)指摘のケース(a): bareと非連結が両方存在する場合、
    bare(連結)側の値が選ばれること（非連結側の値と取り違えないこと）。"""
    assert CONSOLIDATION_METHOD_BY_CODE[_TEST_CODE_JGAAP] == CONSOLIDATION_METHOD_JGAAP_BARE
    result = parse_financial_periods(_SYNTHETIC_XBRL_BARE_AND_NONCONSOLIDATED_MIXED, _TEST_CODE_JGAAP)
    assert len(result["bvps"]) == 1, result["bvps"]
    assert result["bvps"][0].value == 1973.30, result["bvps"][0].value  # bare(連結)側の値


def test_jgaap_method_raises_when_only_nonconsolidated_present():
    """2026-08-16実装レビュー(QA)指摘のケース(b): 非連結contextしか存在しない場合、
    サイレントに非連結値へフォールバックせず例外を送出すること
    （2026-08-16に実際に発生したバグの再発防止）。"""
    assert CONSOLIDATION_METHOD_BY_CODE[_TEST_CODE_JGAAP] == CONSOLIDATION_METHOD_JGAAP_BARE
    try:
        parse_financial_periods(_SYNTHETIC_XBRL_NONCONSOLIDATED_ONLY, _TEST_CODE_JGAAP)
        raised = False
    except EdinetXbrlStructureError:
        raised = True
    assert raised, "非連結contextしか無い場合は例外が送出されるべき"


def test_ifrs_method_computes_consolidated_bvps_correctly():
    """2026-08-16実装レビュー(QA)指摘のケース(c): IFRS方式（親会社株主帰属持分÷
    自己株式控除後の発行済株式数）で正しく連結BVPSが計算されること。
    非連結のNetAssetsPerShareタグが同居していてもそちらに引っ張られないこと。"""
    assert CONSOLIDATION_METHOD_BY_CODE[_TEST_CODE_IFRS] == CONSOLIDATION_METHOD_IFRS_COMPUTED
    result = parse_financial_periods(_SYNTHETIC_XBRL_IFRS, _TEST_CODE_IFRS)
    assert len(result["bvps"]) == 1, result["bvps"]
    computed = result["bvps"][0]
    expected = 4086933000000 / (2000000000 - 224199500)
    assert abs(computed.value - expected) < 0.01, computed.value
    assert computed.value != 753.52  # 非連結値と取り違えていないこと
    assert computed.period_end == date(2025, 12, 31), computed.period_end


def test_ifrs_method_uses_fallback_tag_when_summary_tag_absent():
    """2026-08-16、JT(2914)の実データ取得で発覚したケース: 主要経営指標等の
    "...SummaryOfBusinessResults"版タグを持たず、連結貸借対照表本体のタグ
    (サフィックス無し)にしか値が無い銘柄でも、フォールバックにより正しく計算できること。"""
    assert CONSOLIDATION_METHOD_BY_CODE[_TEST_CODE_IFRS] == CONSOLIDATION_METHOD_IFRS_COMPUTED
    result = parse_financial_periods(_SYNTHETIC_XBRL_IFRS_FALLBACK_TAG, _TEST_CODE_IFRS)
    assert len(result["bvps"]) == 1, result["bvps"]
    expected = 4086933000000 / (2000000000 - 224199500)
    assert abs(result["bvps"][0].value - expected) < 0.01, result["bvps"][0].value


def test_usgaap_method_selects_bare_consolidated_value():
    """2026-08-16、コマツ(6301)の実データ取得で発覚した3つ目のパターン（US GAAP方式）。
    1株当たり値の完成品タグをそのまま使い、非連結のNetAssetsPerShareタグに引っ張られないこと。"""
    assert CONSOLIDATION_METHOD_BY_CODE[_TEST_CODE_USGAAP] == CONSOLIDATION_METHOD_USGAAP_BARE
    result = parse_financial_periods(_SYNTHETIC_XBRL_USGAAP, _TEST_CODE_USGAAP)
    assert len(result["bvps"]) == 2, result["bvps"]  # Current・Prior1の2期分
    current = next(f for f in result["bvps"] if f.context_ref == "CurrentYearInstant")
    assert current.value == 3896.10, current.value
    assert current.value != 937.51  # 非連結値と取り違えていないこと


def test_ifrs_method_treats_missing_treasury_tag_as_zero():
    """2026-08-16、日本取引所G(8697)の実データ取得で発覚したケース: 自己株式数タグ自体が
    存在しない年は、エラーにせず自己株式数0として計算を継続すること。"""
    result = parse_financial_periods(_SYNTHETIC_XBRL_IFRS_NO_TREASURY_TAG, _TEST_CODE_IFRS)
    assert len(result["bvps"]) == 1, result["bvps"]
    expected = 345015000000 / 522289183  # 自己株式控除なし(0扱い)
    assert abs(result["bvps"][0].value - expected) < 0.01, result["bvps"][0].value


def _escape_html_for_textblock(raw_html: str) -> str:
    """XML内のTextBlockが実際に使う形式（HTMLをXMLエンティティエスケープした文字列）に変換する。"""
    return raw_html.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def test_extract_parent_equity_usgaap_old_filing():
    """2026-08-16、クボタ・NTTの2017-2018年提出分がUS GAAPタグ
    (ConsolidatedBalanceSheetUSGAAPTextBlock)だったケース。「株主資本合計」行
    （非支配持分は別行のため既に親会社株主帰属分のみ）を直接使う。"""
    table_html = (
        "<table><tr><td>株主資本合計</td><td>1,198,761</td><td>44.9</td></tr>"
        "<tr><td>非支配持分</td><td>73,164</td><td>2.7</td></tr></table>"
    )
    xbrl = f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2018-02-28/jpcrp_cor">
  <jpcrp_cor:ConsolidatedBalanceSheetUSGAAPTextBlock>{_escape_html_for_textblock(table_html)}</jpcrp_cor:ConsolidatedBalanceSheetUSGAAPTextBlock>
</xbrli:xbrl>
"""
    equity = _extract_parent_equity_from_html(xbrl)
    assert equity == 1198761 * 1_000_000, equity


def test_extract_parent_equity_jgaap_old_filing_via_subtraction():
    """2026-08-16、ブリヂストンの2016-2019年提出分がJGAAPタグ(ConsolidatedBalanceSheetTextBlock)で、
    親会社株主帰属分の直接行が無いケース。「純資産合計－非支配株主持分」で算出する。"""
    table_html = (
        "<table><tr><td>非支配株主持分</td><td>54,198</td><td>52,576</td></tr>"
        "<tr><td>純資産合計</td><td>2,436,162</td><td>2,344,290</td></tr></table>"
    )
    xbrl = f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:jpcrp_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2018-02-28/jpcrp_cor">
  <jpcrp_cor:ConsolidatedBalanceSheetTextBlock>{_escape_html_for_textblock(table_html)}</jpcrp_cor:ConsolidatedBalanceSheetTextBlock>
</xbrli:xbrl>
"""
    equity = _extract_parent_equity_from_html(xbrl)
    expected = (2436162 - 54198) * 1_000_000
    assert equity == expected, equity


def test_find_table_row_value_parses_html_escaped_table():
    """_find_table_row_value()が、実際のEDINET形式（XMLエンティティエスケープされたHTML表）
    から指定ラベル行の数値を正しく取り出せること。"""
    html = "&lt;table&gt;&lt;tr&gt;&lt;td&gt;計&lt;/td&gt;&lt;td&gt;1,234,567&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;"
    # _find_table_row_value自体はエスケープ解除済みの生HTML文字列を受け取る想定なので、
    # ここではET経由でelem.textとして取り出した後の状態（エスケープ解除済み）を模す。
    import html as html_module
    unescaped = html_module.unescape(html)
    value = _find_table_row_value(unescaped, ("計",))
    assert value == 1234567.0, value


def test_extract_issued_shares_and_treasury_and_equity_from_html():
    """2026-08-16、2018年頃までの古い提出分向けHTMLフォールバック3関数が、
    実データ形式（三菱商事2018年3月期・JT2018年12月期）を模した合成データから
    正しく値を抽出できること。"""
    issued = _extract_issued_shares_from_html(_SYNTHETIC_XBRL_IFRS_OLD_FILING_HTML_ONLY)
    assert issued == 1590076851.0, issued
    treasury = _extract_treasury_shares_from_html(_SYNTHETIC_XBRL_IFRS_OLD_FILING_HTML_ONLY)
    assert treasury == 4107848.0, treasury
    equity = _extract_parent_equity_from_html(_SYNTHETIC_XBRL_IFRS_OLD_FILING_HTML_ONLY)
    assert equity == 2761687000000.0, equity  # 百万円 -> 円に変換済みであること


def test_ifrs_method_falls_back_to_html_for_old_filing_without_structured_tags():
    """2026-08-16、古い提出分（構造化タグが一切無くHTMLのみ）でも、parse_financial_periods()
    がHTMLフォールバック経由で正しくBVPSを計算できること（実データ検証: JTの根本原因の再現）。"""
    result = parse_financial_periods(_SYNTHETIC_XBRL_IFRS_OLD_FILING_HTML_ONLY, _TEST_CODE_IFRS)
    assert len(result["bvps"]) == 1, result["bvps"]
    expected = 2761687000000.0 / (1590076851.0 - 4107848.0)
    assert abs(result["bvps"][0].value - expected) < 0.01, result["bvps"][0].value


def test_per_year_fetch_codes_restrict_to_current_year_only():
    """2026-08-16、SMFG・東京海上HDで発覚した「1書類内の比較期間の遡及修正が一貫しない」問題への
    対応。PER_YEAR_FETCH_CODES対象銘柄は、bare方式でもPrior1〜4Yearを一切使わず
    CurrentYearInstantのみを返すこと。"""
    assert _TEST_CODE_JGAAP_PER_YEAR in PER_YEAR_FETCH_CODES
    result = parse_financial_periods(_SYNTHETIC_XBRL, _TEST_CODE_JGAAP_PER_YEAR)
    assert len(result["bvps"]) == 1, result["bvps"]  # 5期分ではなく当期分のみ
    assert result["bvps"][0].context_ref == "CurrentYearInstant"
    assert result["bvps"][0].value == 3456.78, result["bvps"][0].value


def test_accounting_standard_transition_switches_to_ifrs_after_transition_date():
    """2026-08-17発覚: 東京海上HDは2026年3月期からJGAAP→IFRSへ会計基準を移行した。
    書類自身の当期(CurrentYearInstant)の期末日が移行日以降なら、コード既定のJGAAP方式ではなく
    IFRS方式で計算した値が返ること（旧JGAAPタグに引っ張られないこと）。"""
    assert _TEST_CODE_TRANSITION in CONSOLIDATION_METHOD_TRANSITIONS_BY_CODE
    assert CONSOLIDATION_METHOD_BY_CODE[_TEST_CODE_TRANSITION] == CONSOLIDATION_METHOD_JGAAP_BARE
    result = parse_financial_periods(_SYNTHETIC_XBRL_TRANSITION_AFTER, _TEST_CODE_TRANSITION)
    assert len(result["bvps"]) == 1, result["bvps"]
    computed = result["bvps"][0]
    expected = 7955554000000 / (1934000000 - 53937600)
    assert abs(computed.value - expected) < 0.01, computed.value
    assert computed.value != 2885.44  # 旧JGAAPタグの値と取り違えていないこと


def test_accounting_standard_transition_keeps_jgaap_before_transition_date():
    """移行日より前の期（2025年3月期）は、従来通りJGAAP方式のbare値がそのまま使われること
    （過去に取得済みのデータへ影響が波及しないことの確認）。"""
    result = parse_financial_periods(_SYNTHETIC_XBRL_TRANSITION_BEFORE, _TEST_CODE_TRANSITION)
    assert len(result["bvps"]) == 1, result["bvps"]
    assert result["bvps"][0].value == 2640.27, result["bvps"][0].value


def test_parse_financial_periods_raises_for_unregistered_code():
    """CONSOLIDATION_METHOD_BY_CODEに未登録の銘柄コードでは、どちらかの方式を
    サイレントに仮定せず例外を送出すること。"""
    try:
        parse_financial_periods(_SYNTHETIC_XBRL, "0000")  # 未登録の架空コード
        raised = False
    except EdinetXbrlStructureError:
        raised = True
    assert raised, "未登録の銘柄コードでは例外が送出されるべき"


def test_raises_when_no_contexts_at_all():
    broken_xml = '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"></xbrli:xbrl>'
    try:
        parse_xbrl_contexts(broken_xml)
        raised = False
    except EdinetXbrlStructureError:
        raised = True
    assert raised, "context要素が0件で例外が送出されるべき"


def test_find_xbrl_instance_in_zip_extracts_correct_file():
    """書類取得API(type=1)が返すZIP（XBRL/PublicDoc/配下に.xbrlファイル）の構造を模した
    合成ZIPから、正しくXBRLインスタンス文字列を取り出せること。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("PublicDoc/0105010_honbun.htm", "<html>ダミーのインラインXBRL本文</html>")
        zf.writestr("XBRL/PublicDoc/jpcrp030000-asr-001_E00492-000_2023-12-31_01_2024-03-22.xbrl", _SYNTHETIC_XBRL)
        zf.writestr("XBRL/AuditDoc/dummy.xbrl", "<xbrli:xbrl></xbrli:xbrl>")
    extracted = find_xbrl_instance_in_zip(buf.getvalue())
    assert "NetAssetsPerShareSummaryOfBusinessResults" in extracted


def test_find_xbrl_instance_in_zip_raises_when_missing():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("PublicDoc/0105010_honbun.htm", "<html>本文のみ、XBRLインスタンスファイル無し</html>")
    try:
        find_xbrl_instance_in_zip(buf.getvalue())
        raised = False
    except EdinetXbrlStructureError:
        raised = True
    assert raised, "XBRL/PublicDoc/*.xbrlが無いZIPでは例外が送出されるべき"


def _run_all():
    tests = [
        test_parses_contexts_instant_and_duration,
        test_extracts_bvps_facts_sorted_by_period_end,
        test_parse_financial_periods_returns_three_series,
        test_dps_interim_values_correct,
        test_dps_total_is_full_year_not_interim,
        test_annual_dividend_only_company_has_empty_interim_list,
        test_raises_when_bvps_tag_missing,
        test_jgaap_method_selects_bare_consolidated_when_nonconsolidated_also_present,
        test_jgaap_method_raises_when_only_nonconsolidated_present,
        test_ifrs_method_computes_consolidated_bvps_correctly,
        test_ifrs_method_uses_fallback_tag_when_summary_tag_absent,
        test_usgaap_method_selects_bare_consolidated_value,
        test_ifrs_method_treats_missing_treasury_tag_as_zero,
        test_extract_parent_equity_usgaap_old_filing,
        test_extract_parent_equity_jgaap_old_filing_via_subtraction,
        test_per_year_fetch_codes_restrict_to_current_year_only,
        test_find_table_row_value_parses_html_escaped_table,
        test_extract_issued_shares_and_treasury_and_equity_from_html,
        test_ifrs_method_falls_back_to_html_for_old_filing_without_structured_tags,
        test_accounting_standard_transition_switches_to_ifrs_after_transition_date,
        test_accounting_standard_transition_keeps_jgaap_before_transition_date,
        test_parse_financial_periods_raises_for_unregistered_code,
        test_raises_when_no_contexts_at_all,
        test_find_xbrl_instance_in_zip_extracts_correct_file,
        test_find_xbrl_instance_in_zip_raises_when_missing,
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
