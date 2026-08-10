# -*- coding: utf-8 -*-
"""
IRBANK(irbank.net)の個別銘柄ページから、年度ごとのPBR高値・安値の推移表を取得・パースする。

無料で閲覧できる個別銘柄ページ（例: https://irbank.net/2914/pbr）をスクレイピングする。
IRBANKの一括ダウンロードAPI（f.irbank.net/files/...）は直近5期分しかBPSを含まないため、
2010年〜の長期データが必要な本検証ではこちらの年度テーブルを使う
（tasks/backtest_design_core16_dividend_range_strategy.md「データソース調査結果」参照）。

設計方針（2026-08-07パネルレビュー・インフラSRE指摘への対応）:
- ページ構造が変わった場合はサイレントに欠損させず、例外を送出して呼び出し元に気づかせる。
- パース処理（parse_pbr_table）はHTML文字列を受け取る純粋関数にし、ネットワーク取得
  （fetch_pbr_range）と分離する。これによりネットワーク不要の合成データテストが書ける
  （tasks/lessons.md「重い処理・外部依存とロジックの分離」の原則、
  test_irbank_pbr_range_synthetic.py参照）。

このプロジェクト(core16_dividend_bot)は他BOT(tradingbot/swing_daily_bot)とコードを共有しない
独立プロジェクトのため、bot/mtf.py等の既存モジュールはimportしない（CLAUDE.md方針）。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from bs4 import BeautifulSoup

IRBANK_PBR_URL_TEMPLATE = "https://irbank.net/{code}/pbr"

# 年度テーブルのヘッダー行（1行目）。これと一致しない場合はページ構造変更とみなし例外を出す。
_EXPECTED_HEADER_ROW0 = ["年度", "株価", "出来高", "PER", "PBR", "時価総額", "期末"]

# データ行のセル数（年度・株価高値・株価安値・大商い・PER高値・PER安値・PBR高値・PBR安値・
# 時価総額高値・時価総額安値・期末PBR の11列）。「最新」行はこの列数と異なるため自動的に除外される。
_EXPECTED_DATA_ROW_CELLS = 11

_FISCAL_YEAR_RE = re.compile(r"(\d{4})年(\d{1,2})月期")


@dataclass(frozen=True)
class PbrPeriod:
    fiscal_year_label: str  # 例: "2014年12月期"
    end_year: int
    end_month: int
    pbr_high: float | None
    pbr_low: float | None
    # 期中の株価高値・安値（IRBANKの年度PBR表の同じ行にある値）。
    # BPS ≈ 株価高値 ÷ PBR高値（安値側とも相互検算）で逆算し、日次PBR(t)を
    # 「yfinance日次終値 ÷ その時点で最新の確定BPS」として算出するために使う
    # （tasks/backtest_design_core16_dividend_range_strategy.md「実現可能な代替設計」参照）。
    price_high: float | None
    price_low: float | None
    # PER高値・安値（同じ表の列）。EPS ≈ 株価 ÷ PER で逆算し、業績正常化チェック
    # （純利益が過去5年平均から著しく乖離していないか）に使う。
    per_high: float | None
    per_low: float | None


class IrbankPageStructureError(Exception):
    """IRBANKページの構造が想定と異なる場合（サイレント欠損を避けるため明示的に送出する）。"""


def _parse_float_or_none(text: str) -> float | None:
    text = text.strip().replace(",", "")
    if text in ("", "-", "―"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _extract_primary_number_text(cell) -> str:
    """株価高値・安値セルは「株価\\n出来高\\n日付」が同じ<span class="text">内に入れ子で
    詰め込まれており、単純にcell.get_text()すると全部連結されてしまう
    （例: "1,790358,0001/13"）。実際のDOMでは株価だけが最初の子<span>に入っている
    （<span class="text"><span class="co_red">1,790</span><span class="co_sm">...</span></span>）
    ため、その最初の子spanだけを取り出す。子spanが無い列（PER/PBR等）はそのままテキストを返す。
    """
    text_span = cell.find("span", class_="text")
    if text_span is None:
        return cell.get_text(strip=True)
    inner = text_span.find("span")
    if inner is None:
        return text_span.get_text(strip=True)
    return inner.get_text(strip=True)


def parse_pbr_table(html: str) -> list[PbrPeriod]:
    """IRBANK個別銘柄ページ(/{code}/pbr)のHTML文字列から年度別PBR高値・安値を抽出する。

    ネットワークアクセスを行わない純粋関数。ページ構造が想定と異なる場合は
    IrbankPageStructureErrorを送出する（欠損データをゼロ件のまま静かに返さない）。
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")

    target_table = None
    for table in tables:
        rows = table.find_all("tr")
        if not rows:
            continue
        header0 = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        if header0 == _EXPECTED_HEADER_ROW0:
            target_table = table
            break

    if target_table is None:
        raise IrbankPageStructureError(
            "年度別PBR高値・安値テーブルが見つからない"
            "（ヘッダー行が想定と一致するtableが無い。IRBANKのページ構造が変更された可能性）"
        )

    rows = target_table.find_all("tr")
    periods: list[PbrPeriod] = []
    for row in rows[2:]:  # 0,1行目はヘッダー（列名・高値/安値ラベル）
        cell_tags = row.find_all(["th", "td"])
        cells = [c.get_text(strip=True) for c in cell_tags]
        if len(cells) != _EXPECTED_DATA_ROW_CELLS:
            # 「最新」行など、期次データでない行は列数が異なるため自然にスキップされる
            continue
        m = _FISCAL_YEAR_RE.match(cells[0])
        if not m:
            continue
        end_year, end_month = int(m.group(1)), int(m.group(2))
        pbr_high = _parse_float_or_none(cells[6])
        pbr_low = _parse_float_or_none(cells[7])
        per_high = _parse_float_or_none(cells[4])
        per_low = _parse_float_or_none(cells[5])
        price_high = _parse_float_or_none(_extract_primary_number_text(cell_tags[1]))
        price_low = _parse_float_or_none(_extract_primary_number_text(cell_tags[2]))
        periods.append(
            PbrPeriod(
                fiscal_year_label=cells[0],
                end_year=end_year,
                end_month=end_month,
                pbr_high=pbr_high,
                pbr_low=pbr_low,
                price_high=price_high,
                price_low=price_low,
                per_high=per_high,
                per_low=per_low,
            )
        )

    if not periods:
        raise IrbankPageStructureError(
            "年度別PBR高値・安値テーブルは見つかったが、パースできたデータ行が0件"
            "（年度ラベルの正規表現が想定と不一致の可能性）"
        )

    return periods


