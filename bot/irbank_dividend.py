# -*- coding: utf-8 -*-
"""
IRBANK(irbank.net)の個別銘柄ページ（/{code}/dividend、内部的にEDINETコードへ301リダイレクト
されることがあるため、取得側はリダイレクト追従が必須）から、年度ごとの1株配当合計を
取得・パースする。bot/irbank_pbr_range.pyと同じ設計方針（パース関数はHTML文字列を受け取る
純粋関数にしてネットワーク取得と分離、構造不一致はサイレント欠損させず例外送出）に従う。

「実績/予想/修正」区分について（2026-08-07調査）: IRBANKページ内にこの区分の明示的な
説明は無く、確定した過去年度でも「予想」表記のままになっているケースがある（例: JTの
2014年12月期は既に確定済みのはずだが表記は「予想」のまま）。区分ラベルだけでは
「まだ支払われていない将来の配当」を判別できないと判断し、本モジュールは区分ラベルに
依存しない設計にする。「まだ確定していない直近の進行期」の除外は、呼び出し側で
期末日と基準日（データ取得日）を比較して行う（このモジュールは生データを忠実に返すのみ）。

株式分割の影響: 配当額は分割時点で表示単位が変わる（例: JTは2012年7月1日に1:200分割、
分割前は5,800円等の表示だったものが分割後は68円等になる）。この生の配当額をそのまま
比較すると壊滅的な「減配」に見えてしまうため、adjust_dividends_for_splits()で
yfinanceのStock Splits列（fetch_yfinance_price_data.pyが保存済み）を使って調整する。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd
from bs4 import BeautifulSoup

IRBANK_DIVIDEND_URL_TEMPLATE = "https://irbank.net/{code}/dividend"

## 銘柄によっては「分割調整」列が追加され、ヘッダーの列数・並びが変わることが判明
## （2026-08-07、花王(4452)で確認: 区分・中間・期末・合計の後に分割調整列が入る銘柄がある）。
## 完全一致ではなく、実際に使う先頭5列（年度・区分・中間・期末・合計）だけを検証することで、
## 末尾列の増減に影響されない設計にする。
_REQUIRED_HEADER_PREFIX = ["年度", "区分", "中間", "期末", "合計"]
_MIN_DATA_ROW_CELLS = len(_REQUIRED_HEADER_PREFIX)
_FISCAL_YEAR_RE = re.compile(r"(\d{4})年(\d{1,2})月$")


@dataclass(frozen=True)
class DividendPeriod:
    fiscal_year_label: str  # 例: "2014年12月"（PBRテーブルと異なり末尾に「期」が付かない）
    end_year: int
    end_month: int
    dps_total: float | None  # 1株配当合計（円）。分割未調整の生の値


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


def parse_dividend_table(html: str) -> list[DividendPeriod]:
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")

    target_table = None
    for table in tables:
        rows = table.find_all("tr")
        if not rows:
            continue
        header0 = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        if header0[: len(_REQUIRED_HEADER_PREFIX)] == _REQUIRED_HEADER_PREFIX:
            target_table = table
            break

    if target_table is None:
        raise IrbankPageStructureError(
            "年度別配当テーブルが見つからない"
            "（ヘッダー行の先頭列が想定[年度・区分・中間・期末・合計]と一致するtableが無い。"
            "IRBANKのページ構造が変更された可能性）"
        )

    rows = target_table.find_all("tr")
    periods: list[DividendPeriod] = []
    for row in rows[1:]:  # 0行目のみヘッダー（PBRテーブルと違い高値/安値のような2段ヘッダーは無い）
        cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
        if len(cells) < _MIN_DATA_ROW_CELLS:
            continue
        m = _FISCAL_YEAR_RE.match(cells[0])
        if not m:
            continue
        end_year, end_month = int(m.group(1)), int(m.group(2))
        dps_total = _parse_float_or_none(cells[4])
        periods.append(
            DividendPeriod(
                fiscal_year_label=cells[0],
                end_year=end_year,
                end_month=end_month,
                dps_total=dps_total,
            )
        )

    if not periods:
        raise IrbankPageStructureError(
            "年度別配当テーブルは見つかったが、パースできたデータ行が0件"
            "（年度ラベルの正規表現が想定と不一致の可能性）"
        )

    return periods


def fetch_dividend_history(code: str, session=None, timeout: int = 20, user_agent: str | None = None) -> list[DividendPeriod]:
    import requests

    url = IRBANK_DIVIDEND_URL_TEMPLATE.format(code=code)
    headers = {
        "User-Agent": user_agent
        or "Mozilla/5.0 (compatible; core16-dividend-bot-research/1.0; +private backtest research)"
    }
    getter = session.get if session is not None else requests.get
    # IRBANKは/{code}/dividendを内部的にEDINETコードへ301リダイレクトすることがある
    # （requestsはデフォルトでリダイレクトに追従するため明示指定は不要だが、意図を明記する）。
    resp = getter(url, headers=headers, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return parse_dividend_table(resp.text)


def period_end_date(end_year: int, end_month: int) -> pd.Timestamp:
    import calendar

    last_day = calendar.monthrange(end_year, end_month)[1]
    return pd.Timestamp(end_year, end_month, last_day)


def adjust_dividends_for_splits(
    periods: list[DividendPeriod], splits: pd.Series
) -> pd.DataFrame:
    """生の年度別配当(dps_total)を、その期末日より後に発生した株式分割の累積比率で
    割り戻し、現在の株式数ベースに揃えた調整後配当(dps_adjusted)を返す。

    splits: yfinanceのStock Splits列そのもの（0以外の値=分割比率、例: 200.0="1株→200株"）。
            fetch_yfinance_price_data.pyが保存したCSVの"Stock Splits"列を渡す想定。
    """
    splits = splits[splits != 0]
    split_events = [(pd.Timestamp(idx).tz_localize(None) if pd.Timestamp(idx).tz else pd.Timestamp(idx), ratio)
                     for idx, ratio in splits.items()]

    rows = []
    for p in periods:
        end_date = period_end_date(p.end_year, p.end_month)
        cumulative_ratio = 1.0
        for split_date, ratio in split_events:
            if split_date > end_date:
                cumulative_ratio *= ratio
        dps_adjusted = p.dps_total / cumulative_ratio if p.dps_total is not None else None
        rows.append(
            {
                "fiscal_year_label": p.fiscal_year_label,
                "end_date": end_date,
                "dps_total_raw": p.dps_total,
                "dps_adjusted": dps_adjusted,
            }
        )
    return pd.DataFrame(rows).sort_values("end_date").reset_index(drop=True)
