# -*- coding: utf-8 -*-
"""
コア16銘柄それぞれについて、yfinanceから日次株価を取得し、
data_cache/yfinance_prices/{code}.csv に銘柄ごとに保存する（実データ取得・読み取り専用スクリプト）。

取得する2系列（tasks/backtest_design_core16_dividend_range_strategy.md「実現可能な代替設計」の
「時点tでの現在PBRは日次算出できる」に対応）:
- Close      : 分割調整済み・配当未調整（現在PBR = Close ÷ その時点で最新のBPS の算出に使う。
               分割はIRBANKのPBR自体も比率であるため分割調整済みの株価と整合する）
- Adj Close  : 分割・配当調整済み（配当込みトータルリターンの算出に使う）

このプロジェクト(core16_dividend_bot)はyfinance呼び出しをこのファイルに閉じ、
tradingbot/bot/data.pyやswing_daily_bot/bot/data.py等の既存データ基盤はimportしない
（各BOTはコードを共有しない独立プロジェクトというCLAUDE.md方針に従う。API呼び出しパターン
[差分キャッシュ・スロットリング]は将来必要になれば個別に実装する。現状は16銘柄×1回きりの
取得のためyfinanceの規模的な負荷は小さく、既存基盤にある高度な差分キャッシュ機構は不要と判断）。

使い方:
    core16_dividend_botディレクトリで `python fetch_yfinance_price_data.py` を実行する。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import yfinance as yf
from curl_cffi.const import CurlHttpVersion
from curl_cffi.requests import Session as CurlCffiSession

from universe import CORE16_UNIVERSE

START_DATE = "2010-01-01"
END_DATE = None  # Noneなら最新日まで
REQUEST_INTERVAL_SEC = 1.5  # 銘柄間の最低待機時間
OUTPUT_DIR = Path(__file__).parent / "data_cache" / "yfinance_prices"

# 2026-08-13: クラウドルーティン環境（core16-signal-check、egressがプロキシ経由）で
# yfinance既定のcurl_cffiセッション（HTTP/2でブラウザTLS偽装）がConnection resetで
# 全銘柄失敗する障害が発生。curl_cffi公式FAQでも「プロキシ経由時のHTTP/2ストリーム
# エラーはHTTP/1.1固定が有効な対処」とされており、このセッションのbashサンドボックス
# （プロキシなし）ではHTTP/1.1固定でも従来通り正常取得できることを確認済み。
# ローカル環境（プロキシなし）への副作用は無い前提でHTTP/1.1固定のセッションを明示的に
# 生成しyf.Tickerへ渡す。
_yf_session = CurlCffiSession(impersonate="chrome", http_version=CurlHttpVersion.V1_1)

# 2010年からの取得を想定した場合の最低営業日数の目安（15年分なら3000日超が普通）。
# これを大きく下回る場合は取得漏れ・上場が新しい等を疑って警告する。
MIN_EXPECTED_ROWS = 500


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    failures: list[tuple[str, str, str]] = []
    saved = 0

    for i, ticker in enumerate(CORE16_UNIVERSE):
        code, name, symbol = ticker["code"], ticker["name"], ticker["yfinance_symbol"]
        print(f"[{i + 1}/{len(CORE16_UNIVERSE)}] {code} {name} ({symbol}) を取得中...")
        try:
            df = yf.Ticker(symbol, session=_yf_session).history(
                start=START_DATE, end=END_DATE, auto_adjust=False, actions=True
            )
        except Exception as e:  # noqa: BLE001 - 1銘柄の失敗で全体を止めず、最後に一覧報告する
            print(f"  失敗（例外・{type(e).__name__}）: {e}")
            failures.append((code, name, f"{type(e).__name__}: {e}"))
            continue

        if df.empty:
            print("  失敗: 空のデータフレームが返却された")
            failures.append((code, name, "empty dataframe"))
            continue
        if len(df) < MIN_EXPECTED_ROWS:
            print(f"  警告: 取得行数が{len(df)}行のみ（目安{MIN_EXPECTED_ROWS}行未満）。取得漏れの可能性あり")
        if (df["Close"] <= 0).any() or (df["Adj Close"] <= 0).any():
            print("  失敗: 0以下の株価が含まれる（データ異常）")
            failures.append((code, name, "non-positive price detected"))
            continue

        out_path = OUTPUT_DIR / f"{code}.csv"
        df[["Close", "Adj Close", "Dividends", "Stock Splits"]].to_csv(out_path, encoding="utf-8")
        saved += 1
        print(f"  成功: {len(df)}行（{df.index[0].date()} 〜 {df.index[-1].date()}） -> {out_path.name}")

        if i < len(CORE16_UNIVERSE) - 1:
            time.sleep(REQUEST_INTERVAL_SEC)

    print(f"\n{saved}/{len(CORE16_UNIVERSE)} 銘柄を保存: {OUTPUT_DIR}")

    if failures:
        print(f"\n失敗した銘柄（{len(failures)}件）:")
        for code, name, err in failures:
            print(f"  {code} {name}: {err}")
        print("\n一部銘柄の取得に失敗したため、キャッシュは不完全です。再実行するか原因を確認してください。")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
