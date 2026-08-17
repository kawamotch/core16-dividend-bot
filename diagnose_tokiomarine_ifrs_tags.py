# -*- coding: utf-8 -*-
"""
【診断専用、読み取り専用の新規スクリプト】

東京海上HD(8766)の2026年3月期分(docID: S100YLS8)の生XBRLを直接確認し、IFRS移行に伴う
BVPS抽出バグの根本原因を確定診断する（2026-08-17、review-panelの結論「着手前に生XBRLを
直接確認してから直す」に対応）。

既存の`CONSOLIDATION_METHOD_BY_CODE`は東京海上HDをJGAAPパターン(bare context)に固定分類して
おり、`NetAssetsPerShareSummaryOfBusinessResults`のCurrentYearInstant contextから値
2885.44円を抽出しているが、独立検証(Yahoo!ファイナンス実績PBRとの突き合わせ)でこれが
実勢(逆算約4309円)より33%低いという異常が判明している。

このスクリプトは既存ファイルを一切変更せず、生XBRLの中身を出力するだけ。
"""
from __future__ import annotations

import json
from pathlib import Path

from bot.edinet_client import fetch_document_binary, load_api_key
from bot.edinet_financials import find_xbrl_instance_in_zip, parse_xbrl_contexts, _local_name
from xml.etree import ElementTree as ET

DOC_ID = "S100YLS8"
OUTPUT_DIR = Path(__file__).parent / "data_cache"


def main() -> int:
    api_key = load_api_key()
    print(f"書類取得API呼び出し中: doc_id={DOC_ID}")
    zip_bytes = fetch_document_binary(DOC_ID, api_key)
    xbrl_xml = find_xbrl_instance_in_zip(zip_bytes)
    print(f"XBRLインスタンス取得済み: {len(xbrl_xml)}文字")

    contexts = parse_xbrl_contexts(xbrl_xml)
    print(f"context数: {len(contexts)}")

    # 1. NetAssetsPerShareSummaryOfBusinessResults の全contextRefと値を出力
    root = ET.fromstring(xbrl_xml)
    lines = []
    lines.append("=== NetAssetsPerShareSummaryOfBusinessResults の全出現 ===")
    for elem in root.iter():
        if _local_name(elem.tag) == "NetAssetsPerShareSummaryOfBusinessResults":
            ctx = elem.get("contextRef")
            lines.append(f"  contextRef={ctx}  period_end={contexts.get(ctx)}  value={elem.text}")

    # 2. 「1株」「PerShare」「NetAssets」「Equity」を含む主要タグ名を洗い出す（IFRS版の正しいタグ名を探す）
    lines.append("\n=== タグ名に PerShare / NetAssets / Equity を含む要素（重複除去、bare/CurrentYearのみ抜粋）===")
    seen_tags = set()
    for elem in root.iter():
        name = _local_name(elem.tag)
        if ("PerShare" in name or "NetAssets" in name or "Equity" in name) and name not in seen_tags:
            ctx = elem.get("contextRef")
            if ctx and "CurrentYear" in ctx:
                seen_tags.add(name)
                lines.append(f"  tag={name}  contextRef={ctx}  value={elem.text}")

    # 3. IFRS特有のタグ(EquityAttributableToOwnersOfParentIFRS系)がこの書類に存在するか確認
    lines.append("\n=== IFRS関連タグ(EquityAttributableToOwnersOfParentIFRS*)の全出現 ===")
    for elem in root.iter():
        name = _local_name(elem.tag)
        if "EquityAttributableToOwnersOfParent" in name:
            ctx = elem.get("contextRef")
            lines.append(f"  tag={name}  contextRef={ctx}  period_end={contexts.get(ctx)}  value={elem.text}")

    # 4. 発行済株式数・自己株式数タグの存在確認（分母側）
    lines.append("\n=== 株式数関連タグ ===")
    for elem in root.iter():
        name = _local_name(elem.tag)
        if name in ("NumberOfSharesIssuedSharesVotingRights", "TotalNumberOfSharesHeldTreasurySharesEtc"):
            ctx = elem.get("contextRef")
            if ctx and "CurrentYear" in ctx:
                lines.append(f"  tag={name}  contextRef={ctx}  value={elem.text}")

    output_text = "\n".join(lines)

    out_path = OUTPUT_DIR / "diagnose_tokiomarine_ifrs_tags_result.txt"
    out_path.write_text(output_text, encoding="utf-8")
    print(f"\n結果を保存: {out_path}")

    # 生XBRL自体も保存（追加調査用）
    xbrl_path = OUTPUT_DIR / "tokiomarine_S100YLS8_raw.xbrl"
    xbrl_path.write_text(xbrl_xml, encoding="utf-8")
    print(f"生XBRLを保存: {xbrl_path}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
