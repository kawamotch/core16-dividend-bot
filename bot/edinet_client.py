# -*- coding: utf-8 -*-
"""
EDINET API（v2）の薄いクライアントラッパー。

IRBANKスクレイピングが利用規約違反と判明した件（2026-08-14）を受けた代替データソース。
書類一覧API・書類取得APIへのHTTPリクエストのみを担当し、XBRLのパース等のロジックは
bot/edinet_financials.pyに分離する（tasks/lessons.md「重い処理・外部依存とロジックの分離」の
原則、bot/irbank_pbr_range.pyと同じ設計方針。ネットワーク不要な合成データテストを別途書ける
ようにするため）。

EDINET公式利用規約（2026-08-16確認、一次情報:
https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0030.html）は、
ウェブサイトへのスクレイピングは禁止する一方「本ウェブサイトのコンテンツを機械的に取得するには
API機能を利用してください」とAPI経由の機械取得を明示的に推奨している（IRBANKの規約とは正反対の
位置づけ）。禁止されるのは「短時間における大量のアクセスその他のAPI機能の運用に支障を与える行為」
のみで具体的なリクエスト数の上限は非公開のため、本モジュールは保守的にリクエスト間隔を空ける
（_REQUEST_INTERVAL_SEC）。429（Too Many Requests）を受けた場合は例外を送出し、呼び出し元が
無言でリトライを繰り返して規約違反状態を継続しないようにする（EDINET API仕様書 v2.9
「3-3 ステータスコード」準拠）。

APIキーの保管場所: core16_dividend_bot/secrets/edinet_credentials.json の"subscription_key"
キー（tradingbot/secrets/kabu_credentials.jsonと同じパターン、.gitignoreで除外済み）。
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

EDINET_API_BASE = "https://api.edinet-fsa.go.jp/api/v2"
DEFAULT_CREDENTIALS_PATH = Path(__file__).parent.parent / "secrets" / "edinet_credentials.json"

# 規約に具体的なレート上限が明記されていないため、安全側に倹約的な間隔を設定する
# （Web調査で確認した実務上の目安「1リクエスト3〜5秒間隔が無難」を踏まえた保守値）。
_REQUEST_INTERVAL_SEC = 3.0

# 書類種別コード: 有価証券報告書・訂正有価証券報告書（EDINET API仕様書「4-1 参考資料」書類種別コード）
DOC_TYPE_YUKASHOKEN = "120"
DOC_TYPE_TEISEI_YUKASHOKEN = "130"


class EdinetApiError(Exception):
    """EDINET APIがエラーレスポンスを返した場合、または通信自体に失敗した場合。

    呼び出し元がサイレントに欠損データとして扱わず気づけるよう、明示的に送出する
    （bot/irbank_pbr_range.pyのIrbankPageStructureErrorと同じ設計方針）。
    """


_last_request_monotonic: float = 0.0


def load_api_key(path: Path | None = None) -> str:
    """secrets/edinet_credentials.json からAPIキー（subscription_key）を読み込む。

    認証情報の値そのものはログ・例外メッセージに一切含めない（CLAUDE.md「認証情報の扱い」）。
    """
    cred_path = path or DEFAULT_CREDENTIALS_PATH
    if not cred_path.exists():
        raise EdinetApiError(f"EDINET認証情報ファイルが見つからない: {cred_path}")
    with open(cred_path, encoding="utf-8") as f:
        data = json.load(f)
    key = data.get("subscription_key")
    if not key:
        raise EdinetApiError(f"{cred_path} に subscription_key が設定されていない")
    return key


def _throttled_get(url: str, params: dict, timeout: int):
    """次のリクエストまで最低_REQUEST_INTERVAL_SEC秒空けてから送信する。

    tasks/lessons.md「レート制限のある外部API呼び出しの並列化」の原則（次に送信してよい時刻を
    共有変数で管理）を単一スレッド用に簡略化した版。本プロジェクトの呼び出しは逐次実行のみを
    想定するため並列化は行わない（規約の「短時間大量アクセス禁止」を踏まえ、そもそも並列化は
    しない方針）。
    """
    import requests

    global _last_request_monotonic
    elapsed = time.monotonic() - _last_request_monotonic
    if elapsed < _REQUEST_INTERVAL_SEC:
        time.sleep(_REQUEST_INTERVAL_SEC - elapsed)
    try:
        resp = requests.get(url, params=params, timeout=timeout)
    finally:
        _last_request_monotonic = time.monotonic()
    return resp


def fetch_document_list(target_date: date, api_key: str, timeout: int = 20) -> dict:
    """書類一覧API（type=2、提出書類一覧及びメタデータ）で指定日1日分の提出書類一覧を取得する。

    EDINET API仕様書「3-1 書類一覧API」準拠。1日分のレスポンスには全提出者分の書類が含まれるため、
    複数銘柄を対象にする場合は同じ日付分のレスポンスを使い回す（銘柄ごとに日付をスキャンし直さない）
    設計を呼び出し元（fetch_edinet_financial_data.py、Phase B）で徹底する。
    """
    import requests

    url = f"{EDINET_API_BASE}/documents.json"
    params = {"date": target_date.isoformat(), "type": "2", "Subscription-Key": api_key}
    try:
        resp = _throttled_get(url, params, timeout=timeout)
    except requests.exceptions.RequestException as e:
        # タイムアウト・接続断等の一時的なネットワーク障害。呼び出し元（discover_doc_ids_for_group、
        # 数百日分の連続スキャンを行う）が1日分の失敗として握りつぶして続行できるよう、
        # 生のrequests例外のまま伝播させず統一的にEdinetApiErrorへ変換する（2026-08-16発覚。
        # 変換前は未捕捉のrequests.exceptions.ReadTimeoutでスクリプト全体がクラッシュし、
        # それまでの取得済みデータ以外がすべて失われていた）。
        raise EdinetApiError(f"書類一覧API 通信エラー（date={target_date}）: {type(e).__name__}: {e}") from e
    if resp.status_code == 429:
        raise EdinetApiError(
            f"429 Too Many Requests（date={target_date}）。リクエスト間隔を見直してください。"
        )
    resp.raise_for_status()
    data = resp.json()
    status = data.get("metadata", {}).get("status")
    if status not in ("200", None):
        message = data.get("metadata", {}).get("message")
        raise EdinetApiError(f"書類一覧API エラー（date={target_date}）: status={status} message={message}")
    return data


def filter_documents_by_edinet_code(
    document_list_response: dict,
    edinet_code: str,
    doc_type_codes: tuple[str, ...] = (DOC_TYPE_YUKASHOKEN, DOC_TYPE_TEISEI_YUKASHOKEN),
) -> list[dict]:
    """fetch_document_list()の結果から、指定EDINETコード・書類種別（既定: 有価証券報告書＋訂正）に
    一致し、かつ取下げられていない書類だけを抽出する純粋関数（ネットワーク不要、テスト容易）。
    """
    results = document_list_response.get("results") or []
    return [
        r
        for r in results
        if r.get("edinetCode") == edinet_code
        and r.get("docTypeCode") in doc_type_codes
        and r.get("withdrawalStatus") == "0"
    ]


def fetch_document_binary(doc_id: str, api_key: str, doc_type: int = 1, timeout: int = 60) -> bytes:
    """書類取得API。doc_type=1（既定、提出本文書及び監査報告書、ZIP形式でXBRLを同梱）を取得する。

    EDINET API仕様書「3-2 書類取得API」準拠。
    """
    import requests

    url = f"{EDINET_API_BASE}/documents/{doc_id}"
    params = {"type": str(doc_type), "Subscription-Key": api_key}
    try:
        resp = _throttled_get(url, params, timeout=timeout)
    except requests.exceptions.RequestException as e:
        # fetch_document_list()と同じ理由でEdinetApiErrorへ変換する（2026-08-16発覚）。
        raise EdinetApiError(f"書類取得API 通信エラー（doc_id={doc_id}）: {type(e).__name__}: {e}") from e
    if resp.status_code == 429:
        raise EdinetApiError(f"429 Too Many Requests（doc_id={doc_id}）。リクエスト間隔を見直してください。")
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "")
    if content_type.startswith("application/json"):
        # エラー時はHTTP 200のままJSONでエラー情報が返る（仕様書「3-3 ステータスコード」）
        data = resp.json()
        raise EdinetApiError(f"書類取得API エラー（doc_id={doc_id}）: {data}")
    return resp.content
