# -*- coding: utf-8 -*-
"""
コア16銘柄それぞれについて、IRBANKの個別銘柄ページから年度別配当を取得し、
yfinanceの分割データで補正した上で data_cache/irbank_dividend_history.json に保存する。

fetch_pbr_range_data.pyと同じ設計方針（レート制御・失敗時は継続して最後に一覧報告・
異常終了コード）に従う。

使い方:
    core16_dividend_botディレクトリで `python fetch_dividend_history_data.py` を実行する。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd
import requests

from bot.irbank_dividend import IrbankPageStructureError, adjust_dividends_for_splits, fetch_dividend_history
from universe import CORE16_UNIVERSE

REQUEST_INTERVAL_SEC = 3.0
OUTPUT_PATH = Path(__file__).parent / "data_cache" / "irbank_dividend_history.json"


def main() -> int:
    session = requests.Session()
    results: dict[str, dict] = {}
    failures: list[tuple[str, str, str]] = []

    for i, ticker in enumerate(CORE16_UNIVERSE):
        code, name = ticker["code"], ticker["name"]
        print(f"[{i + 1}/{len(CORE16_UNIVERSE)}] {code} {name} を取得中...")
        try:
            periods = fetch_dividend_history(code, session=session)
        except (IrbankPageStructureError, requests.RequestException) as e:
            print(f"  失敗: {e}")
            failures.append((code, name, str(e)))
        else:
            price_path = Path("data_cache/yfinance_prices") / f"{code}.csv"
            price_df = pd.read_csv(price_path, index_col="Date", parse_dates=True)
            splits = price_df["Stock Splits"]
            splits.index = splits.index.tz_localize(None)
            adjusted_df = adjust_dividends_for_splits(periods, splits)
            adjusted_df["end_date"] = adjusted_df["end_date"].astype(str)
            results[code] = {"name": name, "periods": adjusted_df.to_dict("records")}
            print(f"  成功: {len(periods)}期分")

        if i < len(CORE16_UNIVERSE) - 1:
            time.sleep(REQUEST_INTERVAL_SEC)

    output = {"fetched_at": pd.Timestamp.utcnow().isoformat(), "source": "https://irbank.net/", "tickers": results}
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n{len(results)}/{len(CORE16_UNIVERSE)} 銘柄を保存: {OUTPUT_PATH}")

    if failures:
        print(f"\n失敗した銘柄（{len(failures)}件）:")
        for code, name, err in failures:
            print(f"  {code} {name}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
