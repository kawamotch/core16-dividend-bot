# -*- coding: utf-8 -*-
"""
コア16銘柄それぞれについて、IRBANKの個別銘柄ページから年度別PBR高値・安値を取得し、
data_cache/irbank_pbr_range.json にまとめて保存する（実データ取得・読み取り専用スクリプト）。

実行前提: test_irbank_pbr_range_synthetic.py が全件合格していること
（CLAUDE.md「合成データでのロジック自己テスト→実データでの本実行」の原則）。

レート制御: IRBANKの公式APIレート制限は不明なため、非商用の小規模研究目的として
安全側に倒し、リクエスト間に一定間隔を空ける（連続アクセスしない）。

失敗時の方針（2026-08-07パネルレビュー・SRE指摘）: 1銘柄が失敗しても他銘柄の取得は
続けるが、最後に失敗一覧を明示し、失敗が1件でもあれば異常終了コードを返す
（成功したふりをして不完全なキャッシュを「完了」扱いしない）。

使い方:
    core16_dividend_botディレクトリで `python fetch_pbr_range_data.py` を実行する。
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import requests

from bot.irbank_pbr_range import IrbankPageStructureError, fetch_pbr_range_with_retry
from universe import CORE16_UNIVERSE

REQUEST_INTERVAL_SEC = 3.0  # 銘柄間の最低待機時間（IRBANKへの配慮。連続アクセスしない）
OUTPUT_PATH = Path(__file__).parent / "data_cache" / "irbank_pbr_range.json"

# 「直近10年ローリングウィンドウ」の判定に最低限必要な期数の目安。
# これを大きく下回る銘柄があれば、取得漏れ・ページ構造変化を疑って警告する。
MIN_EXPECTED_PERIODS = 10


def main() -> int:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    results: dict[str, dict] = {}
    failures: list[tuple[str, str, str]] = []  # (code, name, error message)

    for i, ticker in enumerate(CORE16_UNIVERSE):
        code, name = ticker["code"], ticker["name"]
        print(f"[{i + 1}/{len(CORE16_UNIVERSE)}] {code} {name} を取得中...")
        try:
            periods = fetch_pbr_range_with_retry(code, session=session)
        except (IrbankPageStructureError, requests.RequestException) as e:
            print(f"  失敗: {e}")
            failures.append((code, name, str(e)))
        else:
            if len(periods) < MIN_EXPECTED_PERIODS:
                print(
                    f"  警告: 取得期数が{len(periods)}件のみ（目安{MIN_EXPECTED_PERIODS}件未満）。"
                    "上場が新しい銘柄でなければ取得漏れの可能性あり"
                )
            results[code] = {
                "name": name,
                "yfinance_symbol": ticker["yfinance_symbol"],
                "periods": [asdict(p) for p in periods],
            }
            print(f"  成功: {len(periods)}期分（{periods[0].fiscal_year_label} 〜 {periods[-1].fiscal_year_label}）")

        if i < len(CORE16_UNIVERSE) - 1:
            time.sleep(REQUEST_INTERVAL_SEC)

    output = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "https://irbank.net/",
        "tickers": results,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{len(results)}/{len(CORE16_UNIVERSE)} 銘柄を保存: {OUTPUT_PATH}")

    if failures:
        print(f"\n失敗した銘柄（{len(failures)}件）:")
        for code, name, err in failures:
            print(f"  {code} {name}: {err}")
        print("\n一部銘柄の取得に失敗したため、キャッシュは不完全です。再実行するか原因を確認してください。")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
