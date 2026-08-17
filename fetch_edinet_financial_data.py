# -*- coding: utf-8 -*-
"""
EDINET移行 Phase B: コア16銘柄の有価証券報告書docIDを探索し、XBRLからBVPS・配当データを取得する。

【探索方針】
EDINETの書類一覧APIは日付指定でしか書類を取得できず、企業を指定した検索はできない
（EDINET API仕様書「3-1-1 リクエストについて」）。そこで、日本の有価証券報告書が
「決算期末後3ヶ月以内」に提出される制度上の制約を利用し、各銘柄の決算月（IRBANK時代の
既存キャッシュdata_cache/irbank_pbr_range.jsonから実測値を取得。3月期11銘柄・12月期5銘柄）を
手がかりに、提出が見込まれる月だけをスキャンする。

【取得件数（2026-08-16、連結/非連結の取り違えバグ修正に伴い変更）】
当初は「1件の有価証券報告書に今期＋過去4期の5年分が収録されるため、直近＋5年前の2件で
10年分がつながる」という設計だったが、`bot/edinet_financials.py`の連結BVPS抽出方式の
うちIFRS方式（16銘柄中13銘柄が該当）で使う親会社株主帰属持分タグは**当期分1点のみ**しか
収録されておらず、過去4期分の時系列を持たないことが判明した（詳細は同ファイルのdocstring
「連結/非連結の取り違え」参照）。そのためIFRS方式の銘柄は**対象年数分（10年）の個別の
有価証券報告書を毎年1件ずつ取得**する（`TARGET_YEARS_BACK_IFRS`）。JGAAP方式・US GAAP方式は
元の設計どおり直近＋5年前の2件で足りる（`TARGET_YEARS_BACK_BARE`）が、うちSMFG・東京海上HDは
**さらに別の理由**（2026-08-16追加発覚）で毎年個別取得に変更した: 1書類内に収録される
Prior1〜4Yearの比較期間を、株式分割後の基準へ遡及修正するかどうかの一貫性が銘柄によって
異なり（`bot/edinet_financials.py`の`PER_YEAR_FETCH_CODES`参照）、日付ベースの分割調整
ロジックでは解決できないため、比較期間を使わずCurrentYearのみに頼る設計にした。
結果としてMUFG・コマツ（分割経験なし）だけが直近＋5年前の2件のままとなる。

同じ決算月グループの銘柄は、書類一覧APIの1日分レスポンスを使い回して複数銘柄を同時に
判定できる（日付ごとのAPI呼び出しをグループ内銘柄数×取得件数ではなく、月内の日数分に
抑える設計）。ただしJGAAP方式の銘柄がIFRS方式の銘柄と同じ決算月グループに属す場合、
両者が必要とするyears_backの和集合でスキャンしつつ、各銘柄は自分が必要とするyears_backの
書類のみを採用する（JGAAP方式の銘柄に不要な年の書類までダウンロードしないため）。

【逐次保存】
1銘柄・1書類の処理が終わるたびに結果をdata_cache/edinet_financial_data.jsonへ書き足す
（tasks/lessons.md「バッチ処理は完了ごとに逐次保存」の原則。中断時も途中経過が残る）。

【実データ取得のみ、config変更なし】
本スクリプトは新規ファイルであり、既存のcheck_signal.py・bot/pbr_signal.py等は一切変更しない
（Phase B: 差分比較・検証専用。本番切り替えはPhase C、要ユーザー許可）。

使い方:
    core16_dividend_botディレクトリで `python fetch_edinet_financial_data.py` を実行する。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from bot.edinet_client import (
    DOC_TYPE_TEISEI_YUKASHOKEN,
    DOC_TYPE_YUKASHOKEN,
    EdinetApiError,
    fetch_document_binary,
    fetch_document_list,
    filter_documents_by_edinet_code,
    load_api_key,
)
from bot.edinet_code_mapping import CORE16_EDINET_CODE_MAP
from bot.edinet_financials import (
    CONSOLIDATION_METHOD_BY_CODE,
    CONSOLIDATION_METHOD_IFRS_COMPUTED,
    CONSOLIDATION_METHOD_JGAAP_BARE,
    CONSOLIDATION_METHOD_USGAAP_BARE,
    PER_YEAR_FETCH_CODES,
    EdinetXbrlStructureError,
    find_xbrl_instance_in_zip,
    parse_financial_periods,
)
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
OUTPUT_PATH = DATA_CACHE / "edinet_financial_data.json"

# 各銘柄の決算月（data_cache/irbank_pbr_range.jsonの実測値から。2026-08-16確認）。
# 3月期11銘柄・12月期5銘柄。将来決算期変更があれば要更新（現状は変更を検知する仕組みは無い）。
FISCAL_YEAR_END_MONTH: dict[str, int] = {
    "2914": 12, "4452": 12, "4578": 12, "5108": 12, "6326": 12,  # 12月期グループ
    "4503": 3, "6301": 3, "8001": 3, "8031": 3, "8058": 3,
    "8306": 3, "8316": 3, "8697": 3, "8766": 3, "9432": 3, "9433": 3,  # 3月期グループ
}

# 提出は決算期末後おおむね3ヶ月以内という制度上の制約から、スキャン対象月を決算月+3とする
# （12月期→3月、3月期→6月）。
FILING_MONTH_OFFSET = 3

# 【2026-08-17検証・確定】17年への延長を試みたが、EDINET書類一覧API(type=2)は2016年より
# 前の日付に対して一律404 Not Foundを返すことを実データで確認した（全銘柄・両決算月グループで
# 再現、427件の404を記録）。つまり2017年（10年前）が現状取得可能な実用上の限界であり、
# 「10年」という当初設計は既にこのAPIの限界に達していたと判明（コード側の設計不足ではなく
# 外部APIの構造的制約）。17年分への再取得を試したが新規ドキュメントは1件も増えなかった
# （tasks/lessons.md参照）。この定数は10年のまま維持する。

# JGAAP方式・US GAAP方式(4銘柄): 「直近の提出分」と「その5年前の提出分」の2件で、
# 重複・欠落なく10年分がつながる（1件の有報にPrior1〜4Yearの5期分が収録されるため）
TARGET_YEARS_BACK_BARE = (0, 5)

# IFRS方式(12銘柄): 親会社株主帰属持分タグが当期分1点のみのため、対象10年分を毎年1件ずつ取得する
# （EDINET書類一覧APIが2016年以前を404で拒否するため、これが実用上の取得可能な限界）
TARGET_YEARS_BACK_IFRS = tuple(range(10))


def _years_back_for_code(code: str) -> tuple[int, ...]:
    """銘柄コードの連結BVPS抽出方式(CONSOLIDATION_METHOD_BY_CODE)に応じて、
    書類一覧APIで探索すべきyears_back（何年前の提出分を探すか）の集合を返す。

    PER_YEAR_FETCH_CODES（bot/edinet_financials.py参照。1書類内の比較期間の遡及修正が
    一貫しない銘柄）はJGAAP/US GAAP方式であっても、IFRS方式と同じ毎年個別取得にする
    （比較期間(Prior1〜4Year)に頼らずCurrentYearのみを使うため）。
    """
    if code in PER_YEAR_FETCH_CODES:
        return TARGET_YEARS_BACK_IFRS
    method = CONSOLIDATION_METHOD_BY_CODE.get(code)
    if method in (CONSOLIDATION_METHOD_JGAAP_BARE, CONSOLIDATION_METHOD_USGAAP_BARE):
        return TARGET_YEARS_BACK_BARE
    if method == CONSOLIDATION_METHOD_IFRS_COMPUTED:
        return TARGET_YEARS_BACK_IFRS
    raise ValueError(f"銘柄コード{code}のconsolidation methodが不明: {method}")


def _filing_month_windows(
    fiscal_end_month: int, years_back_list: tuple[int, ...], today: date
) -> list[tuple[int, date, date]]:
    """決算月から、years_back_listで指定した各年（何年前の提出分か）の提出月（1ヶ月分）の
    開始日・終了日のリストを返す。戻り値の各要素は(years_back, start, end)。"""
    filing_month = fiscal_end_month + FILING_MONTH_OFFSET
    if filing_month > 12:
        filing_month -= 12
    # filing_monthは1〜12に正規化済みなので、以降は「提出が行われる暦年」を直接
    # todayから求めればよい（決算月が年をまたぐこと自体はfiling_monthの正規化で吸収済み。
    # 過去バージョンではここでさらに年オフセットを加算していたが、それは「決算期の年」と
    # 「提出が行われる年」を混同した二重計算になっており、today基準の年またぎ判定と
    # ズレる不具合を生んでいた。実提出年だけを直接扱うことで単純化する）。
    base_filing_year = today.year
    if date(base_filing_year, filing_month, 28) > today:
        base_filing_year -= 1

    windows = []
    for years_back in years_back_list:
        candidate_year = base_filing_year - years_back
        start = date(candidate_year, filing_month, 1)
        # 月末日を計算（timedelta経由、外部ライブラリ非依存）
        if filing_month == 12:
            end = date(candidate_year, 12, 31)
        else:
            end = date(candidate_year, filing_month + 1, 1) - timedelta(days=1)
        windows.append((years_back, start, end))
    return windows


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _load_existing_output() -> dict:
    if OUTPUT_PATH.exists():
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"tickers": {}}


def _save_output(output: dict) -> None:
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    tmp_path = OUTPUT_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    tmp_path.replace(OUTPUT_PATH)  # atomic write


def discover_doc_ids_for_group(
    tickers_in_group: list[dict], fiscal_end_month: int, api_key: str, today: date
) -> dict[str, list[dict]]:
    """同一決算月グループの銘柄について、各銘柄が必要とするyears_back（JGAAP方式は直近+5年前、
    IFRS方式は直近10年毎年）の和集合で提出月をスキャンし、書類一覧APIの1日分レスポンスを
    グループ内全銘柄で使い回してdocIDを収集する。ただし採用（found[code]への追加）は、
    その銘柄が実際に必要とするyears_backの書類のみに限定する（JGAAP方式の銘柄に不要な年の
    書類までダウンロードしないため）。

    戻り値: {code: [{"docId":..., "periodEnd":..., "submitDateTime":...}, ...]}
    """
    edinet_code_by_code = {t["code"]: CORE16_EDINET_CODE_MAP[t["code"]] for t in tickers_in_group}
    years_back_by_code = {t["code"]: set(_years_back_for_code(t["code"])) for t in tickers_in_group}
    all_years_back = tuple(sorted({yb for ybs in years_back_by_code.values() for yb in ybs}))

    windows = _filing_month_windows(fiscal_end_month, all_years_back, today)
    found: dict[str, list[dict]] = {t["code"]: [] for t in tickers_in_group}

    for years_back, win_start, win_end in windows:
        print(f"  スキャン範囲(years_back={years_back}): {win_start} 〜 {win_end}")
        for day in _daterange(win_start, min(win_end, today)):
            try:
                day_data = fetch_document_list(day, api_key)
            except EdinetApiError as e:
                print(f"    警告: {day} の書類一覧取得に失敗: {e}")
                continue
            for code, edinet_code in edinet_code_by_code.items():
                if years_back not in years_back_by_code[code]:
                    continue  # この銘柄はこのyears_backを必要としない
                matches = filter_documents_by_edinet_code(
                    day_data, edinet_code, doc_type_codes=(DOC_TYPE_YUKASHOKEN, DOC_TYPE_TEISEI_YUKASHOKEN)
                )
                for m in matches:
                    found[code].append(
                        {
                            "docId": m["docID"],
                            "docTypeCode": m["docTypeCode"],
                            "periodEnd": m.get("periodEnd"),
                            "submitDateTime": m.get("submitDateTime"),
                        }
                    )
    return found


def main() -> int:
    api_key = load_api_key()
    today = date.today()
    output = _load_existing_output()

    groups: dict[int, list[dict]] = {}
    for t in CORE16_UNIVERSE:
        fye = FISCAL_YEAR_END_MONTH[t["code"]]
        groups.setdefault(fye, []).append(t)

    all_found: dict[str, list[dict]] = {}
    for fye_month, tickers_in_group in groups.items():
        names = ", ".join(f"{t['code']}({t['name']})" for t in tickers_in_group)
        print(f"\n=== 決算月{fye_month}月グループ（{len(tickers_in_group)}銘柄）: {names} ===")
        found = discover_doc_ids_for_group(tickers_in_group, fye_month, api_key, today)
        all_found.update(found)

    print("\n=== docID探索結果サマリ ===")
    for code, docs in all_found.items():
        name = next(t["name"] for t in CORE16_UNIVERSE if t["code"] == code)
        print(f"  {code} {name}: {len(docs)}件")
        for d in docs:
            print(f"    docId={d['docId']} type={d['docTypeCode']} period_end={d['periodEnd']} submitted={d['submitDateTime']}")

    # 各銘柄、見つかった書類（原則2件）についてXBRLを取得・パースし、逐次保存する
    print("\n=== XBRL取得・パース ===")
    for code, docs in all_found.items():
        name = next(t["name"] for t in CORE16_UNIVERSE if t["code"] == code)
        if not docs:
            print(f"  警告: {code} {name} は書類が1件も見つからなかった（スキャン窓の見直しが必要な可能性）")
            continue
        ticker_result = output["tickers"].setdefault(code, {"name": name, "documents": {}})
        for d in docs:
            doc_id = d["docId"]
            if doc_id in ticker_result["documents"]:
                continue  # 既に取得済み（再実行時のスキップ）
            try:
                zip_bytes = fetch_document_binary(doc_id, api_key, doc_type=1)
                xbrl_xml = find_xbrl_instance_in_zip(zip_bytes)
                periods = parse_financial_periods(xbrl_xml, code)
                ticker_result["documents"][doc_id] = {
                    "meta": d,
                    "bvps": [asdict(f) for f in periods["bvps"]],
                    "dps_total": [asdict(f) for f in periods["dps_total"]],
                    "dps_interim": [asdict(f) for f in periods["dps_interim"]],
                }
                print(f"  OK: {code} {name} docId={doc_id} (BVPS{len(periods['bvps'])}期・DPS合計{len(periods['dps_total'])}期・DPS中間{len(periods['dps_interim'])}期)")
            except (EdinetApiError, EdinetXbrlStructureError) as e:
                print(f"  失敗: {code} {name} docId={doc_id}: {type(e).__name__}: {e}")
                ticker_result["documents"][doc_id] = {"meta": d, "error": str(e)}
            _save_output(output)  # 1件ごとに逐次保存

    print(f"\n完了。結果を {OUTPUT_PATH} へ保存しました。")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
