# -*- coding: utf-8 -*-
"""
一時的な診断スクリプト（2026-08-13、core16-signal-checkクラウドルーティン環境での
yfinance全銘柄接続失敗の原因切り分け用）。

これまでの経緯:
- HTTP/1.1固定のcurl_cffiセッションに変更したが改善せず、`run_log_latest.txt`で
  例外の型が`SSLError`だと判明（Connection reset by peer）。HTTP/2ストリーム問題という
  当初の仮説は誤りだった可能性が高い（TLSハンドシェイク段階での失敗を示唆）。
- 一方、同じ環境でIRBANK（plain requests使用）へのアクセスは成功しており、
  「プロキシがYahoo Financeへの通信を丸ごとブロックしている」わけではなさそう。
- 過去の調査（同環境）では、curl_cffiのimpersonateを使わない生のHTTPS通信は疎通する
  （ただしYahoo側から429 Too Many Requestsが返る）ことも確認済み。

この診断スクリプトは、複数のシナリオを1銘柄・少数リクエストで切り分け、
「TLS証明書信頼の問題か」「TLSフィンガープリント（impersonate）自体が弾かれているのか」
「plain requestsなら通るのか」を特定する。実データ取得スクリプトではなく使い捨ての
診断用（原因が特定でき次第、本体の修正に反映した上でこのファイルは削除してよい）。

使い方:
    core16_dividend_botディレクトリで `python diagnose_yfinance_network.py` を実行する。
"""
from __future__ import annotations

import os
import re
import sys
import time

TEST_URL = "https://query1.finance.yahoo.com/v8/finance/chart/2914.T"
TEST_PARAMS = {"range": "5d", "interval": "1d"}


def _mask_proxy_url(val: str) -> str:
    """プロキシURLに認証情報(user:pass@)が含まれる場合はマスクする。"""
    return re.sub(r"//[^/@]+@", "//***:***@", val)


def print_env_vars() -> None:
    print("=== 関連環境変数 ===")
    keys = [
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy",
        "CURL_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    ]
    for k in keys:
        v = os.environ.get(k)
        print(f"  {k} = {_mask_proxy_url(v) if v else '(未設定)'}")
    print()


def try_scenario(name: str, fn) -> None:
    print(f"--- シナリオ: {name} ---")
    t0 = time.time()
    try:
        result = fn()
        print(f"  成功  elapsed={time.time() - t0:.1f}s  {result}")
    except Exception as e:  # noqa: BLE001 - 診断用途につき全例外を捕捉して記録
        print(f"  失敗（{type(e).__name__}）: {e}  elapsed={time.time() - t0:.1f}s")
    print()


def scenario_curl_cffi(impersonate: str | None, verify: bool, http_version=None) -> str:
    from curl_cffi.requests import Session as CurlCffiSession

    kwargs: dict = {}
    if impersonate is not None:
        kwargs["impersonate"] = impersonate
    if http_version is not None:
        kwargs["http_version"] = http_version
    s = CurlCffiSession(verify=verify, **kwargs)
    r = s.get(TEST_URL, params=TEST_PARAMS, timeout=15)
    return f"status={r.status_code} body_head={r.text[:80]!r}"


def scenario_plain_requests() -> str:
    import requests

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    r = requests.get(TEST_URL, params=TEST_PARAMS, headers=headers, timeout=15)
    return f"status={r.status_code} body_head={r.text[:80]!r}"


def main() -> int:
    print_env_vars()

    from curl_cffi.const import CurlHttpVersion

    try_scenario(
        "curl_cffi impersonate=chrome, verify=True, http1.1（現行fetch_yfinance_price_data.pyと同一設定）",
        lambda: scenario_curl_cffi("chrome", True, CurlHttpVersion.V1_1),
    )
    try_scenario(
        "curl_cffi impersonate=chrome, verify=False, http1.1（証明書信頼の問題か切り分け）",
        lambda: scenario_curl_cffi("chrome", False, CurlHttpVersion.V1_1),
    )
    try_scenario(
        "curl_cffi impersonate=chrome, verify=False, http2（デフォルトHTTPバージョン）",
        lambda: scenario_curl_cffi("chrome", False, None),
    )
    try_scenario(
        "curl_cffi impersonate=None（TLS偽装なし）, verify=True",
        lambda: scenario_curl_cffi(None, True, None),
    )
    try_scenario(
        "curl_cffi impersonate=safari15_5（別ブランドで再試行）, verify=False",
        lambda: scenario_curl_cffi("safari15_5", False, None),
    )
    try_scenario(
        "plain requests（curl_cffi不使用、ブラウザ風User-Agentのみ）",
        scenario_plain_requests,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