def fetch_pbr_range(code: str, session=None, timeout: int = 20, user_agent: str | None = None) -> list[PbrPeriod]:
    """IRBANK個別銘柄ページを実際にHTTP取得し、年度別PBR高値・安値を返す。

    session: requests.Session（再利用してTCP接続を使い回すため。省略時はrequestsを直接使う）
    """
    import requests

    url = IRBANK_PBR_URL_TEMPLATE.format(code=code)
    headers = {
        "User-Agent": user_agent
        or "Mozilla/5.0 (compatible; core16-dividend-bot-research/1.0; +private backtest research)"
    }
    getter = session.get if session is not None else requests.get
    resp = getter(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return parse_pbr_table(resp.text)


def fetch_pbr_range_with_retry(
    code: str, session=None, timeout: int = 20, max_retries: int = 2, retry_wait_sec: float = 3.0
) -> list[PbrPeriod]:
    """fetch_pbr_rangeの単純リトライ版（一時的なネットワークエラー対策）。"""
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fetch_pbr_range(code, session=session, timeout=timeout)
        except Exception as e:  # noqa: BLE001 - 最終的に呼び出し元へ再送出するため広く捕捉
            last_err = e
            if attempt < max_retries:
                time.sleep(retry_wait_sec)
    assert last_err is not None
    raise last_err
