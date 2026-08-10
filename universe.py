# -*- coding: utf-8 -*-
"""
コア16銘柄 割安判定戦略バックテスト: 対象銘柄の固定リスト。

出典: tasks/backtest_design_core16_dividend_range_strategy.md の「対象銘柄(コア16)」。
このファイルは対象銘柄の定義のみを持つ（データ取得・検証ロジックは持たない）。
"""
from __future__ import annotations

# code: 証券コード（IRBANKのURLパスにそのまま使う）
# yfinance_symbol: yfinanceでの銘柄コード（東証は".T"サフィックス）
# name: 銘柄名（ログ・レポート表示用）
CORE16_UNIVERSE = [
    {"code": "2914", "yfinance_symbol": "2914.T", "name": "JT"},
    {"code": "8306", "yfinance_symbol": "8306.T", "name": "MUFG"},
    {"code": "8316", "yfinance_symbol": "8316.T", "name": "SMFG"},
    {"code": "8058", "yfinance_symbol": "8058.T", "name": "三菱商事"},
    {"code": "8031", "yfinance_symbol": "8031.T", "name": "三井物産"},
    {"code": "8001", "yfinance_symbol": "8001.T", "name": "伊藤忠"},
    {"code": "9432", "yfinance_symbol": "9432.T", "name": "NTT"},
    {"code": "9433", "yfinance_symbol": "9433.T", "name": "KDDI"},
    {"code": "8766", "yfinance_symbol": "8766.T", "name": "東京海上HD"},
    {"code": "4503", "yfinance_symbol": "4503.T", "name": "アステラス製薬"},
    {"code": "4578", "yfinance_symbol": "4578.T", "name": "大塚HD"},
    {"code": "6301", "yfinance_symbol": "6301.T", "name": "コマツ"},
    {"code": "6326", "yfinance_symbol": "6326.T", "name": "クボタ"},
    {"code": "8697", "yfinance_symbol": "8697.T", "name": "日本取引所G"},
    {"code": "4452", "yfinance_symbol": "4452.T", "name": "花王"},
    {"code": "5108", "yfinance_symbol": "5108.T", "name": "ブリヂストン"},
]

assert len(CORE16_UNIVERSE) == 16, "コア16銘柄の定義数が16件でない"
assert len({t["code"] for t in CORE16_UNIVERSE}) == 16, "証券コードに重複がある"
