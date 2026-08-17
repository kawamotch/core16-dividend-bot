# -*- coding: utf-8 -*-
"""
EDINET書類取得API（type=1、提出本文書及び監査報告書ZIP）に含まれるXBRLインスタンスファイルから、
1株当たり純資産（BVPS）・1株配当（中間・期末を別々に）を年度別に抽出する。

bot/irbank_pbr_range.py・bot/irbank_dividend.pyと同じ設計方針（パース関数はXBRL文字列を受け取る
純粋関数にしてネットワーク取得と分離、構造不一致はサイレント欠損させず例外送出）に従う。

EDINET実現可能性検証（2026-08-14、JT/2914で実施）で確認済みのタグ:
- jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults（BVPS、1株当たり純資産）
- jpcrp_cor:DividendPaidPerShareSummaryOfBusinessResults（年間配当合計）
- jpcrp_cor:InterimDividendPaidPerShareSummaryOfBusinessResults（中間配当、独立タグ）
1件の有価証券報告書に過去5年分（Current〜Prior4Year）が一括収録されている。

XBRLの構造について: 書類取得API type=1のZIPには、インライン XBRL（HTML内埋め込み、PublicDoc配下）
とは別に、XBRL/PublicDoc配下に純粋なXBRLインスタンスファイル（.xbrl拡張子、通常のXML）が同梱される
（EDINET API仕様書「3-2-2 レスポンスについて 圧縮ファイルの構成」参照）。本モジュールはこちらの
XBRLインスタンスファイルをパース対象とする（インラインHTMLよりタグ抽出が単純で壊れにくいため）。

BVPSは期末時点のストック値のため<xbrli:instant>を持つcontext（例: "CurrentYearInstant"）に、
配当は年度中のフロー値のため<xbrli:startDate>/<xbrli:endDate>を持つcontext
（例: "CurrentYearDuration"）に、それぞれ紐づく想定。

【連結/非連結の取り違えバグと修正（2026-08-16発覚・修正）】
`NetAssetsPerShareSummaryOfBusinessResults`はcontextRefで「連結」と「提出会社（非連結・単体）」の
両方に使われるタグで、当初contextRefを絞り込まず全件抽出していたため、JT(2914)等でこのタグの
非連結版（"_NonConsolidatedMember"サフィックス付き）しか存在しない銘柄で、意図せず非連結BVPSを
抽出していた（市場PBRの前提となる連結BVPSの2〜3倍過小評価。詳細はtasks/lessons.md参照）。

当初、16銘柄全件の生XBRL監査で「JGAAP方式・IFRS方式の2パターンのみ」と結論づけたが、
その後の実データ取得（全銘柄・全年度分の本番取得）で③US GAAP方式（コマツ1銘柄）という
想定外の3つ目のパターンが発覚した。「N銘柄中M銘柄が同一パターン」という事前調査は
サンプル調査に過ぎず、全数を実際に取得して初めて例外を検出できることの実例
（tasks/lessons.md参照）。詳細はtasks/handoff_next_session.md 2026-08-16追記・続き参照:
- **JGAAP方式**（3銘柄: MUFG・SMFG・東京海上HD）: `NetAssetsPerShareSummaryOfBusinessResults`の
  bare context（"_NonConsolidatedMember"サフィックス無し＝連結）がそのまま使える。
  Prior1〜4Yearの過去4期分も同じタグ・同じパターンで収録されている。
- **US GAAP方式**（1銘柄: コマツ）: `EquityAttributableToOwnersOfParentPerShareUSGAAPSummaryOfBusinessResults`
  という1株当たり値の完成品タグをJGAAP方式と同じ要領（bare context選択）で使う。
  こちらもPrior1〜4Yearの5期分時系列を持つ。
- **IFRS方式**（残り12銘柄）: 連結の「1株純資産」完成品タグが存在しない。代わりに
  `EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults`（親会社株主帰属持分）を
  `NumberOfSharesIssuedSharesVotingRights`−`TotalNumberOfSharesHeldTreasurySharesEtc`
  （自己株式控除後の発行済株式数）で割って自前計算する。ただしこの親会社株主帰属持分タグは
  **当期分(CurrentYearInstant)1点のみ**で過去4期分が無い（JTの生XBRLで確認済み。5年分の
  時系列を持つ`TotalEquityIFRSSummaryOfBusinessResults`は非支配株主持分を含んでしまうため
  代用不可とユーザー判断済み）。このためIFRS方式の12銘柄は、本モジュール1回の呼び出しで
  5期分をまとめて取得できず、**呼び出し側が対象年数分の個別の有価証券報告書を1年ごとに
  取得してこのモジュールへ渡す**設計が前提になっている（`fetch_edinet_financial_data.py`の
  年次取得ループ参照）。

銘柄コード→方式の対応は`CONSOLIDATION_METHOD_BY_CODE`に持つ。新規銘柄を追加する際は、
必ず生XBRLでこの2パターンのどちらに該当するか（またはさらに別のパターンか）を先に確認し、
このテーブルに明示的に登録すること（未登録の銘柄コードは例外を送出し、サイレントに
どちらかの方式を仮定しない）。

【銘柄が期の途中で会計基準を変更するケースと修正（2026-08-17発覚・修正）】
上記の「銘柄コード→方式」は固定の1対1対応を前提としていたが、東京海上HD(8766)は
2026年3月期からJGAAPからIFRSへ会計基準を移行しており、この前提が崩れることが判明した
（新方式の現在値を旧IRBANK方式との一致度ではなく、Yahoo!ファイナンスの公表PBRという
どちらの方式にも依存しない第三の情報源と突き合わせる独立検証で発覚。詳細は
tasks/lessons.mdおよびtasks/handoff_next_session.md参照）。移行後もJGAAPの
`NetAssetsPerShareSummaryOfBusinessResults`タグ自体は値を持ち続けるが、もはや正しい
連結BVPSを表さない（実勢比-33%）。`CONSOLIDATION_METHOD_TRANSITIONS_BY_CODE`で
銘柄コード→(移行日, 移行後の方式)を登録し、`parse_financial_periods()`が書類自身の
当期(CurrentYearInstant)の期末日で移行日以降かどうかを判定して方式を動的に切り替える。
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from datetime import date
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

XBRLI_NS = "http://www.xbrl.org/2003/instance"

TAG_BVPS = "NetAssetsPerShareSummaryOfBusinessResults"
TAG_DPS_TOTAL = "DividendPaidPerShareSummaryOfBusinessResults"
TAG_DPS_INTERIM = "InterimDividendPaidPerShareSummaryOfBusinessResults"

# IFRS方式で連結BVPSを自前計算するための3タグ（上記docstring「連結/非連結の取り違え」参照）
TAG_IFRS_EQUITY_ATTRIBUTABLE_TO_PARENT = "EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults"
# JT(2914)は主要経営指標等の表にこの概念のSummaryOfBusinessResults版タグを持たず、
# 連結貸借対照表本体のタグ（サフィックス無し）にしか値が無い（2026-08-16、実データ取得で発覚）。
# 他12銘柄は逆にこちらのタグが存在しないため、TAG_IFRS_EQUITY_ATTRIBUTABLE_TO_PARENTで
# 見つからない場合のフォールバックとして使う。
TAG_IFRS_EQUITY_ATTRIBUTABLE_TO_PARENT_FALLBACK = "EquityAttributableToOwnersOfParentIFRS"
TAG_SHARES_ISSUED = "NumberOfSharesIssuedSharesVotingRights"
TAG_TREASURY_SHARES = "TotalNumberOfSharesHeldTreasurySharesEtc"

# US GAAP方式（コマツのみ、2026-08-16実データ取得で発覚）: JGAAP方式と同様、1株純資産の
# 完成品タグがそのままPrior1〜4Yearの5期分時系列を持つ（生XBRLで確認済み）。
TAG_USGAAP_BVPS = "EquityAttributableToOwnersOfParentPerShareUSGAAPSummaryOfBusinessResults"

CONSOLIDATION_METHOD_JGAAP_BARE = "jgaap_bare"
CONSOLIDATION_METHOD_IFRS_COMPUTED = "ifrs_computed"
CONSOLIDATION_METHOD_USGAAP_BARE = "usgaap_bare"

# 銘柄コード → 連結BVPSの抽出方式。
# 2026-08-16、16銘柄全件の生XBRL監査で「JGAAP/IFRSの2パターンのみ」と当初結論づけたが、
# 実データ取得で③コマツがUS GAAP採用（3つ目の会計基準）と判明し修正した
# （tasks/lessons.md「N銘柄中M銘柄が同一パターン」の教訓参照。事前監査だけで安心せず、
# 全銘柄・全年度の実データ取得を通して初めて例外を検出できた）。
CONSOLIDATION_METHOD_BY_CODE: dict[str, str] = {
    "8306": CONSOLIDATION_METHOD_JGAAP_BARE,  # MUFG
    "8316": CONSOLIDATION_METHOD_JGAAP_BARE,  # SMFG
    "8766": CONSOLIDATION_METHOD_JGAAP_BARE,  # 東京海上HD（2026年3月期以降はIFRSへ移行。下記
    # CONSOLIDATION_METHOD_TRANSITIONS_BY_CODE参照）
    "6301": CONSOLIDATION_METHOD_USGAAP_BARE,  # コマツ
    "2914": CONSOLIDATION_METHOD_IFRS_COMPUTED,  # JT
    "8058": CONSOLIDATION_METHOD_IFRS_COMPUTED,  # 三菱商事
    "8031": CONSOLIDATION_METHOD_IFRS_COMPUTED,  # 三井物産
    "8001": CONSOLIDATION_METHOD_IFRS_COMPUTED,  # 伊藤忠
    "9432": CONSOLIDATION_METHOD_IFRS_COMPUTED,  # NTT
    "9433": CONSOLIDATION_METHOD_IFRS_COMPUTED,  # KDDI
    "4503": CONSOLIDATION_METHOD_IFRS_COMPUTED,  # アステラス製薬
    "4578": CONSOLIDATION_METHOD_IFRS_COMPUTED,  # 大塚HD
    "6326": CONSOLIDATION_METHOD_IFRS_COMPUTED,  # クボタ
    "8697": CONSOLIDATION_METHOD_IFRS_COMPUTED,  # 日本取引所G
    "4452": CONSOLIDATION_METHOD_IFRS_COMPUTED,  # 花王
    "5108": CONSOLIDATION_METHOD_IFRS_COMPUTED,  # ブリヂストン
}

# JGAAP方式/US GAAP方式（bare、1書類にPrior1〜4Yearの比較期間が収録される方式）は通常
# 銘柄あたり2件（直近+5年前）の取得で足りるが、比較期間に株式分割をまたぐ銘柄は例外扱いする。
# 1書類内の複数比較期間を、会社側が最新の分割後基準へ遡及修正するかどうかの一貫性は
# 銘柄によって異なり（2026-08-16発覚、東京海上HDは書類内で一貫、SMFGは書類内で基準が混在）、
# 日付ベースの調整では解決できない。この2銘柄は比較期間(Prior1〜4Year)に一切頼らず、
# IFRS方式と同じ「対象年数分を毎年1件ずつ取得し、その書類自身のCurrentYearのみを使う」
# 方式に切り替えて根本的に回避する（ユーザー判断、2026-08-16）。
PER_YEAR_FETCH_CODES: frozenset[str] = frozenset({"8316", "8766"})  # SMFG, 東京海上HD

# 銘柄が期の途中で会計基準を変更するケース（2026-08-17発覚）: 東京海上HDは2026年3月期から
# JGAAP(日本基準)からIFRSへ移行した（2026年6月26日開示のIFRS決算短信で確認。Web検索で裏取り
# 済み）。独立検証（新方式の現在値をYahoo!ファイナンスの公表PBRという第三の情報源と突き合わせる、
# tasks/lessons.md参照）で東京海上HDのみ-33%という桁違いの乖離が判明し、原因を追跡した結果、
# `NetAssetsPerShareSummaryOfBusinessResults`(JGAAP)タグは2026年3月期分も引き続き値を持つ
# （2885.44円）が、これはもはや正しい連結BVPSを表していない（同一書類S100YLS8に他12銘柄と
# 同じIFRS方式タグ`EquityAttributableToOwnersOfParentIFRSSummaryOfBusinessResults`も
# 別途存在し、そちらから計算した値は4231.54円で、Yahoo!ファイナンス逆算値4308.6円との差は
# -1.8%と他15銘柄と同水準の「鮮度による説明可能な範囲」に収まることを検算済み）。
# コマツ(USGAAP)のように「銘柄固定で1つの会計基準」という前提では対応できない、
# 「同一銘柄が期の途中で会計基準を変える」ケース。対象期間（period_end、CurrentYearInstantの
# 期末日）が移行日以降ならIFRS方式に切り替える。移行前の期間（2017〜2025年3月期）は
# 従来通りJGAAP方式のまま（当時の主基準がJGAAPだったため）。
CONSOLIDATION_METHOD_TRANSITIONS_BY_CODE: dict[str, tuple[date, str]] = {
    "8766": (date(2026, 3, 31), CONSOLIDATION_METHOD_IFRS_COMPUTED),  # 東京海上HD、2026年3月期以降
}

# 「連結ベースの標準表現」と認識するcontextRef（member修飾の無い素のcontext）。
# "_NonConsolidatedMember"等のサフィックス付きcontextと明確に区別するため、
# 曖昧な文字列判定（アンダースコアの有無等）ではなく既知のbase名の完全一致で判定する。
_BARE_PERIOD_INSTANT_CONTEXTS = (
    "CurrentYearInstant",
    "Prior1YearInstant",
    "Prior2YearInstant",
    "Prior3YearInstant",
    "Prior4YearInstant",
)

# ZIP内、純粋なXBRLインスタンスファイルの想定パスパターン（末尾が.xbrlのPublicDoc配下ファイル）
_XBRL_INSTANCE_PATH_RE = re.compile(r"^XBRL/PublicDoc/.*\.xbrl$")


class EdinetXbrlStructureError(Exception):
    """EDINET XBRLの構造が想定と異なる場合（サイレント欠損を避けるため明示的に送出する）。"""


@dataclass(frozen=True)
class XbrlFact:
    context_ref: str
    period_end: date  # instantの日付、またはdurationのendDate
    value: float | None


def _local_name(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def find_xbrl_instance_in_zip(zip_bytes: bytes) -> str:
    """書類取得API（type=1）が返すZIPバイト列から、XBRLインスタンスファイル（.xbrl）の中身を
    文字列として取り出す。複数該当する場合は最初の1件を使う（提出本文書は通常1ファイル）。
    """
    with zipfile.ZipFile(__import__("io").BytesIO(zip_bytes)) as zf:
        candidates = [n for n in zf.namelist() if _XBRL_INSTANCE_PATH_RE.match(n)]
        if not candidates:
            raise EdinetXbrlStructureError(
                "ZIP内にXBRL/PublicDoc/*.xbrlが見つからない（EDINETのZIP構成が変更された可能性）"
            )
        with zf.open(sorted(candidates)[0]) as f:
            return f.read().decode("utf-8")


def parse_xbrl_contexts(xbrl_xml: str) -> dict[str, date]:
    """<xbrli:context id="...">から、context id → 期末日（instant日付 or endDate）の対応表を作る。

    ネットワーク不要の純粋関数。
    """
    root = ET.fromstring(xbrl_xml)
    contexts: dict[str, date] = {}
    for elem in root.iter():
        if _local_name(elem.tag) != "context":
            continue
        context_id = elem.get("id")
        if not context_id:
            continue
        period_end: date | None = None
        for child in elem:
            if _local_name(child.tag) != "period":
                continue
            for pchild in child:
                name = _local_name(pchild.tag)
                if name == "instant" and pchild.text:
                    period_end = date.fromisoformat(pchild.text.strip())
                elif name == "endDate" and pchild.text:
                    period_end = date.fromisoformat(pchild.text.strip())
        if period_end is not None:
            contexts[context_id] = period_end

    if not contexts:
        raise EdinetXbrlStructureError("XBRLにcontext要素が1件も見つからない（構造変更の可能性）")
    return contexts


def _parse_float_or_none(text: str | None) -> float | None:
    if text is None:
        return None
    text = text.strip().replace(",", "")
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def extract_facts_by_tag(xbrl_xml: str, tag_local_name: str, contexts: dict[str, date]) -> list[XbrlFact]:
    """指定タグ名（例: NetAssetsPerShareSummaryOfBusinessResults）を持つ全要素を抽出し、
    context経由で期末日を紐づけたXbrlFactのリストを返す（期末日の昇順）。

    同一タグが「連結」「個別」等の複数contextに存在する場合はすべて含める（呼び出し元で
    連結を優先する等のフィルタを行う想定。当プロジェクトは全銘柄連結決算のため通常は
    "Consolidated"系contextのみヒットする）。
    """
    root = ET.fromstring(xbrl_xml)
    facts: list[XbrlFact] = []
    for elem in root.iter():
        if _local_name(elem.tag) != tag_local_name:
            continue
        context_ref = elem.get("contextRef")
        if not context_ref or context_ref not in contexts:
            continue
        facts.append(
            XbrlFact(
                context_ref=context_ref,
                period_end=contexts[context_ref],
                value=_parse_float_or_none(elem.text),
            )
        )
    facts.sort(key=lambda f: f.period_end)
    return facts


def _select_bare_consolidated_facts(
    raw_facts: list[XbrlFact], tag_name: str, current_year_only: bool = False
) -> list[XbrlFact]:
    """JGAAP方式・US GAAP方式共通: 1株純資産の完成品タグのbare context（連結）のみを選ぶ。

    非連結(_NonConsolidatedMember)版しか無い場合、連結/非連結の取り違え（2026-08-16発覚）を
    再発させないため、サイレントに非連結値へフォールバックせず例外を送出する。

    current_year_only=True の場合、Prior1〜4Yearの比較期間を一切使わずCurrentYearInstant
    のみを返す（PER_YEAR_FETCH_CODES向け。1書類内の比較期間を株式分割後基準へ遡及修正するか
    どうかの一貫性が銘柄によって異なる問題を、比較期間に頼らないことで根本的に回避する。
    2026-08-16、SMFG・東京海上HDで発覚・ユーザー判断）。
    """
    allowed_contexts = ("CurrentYearInstant",) if current_year_only else _BARE_PERIOD_INSTANT_CONTEXTS
    bare = [f for f in raw_facts if f.context_ref in allowed_contexts]
    if not bare:
        raise EdinetXbrlStructureError(
            f"{tag_name}に連結(bare context)の値が見つからない（非連結版しか無い可能性）。"
            "この銘柄はIFRS方式（CONSOLIDATION_METHOD_IFRS_COMPUTED）が必要かもしれない。"
            "CONSOLIDATION_METHOD_BY_CODEの設定を見直すこと。"
        )
    return bare


# ─────────────────────────────────────────────────────────────────────────
# HTML(TextBlock)フォールバック（2018年頃までの古い提出分向け、2026-08-16追加）
#
# 過去の確定済み実績値（発行済株式数・自己株式数・親会社の所有者に帰属する持分）を
# 一度だけバックフィルする用途。継続的に再取得・再パースする性質のコードではないため、
# 汎用的なHTML解析器を作り込むのではなく、実データで確認済みの3つのTextBlock・行ラベルの
# 組み合わせのみをピンポイントでサポートする（未知の構造に出会ったら例外を送出し、
# サイレントに欠損値へフォールバックしない）。
# ─────────────────────────────────────────────────────────────────────────

_TEXTBLOCK_ISSUED_SHARES = "IssuedSharesTotalNumberOfSharesEtcTextBlock"  # ②【発行済株式】
_TEXTBLOCK_TREASURY_SHARES = "DisposalsOrHoldingOfAcquiredTreasurySharesTextBlock"  # 自己株式の取得等の状況

# 連結貸借対照表系TextBlockは、その提出当時の会計基準（IFRS/US GAAP/JGAAP）によってタグ名が
# 異なる（同一銘柄でも会計基準移行前後の古い提出分では現行と異なるタグを使っている場合がある。
# 2026-08-16、クボタ・NTTの2017-2018年提出分がUS GAAPタグ、ブリヂストンの2016-2019年提出分が
# JGAAPタグだったことを実データで確認済み）。
_TEXTBLOCK_CONSOLIDATED_BS_IFRS = "ConsolidatedStatementOfFinancialPositionIFRSTextBlock"
_TEXTBLOCK_CONSOLIDATED_BS_USGAAP = "ConsolidatedBalanceSheetUSGAAPTextBlock"
_TEXTBLOCK_CONSOLIDATED_BS_JGAAP = "ConsolidatedBalanceSheetTextBlock"

# 親会社株主に帰属する持分（非支配持分を除く）の行ラベル。会計基準・表記ゆれに備え複数候補を試す。
_PARENT_EQUITY_ROW_LABELS = ("親会社の所有者に帰属する持分", "親会社株主に帰属する持分", "株主資本合計")
_TOTAL_NET_ASSETS_ROW_LABELS = ("純資産合計",)  # JGAAP: 非支配持分込みの合計（要非支配持分控除）
_NONCONTROLLING_INTERESTS_ROW_LABELS = ("非支配株主持分", "非支配持分")


def _extract_textblock_html(xbrl_xml: str, tag_local_name: str) -> str | None:
    """指定したTextBlock要素のHTML文字列を取り出す（見つからなければNone）。"""
    root = ET.fromstring(xbrl_xml)
    for elem in root.iter():
        if _local_name(elem.tag) == tag_local_name and elem.text:
            return elem.text
    return None


def _parse_japanese_number(text: str) -> float | None:
    """全角数字・カンマ区切りを含む日本語の財務表セル文字列をfloatに変換する。
    「－」「―」「-」等の空欄記号やパース不能な文字列はNoneを返す（0とは区別する）。
    """
    text = text.strip().translate(str.maketrans("０１２３４５６７８９，", "0123456789,"))
    text = text.replace(",", "")
    if text in ("", "－", "―", "-", "‐"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _find_table_row_value(html: str, row_labels: tuple[str, ...]) -> float | None:
    """HTML内の<table>群から、先頭セルが row_labels のいずれかと完全一致する行を探し、
    それに続くセルのうち最初にパースできた数値を返す（見つからなければNone）。
    """
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if not cells or cells[0] not in row_labels:
                continue
            for cell in cells[1:]:
                value = _parse_japanese_number(cell)
                if value is not None:
                    return value
    return None


def _extract_issued_shares_from_html(xbrl_xml: str) -> float | None:
    """②【発行済株式】テーブルの「計」行から発行済株式総数を取得する
    （実データ検証済み: 三菱商事2018年3月期、単位=株そのまま）。"""
    html = _extract_textblock_html(xbrl_xml, _TEXTBLOCK_ISSUED_SHARES)
    if html is None:
        return None
    return _find_table_row_value(html, ("計",))


def _extract_treasury_shares_from_html(xbrl_xml: str) -> float | None:
    """「自己株式の取得等の状況」テーブルの「保有自己株式数」行（当事業年度列）から
    自己株式数を取得する（実データ検証済み: 三菱商事2018年3月期、単位=株そのまま）。"""
    html = _extract_textblock_html(xbrl_xml, _TEXTBLOCK_TREASURY_SHARES)
    if html is None:
        return None
    return _find_table_row_value(html, ("保有自己株式数",))


def _extract_parent_equity_from_html(xbrl_xml: str) -> float | None:
    """連結貸借対照表系TextBlockから、親会社株主に帰属する持分（非支配持分を除く）を取得する。
    提出当時の会計基準によってタグ名・表記が異なるため、実データで確認済みの3パターンを
    順に試す（値はいずれも百万円単位で開示されているため100万倍して円単位に揃える。
    TAG_IFRS_EQUITY_ATTRIBUTABLE_TO_PARENT等の構造化タグと単位を整合させるため）。

    - IFRS（実データ検証済み: JT2018年12月期）: 「親会社の所有者に帰属する持分」行を直接使う
    - US GAAP（実データ検証済み: クボタ・NTT2017-2018年3月期）: 「株主資本合計」行
      （非支配持分は別行のため、この行は既に親会社株主帰属分のみ）を直接使う
    - JGAAP（実データ検証済み: ブリヂストン2016-2019年12月期）: 「親会社株主に帰属する持分」の
      行自体が存在しないため、「純資産合計」（非支配持分込み）から「非支配株主持分」を
      差し引いて算出する
    """
    for textblock in (_TEXTBLOCK_CONSOLIDATED_BS_IFRS, _TEXTBLOCK_CONSOLIDATED_BS_USGAAP):
        html = _extract_textblock_html(xbrl_xml, textblock)
        if html is None:
            continue
        value_in_millions = _find_table_row_value(html, _PARENT_EQUITY_ROW_LABELS)
        if value_in_millions is not None:
            return value_in_millions * 1_000_000

    # JGAAPの古い提出分: 親会社株主帰属分の直接行が無く、純資産合計－非支配持分で算出する
    html = _extract_textblock_html(xbrl_xml, _TEXTBLOCK_CONSOLIDATED_BS_JGAAP)
    if html is None:
        return None
    total_net_assets = _find_table_row_value(html, _TOTAL_NET_ASSETS_ROW_LABELS)
    noncontrolling = _find_table_row_value(html, _NONCONTROLLING_INTERESTS_ROW_LABELS)
    if total_net_assets is None or noncontrolling is None:
        return None
    return (total_net_assets - noncontrolling) * 1_000_000


def _compute_ifrs_consolidated_bvps(xbrl_xml: str, contexts: dict[str, date]) -> list[XbrlFact]:
    """IFRS方式: 親会社株主帰属持分 ÷ 自己株式控除後の発行済株式数で連結BVPSを自前計算する。

    親会社株主帰属持分タグ(TAG_IFRS_EQUITY_ATTRIBUTABLE_TO_PARENT)はCurrentYearInstant
    （その書類自身の当期分）にしか値が無い（Prior1〜4Yearの時系列を持たない、2026-08-16
    JTの生XBRLで確認済み）。そのため呼び出し側が対象年数分の個別の有価証券報告書を1年ごとに
    取得する設計を前提とし、本関数は常に単一期（1件のXbrlFact）を返す。

    JT(2914)は他12銘柄と異なり、主要経営指標等の表にこの概念の"SummaryOfBusinessResults"
    版タグを持たず、連結貸借対照表本体のタグ(TAG_IFRS_EQUITY_ATTRIBUTABLE_TO_PARENT_FALLBACK、
    サフィックス無し)にしか値が無い（2026-08-16、実データ取得で発覚。生XBRLで確認済み）。
    主タグで見つからない場合はフォールバックタグも試す。

    2018年頃までの提出分は【株式等の状況】・（一部銘柄では）連結貸借対照表の該当項目が
    構造化XBRLタグを持たず、自由記述のHTML(TextBlock)としてのみ開示されている
    （2026-08-16発覚）。この場合、下記の`_html_fallback`各関数でHTML表から直接値を
    パースするフォールバックを試みる（このフォールバックは過去の確定済み実績値を
    一度だけ取得するための用途であり、`bot/edinet_financials.py`の主経路ほどの
    汎用性は求めない。それでもフォールバックすら失敗した場合はサイレントに諦めず
    例外を送出する）。

    自己株式数(TAG_TREASURY_SHARES)が存在しない年は、その期の自己株式保有数が実質ゼロ
    （買い戻し分を全て消却済み等）で開示側が該当行を省略したものとみなし0として扱う
    （2026-08-16、日本取引所G(8697)で発見。同一銘柄でも年によってタグの有無が変わり、
    発行済株式数だけは常に存在することを実データで確認済み）。発行済株式数タグ自体が
    無い場合（古い提出分の構造的制約）と区別するため、発行済株式数タグの欠損は
    引き続き例外を送出する。
    """
    equity_facts = extract_facts_by_tag(xbrl_xml, TAG_IFRS_EQUITY_ATTRIBUTABLE_TO_PARENT, contexts)
    equity_bare = [f for f in equity_facts if f.context_ref == "CurrentYearInstant"]
    if not equity_bare:
        # JTのようにSummaryOfBusinessResults版タグが無い銘柄向けフォールバック
        fallback_facts = extract_facts_by_tag(xbrl_xml, TAG_IFRS_EQUITY_ATTRIBUTABLE_TO_PARENT_FALLBACK, contexts)
        equity_bare = [f for f in fallback_facts if f.context_ref == "CurrentYearInstant"]

    shares_issued_facts = extract_facts_by_tag(xbrl_xml, TAG_SHARES_ISSUED, contexts)
    treasury_facts = extract_facts_by_tag(xbrl_xml, TAG_TREASURY_SHARES, contexts)
    shares_bare = [f for f in shares_issued_facts if f.context_ref == "CurrentYearInstant"]
    treasury_bare = [f for f in treasury_facts if f.context_ref == "CurrentYearInstant"]

    period_end = contexts.get("CurrentYearInstant")
    if period_end is None:
        raise EdinetXbrlStructureError("CurrentYearInstant contextの期末日が見つからない")

    equity_value = equity_bare[0].value if equity_bare else None
    if equity_value is None:
        equity_value = _extract_parent_equity_from_html(xbrl_xml)  # 古い提出分向けHTMLフォールバック
    if equity_value is None:
        raise EdinetXbrlStructureError(
            f"{TAG_IFRS_EQUITY_ATTRIBUTABLE_TO_PARENT}・{TAG_IFRS_EQUITY_ATTRIBUTABLE_TO_PARENT_FALLBACK}・"
            "HTMLフォールバックのいずれからも親会社株主帰属持分が見つからない（要個別確認）"
        )

    shares_issued = shares_bare[0].value if shares_bare else None
    if shares_issued is None:
        shares_issued = _extract_issued_shares_from_html(xbrl_xml)  # 古い提出分向けHTMLフォールバック
    if shares_issued is None:
        raise EdinetXbrlStructureError(
            f"{TAG_SHARES_ISSUED}・HTMLフォールバックのいずれからも発行済株式数が見つからない（要個別確認）"
        )

    # 自己株式数タグが無い年は実質ゼロとみなす（構造化タグが無ければHTMLフォールバックを試み、
    # それも見つからなければ0とみなす。タグ自体はあるが値がNoneの場合も同様に0として扱う）。
    if treasury_bare and treasury_bare[0].value is not None:
        treasury = treasury_bare[0].value
    else:
        html_treasury = _extract_treasury_shares_from_html(xbrl_xml)
        treasury = html_treasury if html_treasury is not None else 0.0

    outstanding = shares_issued - treasury
    if outstanding <= 0:
        raise EdinetXbrlStructureError(
            f"自己株式控除後の発行済株式数が0以下({outstanding})。データ異常の可能性、要個別確認"
        )

    return [XbrlFact(context_ref="CurrentYearInstant", period_end=period_end, value=equity_value / outstanding)]


def parse_financial_periods(xbrl_xml: str, code: str) -> dict[str, list[XbrlFact]]:
    """1件のXBRLインスタンスから、BVPS・年間配当合計・中間配当の3系列をまとめて抽出する。

    code: 証券コード。CONSOLIDATION_METHOD_BY_CODEを引いて連結BVPSの抽出方式（JGAAP方式/
    IFRS方式）を選ぶ（詳細は本モジュールdocstring「連結/非連結の取り違え」参照）。

    戻り値: {"bvps": [...], "dps_total": [...], "dps_interim": [...]}
    いずれも1件も取得できなかった場合はEdinetXbrlStructureErrorを送出する
    （タグ名がIFRS採用企業等で異なる可能性があるため、サイレントに空リストのまま進めない）。
    """
    contexts = parse_xbrl_contexts(xbrl_xml)

    method = CONSOLIDATION_METHOD_BY_CODE.get(code)
    if method is None:
        raise EdinetXbrlStructureError(
            f"銘柄コード{code}がCONSOLIDATION_METHOD_BY_CODEに未登録。新規銘柄を追加する場合は、"
            "先に生XBRLでJGAAP方式/IFRS方式のどちらに該当するか確認してから登録すること"
            "（サイレントにどちらかの方式を仮定しない）"
        )

    # 銘柄が期の途中で会計基準を変更するケース（東京海上HDのJGAAP→IFRS移行、2026-08-17発覚。
    # 上記CONSOLIDATION_METHOD_TRANSITIONS_BY_CODEのdocstring参照）への対応。この書類自身の
    # 当期(CurrentYearInstant)の期末日が移行日以降なら、コード既定の方式を上書きする。
    transition = CONSOLIDATION_METHOD_TRANSITIONS_BY_CODE.get(code)
    if transition is not None:
        transition_date, transitioned_method = transition
        current_year_period_end = contexts.get("CurrentYearInstant")
        if current_year_period_end is not None and current_year_period_end >= transition_date:
            method = transitioned_method

    current_year_only = code in PER_YEAR_FETCH_CODES
    if method == CONSOLIDATION_METHOD_JGAAP_BARE:
        raw_bvps = extract_facts_by_tag(xbrl_xml, TAG_BVPS, contexts)
        bvps = _select_bare_consolidated_facts(raw_bvps, TAG_BVPS, current_year_only)
    elif method == CONSOLIDATION_METHOD_USGAAP_BARE:
        raw_bvps = extract_facts_by_tag(xbrl_xml, TAG_USGAAP_BVPS, contexts)
        bvps = _select_bare_consolidated_facts(raw_bvps, TAG_USGAAP_BVPS, current_year_only)
    elif method == CONSOLIDATION_METHOD_IFRS_COMPUTED:
        bvps = _compute_ifrs_consolidated_bvps(xbrl_xml, contexts)
    else:
        raise EdinetXbrlStructureError(f"未知のconsolidation method: {method}")

    dps_total = extract_facts_by_tag(xbrl_xml, TAG_DPS_TOTAL, contexts)
    dps_interim = extract_facts_by_tag(xbrl_xml, TAG_DPS_INTERIM, contexts)

    if not dps_total:
        raise EdinetXbrlStructureError(
            f"{TAG_DPS_TOTAL} タグが見つからない（IFRS採用等でタグ名が異なる可能性。要個別確認）"
        )
    # 中間配当は年1回配当の銘柄では0件（または0円）になり得るため、無ければ空リストのまま許容する
    # （dps_totalと違い必須にしない。2026-08-14引き継ぎメモの「16銘柄が全て年2回配当パターンか
    # どうかの事前監査が必要」という指摘に対応するため、ここでは検出のみ行い判断は呼び出し元に委ねる）。

    return {"bvps": bvps, "dps_total": dps_total, "dps_interim": dps_interim}
