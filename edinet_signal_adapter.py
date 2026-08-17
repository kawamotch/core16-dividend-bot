# -*- coding: utf-8 -*-
"""
【新規・読み取り専用アダプター】

既存バックテスト群（backtest_threshold_comparison.py等）が使う
`bot.pbr_signal.compute_daily_signal()`（IRBANKデータ源）と同じ出力インターフェース
（indexが日付、"range_position_pct"列を持つDataFrame）を、EDINETベースの新方式データ
（`compute_edinet_pbr_range.compute_new_method_daily`、Phase Bで実装・検証済みのロジック）で
提供する。既存の`bot/pbr_signal.py`・各backtest_*.pyは一切変更しない（新規ファイルのみ）。

これにより、既存バックテストスクリプトの新方式版（backtest_*_edinet.py）は
「データ読み込み部分をこのアダプター呼び出しに差し替えるだけ」で作れる
（分析ロジック本体は既存スクリプトと同一のものを流用する）。

使い方: 他のスクリプトから `from edinet_signal_adapter import compute_daily_signal_edinet` で
import する。単体実行はしない。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from compute_edinet_pbr_range import EDINET_DATA_PATH, compute_new_method_daily

_edinet_data_cache: dict | None = None


def _load_edinet_data() -> dict:
    global _edinet_data_cache
    if _edinet_data_cache is None:
        _edinet_data_cache = json.loads(EDINET_DATA_PATH.read_text(encoding="utf-8"))
    return _edinet_data_cache


def compute_daily_signal_edinet(code: str) -> pd.DataFrame | None:
    """EDINETベースの新方式で、1銘柄分の日次シグナルDataFrameを返す
    （bot.pbr_signal.compute_daily_signal()と同じ"range_position_pct"列を持つ）。
    データ不足銘柄はNoneを返す（呼び出し元はbot.pbr_signal版と同じくスキップする）。

    "Adj Close"（配当込み総リターン用の調整後終値）列も付与する。
    `compute_new_method_daily`が内部で使う`_load_price_df`は"Close"列（分割調整済みのみ、
    配当未調整）しか読み込まないため、そのまま使うと既存バックテスト群（bot.pbr_signal版、
    "Adj Close"を使ってフォワードリターンを計算）とフェアに比較できない
    （配当を無視した価格リターンだけで比較してしまう、2026-08-17発覚）。
    """
    edinet_data = _load_edinet_data()
    df = compute_new_method_daily(code, edinet_data)
    if df is None:
        return None
    price_path = Path(__file__).parent / "data_cache" / "yfinance_prices" / f"{code}.csv"
    price_df = pd.read_csv(price_path, index_col="Date", parse_dates=True)
    if price_df.index.tz is not None:
        price_df.index = price_df.index.tz_localize(None)
    df["Adj Close"] = price_df["Adj Close"].reindex(df.index).values
    return df
