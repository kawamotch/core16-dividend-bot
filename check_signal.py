# -*- coding: utf-8 -*-
"""
オンデマンド・シグナル照会（2026-08-10、12人パネル承認済み「常駐なし・発注なし」設計の実装）。

常時稼働のエンジン・自動発注・自動通知は一切持たない。呼ばれた時だけ実行し、
「前回チェック以降に新しく閾値30%以下（割安ゾーン）に入った銘柄」をまとめて報告する
（ユーザーが毎日聞くとは限らないため、空白期間はキャッチアップする設計）。

実行の流れ:
1. data_cache/last_check_state.json から前回チェック日を読む（無ければ「今回が初回」扱い）
2. PBRレンジデータ（IRBANK）が古ければ（STALENESS_DAYS超）再取得、株価データは毎回軽量に再取得
   （fetch_yfinance_price_data.pyは16銘柄・数十秒程度で軽量なため、鮮度を優先し毎回実行する）
3. 全16銘柄のレンジ位置(%)を計算し、bot.pbr_signal・backtest_holding_drawdown.pyと同じ
   立ち上がりエッジ方式（閾値超過→再び以下に下がった日のみを「買いイベント」とみなす）で
   前回チェック日より後に発生したイベントを抽出
4. 結果をコンソールに人間可読な形で出力（クラウドルーティンの応答としてそのままユーザーに返る想定）
5. last_check_state.jsonを更新

このスクリプト自体は発注・通知配信を一切行わない（照会結果を返すのみ）。
実発注コードは絶対に実装しない（CLAUDE.md絶対厳守ルール）。

使い方:
    core16_dividend_botディレクトリで `python check_signal.py` を実行する。
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from bot.pbr_signal import build_period_records, compute_daily_signal
from backtest_holding_drawdown import _buy_events, THRESHOLD_PCT
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
STATE_PATH = DATA_CACHE / "last_check_state.json"
PBR_RANGE_PATH = DATA_CACHE / "irbank_pbr_range.json"
STALENESS_DAYS = 25  # PBRレンジデータ（四半期更新）を再取得しなおすまでの許容日数


def _load_last_checked_date() -> str | None:
    if not STATE_PATH.exists():
        return None
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f).get("last_checked_date")


def _save_last_checked_date(date_str: str) -> None:
    tmp_path = STATE_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"last_checked_date": date_str, "updated_at": datetime.now(timezone.utc).isoformat()}, f, ensure_ascii=False, indent=2)
    tmp_path.replace(STATE_PATH)  # atomic write（tasks/lessons.md標準パターン）


def _pbr_range_data_is_stale() -> bool:
    if not PBR_RANGE_PATH.exists():
        return True
    with open(PBR_RANGE_PATH, encoding="utf-8") as f:
        fetched_at = json.load(f).get("fetched_at")
    if not fetched_at:
        return True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
    return age > timedelta(days=STALENESS_DAYS)


def _run_fetch_script(script_name: str) -> bool:
    print(f"--- {script_name} を実行中 ---")
    result = subprocess.run([sys.executable, script_name], cwd=Path(__file__).parent)
    return result.returncode == 0


def _compute_all_signals() -> dict[str, pd.DataFrame]:
    with open(PBR_RANGE_PATH, encoding="utf-8") as f:
        pbr_data = json.load(f)

    signals = {}
    for ticker in CORE16_UNIVERSE:
        code = ticker["code"]
        price_path = DATA_CACHE / "yfinance_prices" / f"{code}.csv"
        if not price_path.exists():
            print(f"  警告: {code} {ticker['name']} の価格データが無い（取得失敗の可能性）。スキップ")
            continue
        try:
            price_df = pd.read_csv(price_path, index_col="Date", parse_dates=True)
            if price_df.index.tz is not None:
                price_df.index = price_df.index.tz_localize(None)
            period_df = build_period_records(pbr_data["tickers"][code]["periods"])
            sig = compute_daily_signal(price_df[["Close"]], period_df)
            signals[code] = sig
        except Exception as e:  # noqa: BLE001 - 1銘柄の失敗で全体を止めない
            print(f"  警告: {code} {ticker['name']} のシグナル計算に失敗: {e}。スキップ")
    return signals


def main() -> int:
    last_checked = _load_last_checked_date()
    is_first_run = last_checked is None
    print(f"前回チェック日: {last_checked or '(初回)'}")

    if _pbr_range_data_is_stale():
        ok = _run_fetch_script("fetch_pbr_range_data.py")
        if not ok:
            print("PBRレンジデータの取得に一部失敗しましたが、既存キャッシュで続行します。")
    else:
        print("PBRレンジデータは十分新しいため再取得をスキップ。")

    _run_fetch_script("fetch_yfinance_price_data.py")

    print("\nシグナルを計算中...")
    signals = _compute_all_signals()
    if not signals:
        print("エラー: 全銘柄でシグナル計算に失敗しました。")
        return 1

    today_str = None
    new_events: list[tuple[str, str, pd.Timestamp, float]] = []  # (code, name, date, range_position_pct)
    current_status: list[tuple[str, str, float | None]] = []  # (code, name, current_range_position_pct)

    name_by_code = {t["code"]: t["name"] for t in CORE16_UNIVERSE}

    for code, sig in signals.items():
        valid = sig.dropna(subset=["range_position_pct"])
        if valid.empty:
            current_status.append((code, name_by_code[code], None))
            continue
        today_str = str(valid.index[-1].date())
        current_status.append((code, name_by_code[code], float(valid["range_position_pct"].iloc[-1])))

        events = _buy_events(sig, THRESHOLD_PCT)
        if is_first_run:
            # 初回は「今日時点で既にゾーン内か」だけを見る（過去16年分を一括報告しない）
            if events and events[-1] == valid.index[-1]:
                new_events.append((code, name_by_code[code], events[-1], float(valid["range_position_pct"].loc[events[-1]])))
        else:
            cutoff = pd.Timestamp(last_checked)
            for ev_date in events:
                if ev_date > cutoff:
                    new_events.append((code, name_by_code[code], ev_date, float(valid["range_position_pct"].loc[ev_date])))

    print(f"\n=== 割安シグナル（閾値{THRESHOLD_PCT}%以下、前回チェック以降の新規分） ===")
    if new_events:
        for code, name, date, pct in sorted(new_events, key=lambda x: x[2]):
            print(f"  {date.date()}  {code} {name}  レンジ位置={pct:.1f}%")
    else:
        print("  新規シグナルなし")

    print(f"\n=== 現在のレンジ位置（全16銘柄、参考） ===")
    for code, name, pct in sorted(current_status, key=lambda x: (x[2] is None, x[2] if x[2] is not None else 999)):
        pct_str = f"{pct:.1f}%" if pct is not None else "算出不能"
        marker = " ★割安ゾーン" if pct is not None and pct <= THRESHOLD_PCT else ""
        print(f"  {code} {name:10s}  {pct_str}{marker}")

    if today_str:
        _save_last_checked_date(today_str)
        print(f"\n前回チェック日を更新: {today_str}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
