# -*- coding: utf-8 -*-
"""
オンデマンド・シグナル照会（2026-08-10、12人パネル承認済み「常駐なし・発注なし」設計の実装）。

常時稼働のエンジン・自動発注・自動通知は一切持たない。呼ばれた時だけ実行し、
「前回チェック以降に新しく閾値30%以下（割安ゾーン）に入った銘柄」をまとめて報告する
（ユーザーが毎日聞くとは限らないため、空白期間はキャッチアップする設計）。

実行の流れ:
1. data_cache/last_check_state.json から前回チェック日を読む（無ければ「今回が初回」扱い）
2. PBRレンジデータ・配当データ（IRBANK）が古ければ（STALENESS_DAYS超）再取得、株価データは
   毎回軽量に再取得（fetch_yfinance_price_data.pyは16銘柄・数十秒程度で軽量なため、
   鮮度を優先し毎回実行する）
3. 全16銘柄のレンジ位置(%)・配当利回り(%)を計算し、bot.pbr_signal・backtest_holding_drawdown.py
   と同じ立ち上がりエッジ方式（閾値超過→再び以下に下がった日のみを「買いイベント」とみなす）で
   前回チェック日より後に発生したイベントを抽出
4. 結果をコンソールに人間可読な形で出力（クラウドルーティンの応答としてそのままユーザーに返る想定）。
   「レンジ位置30%以下 かつ 配当利回り3%以上」のAND条件を満たす銘柄は、2026-08-10の
   フルレビュー（配当利回りフィルター検証結果）に基づき特に強調表示する
   （backtest_dividend_yield_filter.py参照。ウォークフォワードOOSで現行のレンジ単体基準を
   上回った、この基準で最も有望な部分集合）。ただしレンジ単体シグナルも引き続き表示し、
   AND条件を満たさないことをもって「シグナルではない」とは扱わない（レビューでの合意事項）。
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
from backtest_dividend_yield_filter import build_dividend_yield_series, YIELD_THRESHOLD_PCT
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
STATE_PATH = DATA_CACHE / "last_check_state.json"
PBR_RANGE_PATH = DATA_CACHE / "irbank_pbr_range.json"
DIVIDEND_HISTORY_PATH = DATA_CACHE / "irbank_dividend_history.json"
STALENESS_DAYS = 25  # PBR/配当レンジデータ（四半期更新）を再取得しなおすまでの許容日数

# 配当利回りの異常値ガード（2026-08-10発覚）: bot/irbank_dividend.adjust_dividends_for_splits()は
# 1期に複数回の分割が絡む・期の途中で分割が起きる（分割前の中間配当＋分割後の期末配当が
# 単純合算される）ケースを正しく補正できず、実際にはあり得ない極端な利回りを生むことがある
# （実例: 伊藤忠2025-12の1:5分割をまたいだ期で利回り9.6%という非現実的な値になった）。
# 根本修正は共有モジュール(bot/irbank_dividend.py)の変更が必要でユーザー確認が必要なため、
# 応急処置としてこの閾値を超える利回りは「要確認」として扱い、AND条件の判定対象からも除外する
# （誤った高利回りで実際より魅力的に見せてしまう事故を防ぐ。閾値8%はコア16銘柄の実勢利回り
# （軒並み1〜5%程度）から見て十分に非現実的な水準）。
YIELD_OUTLIER_CEILING_PCT = 8.0


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


def _cache_file_is_stale(path: Path) -> bool:
    if not path.exists():
        return True
    with open(path, encoding="utf-8") as f:
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
    """レンジ位置(%)に加えて配当利回り(%)も計算する（dividend_yield_pct列）。
    配当データが無い/取得失敗の銘柄でもレンジ位置側の判定は継続できるよう、
    利回り計算の失敗は個別にNaN扱いにしてスキップする（1銘柄の欠損で全体を止めない）。"""
    with open(PBR_RANGE_PATH, encoding="utf-8") as f:
        pbr_data = json.load(f)

    div_data = {}
    if DIVIDEND_HISTORY_PATH.exists():
        with open(DIVIDEND_HISTORY_PATH, encoding="utf-8") as f:
            div_data = json.load(f).get("tickers", {})

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

            if code in div_data:
                try:
                    sig["dividend_yield_pct"] = build_dividend_yield_series(div_data[code]["periods"], price_df)
                except Exception as e:  # noqa: BLE001 - 利回り計算の失敗はレンジ判定に影響させない
                    print(f"  警告: {code} {ticker['name']} の配当利回り計算に失敗: {e}（レンジ位置判定は継続）")
                    sig["dividend_yield_pct"] = float("nan")
            else:
                sig["dividend_yield_pct"] = float("nan")

            signals[code] = sig
        except Exception as e:  # noqa: BLE001 - 1銘柄の失敗で全体を止めない
            print(f"  警告: {code} {ticker['name']} のシグナル計算に失敗: {e}。スキップ")
    return signals


def main() -> int:
    last_checked = _load_last_checked_date()
    is_first_run = last_checked is None
    print(f"前回チェック日: {last_checked or '(初回)'}")

    if _cache_file_is_stale(PBR_RANGE_PATH):
        ok = _run_fetch_script("fetch_pbr_range_data.py")
        if not ok:
            print("PBRレンジデータの取得に一部失敗しましたが、既存キャッシュで続行します。")
    else:
        print("PBRレンジデータは十分新しいため再取得をスキップ。")

    _run_fetch_script("fetch_yfinance_price_data.py")

    # 配当データ取得はfetch_yfinance_price_data.pyが保存する分割情報(Stock Splits列)に
    # 依存するため、必ず株価取得の後に実行する
    if _cache_file_is_stale(DIVIDEND_HISTORY_PATH):
        ok = _run_fetch_script("fetch_dividend_history_data.py")
        if not ok:
            print("配当データの取得に一部失敗しましたが、既存キャッシュ（無ければ利回り判定なし）で続行します。")
    else:
        print("配当データは十分新しいため再取得をスキップ。")

    print("\nシグナルを計算中...")
    signals = _compute_all_signals()
    if not signals:
        print("エラー: 全銘柄でシグナル計算に失敗しました。")
        return 1

    today_str = None
    # (code, name, date, range_position_pct, dividend_yield_pct_or_None, and_condition_met)
    new_events: list[tuple[str, str, pd.Timestamp, float, float | None, bool]] = []
    # (code, name, current_range_position_pct, current_dividend_yield_pct_or_None)
    current_status: list[tuple[str, str, float | None, float | None]] = []

    name_by_code = {t["code"]: t["name"] for t in CORE16_UNIVERSE}

    def _clean_yield(raw_val) -> float | None:
        """NaN→None。異常値（YIELD_OUTLIER_CEILING_PCT超）もNone扱いにする
        （分割またぎ等でのデータ異常を「利回り基準を満たさない」と同じ安全側に倒す。
        表示上は_fmt_yieldが元の値と要確認マークを別途出すため、ここではAND条件判定・
        現在値表示用の値そのものをNoneにはしない。代わりに呼び出し側でoutlier判定を都度行う）。"""
        return float(raw_val) if pd.notna(raw_val) else None

    def _is_outlier(y: float | None) -> bool:
        return y is not None and y > YIELD_OUTLIER_CEILING_PCT

    for code, sig in signals.items():
        valid = sig.dropna(subset=["range_position_pct"])
        if valid.empty:
            current_status.append((code, name_by_code[code], None, None))
            continue
        today_str = str(valid.index[-1].date())
        today_yield = _clean_yield(sig["dividend_yield_pct"].loc[valid.index[-1]])
        current_status.append((code, name_by_code[code], float(valid["range_position_pct"].iloc[-1]), today_yield))

        events = _buy_events(sig, THRESHOLD_PCT)

        def _event_row(ev_date: pd.Timestamp) -> tuple[str, str, pd.Timestamp, float, float | None, bool]:
            range_pct = float(valid["range_position_pct"].loc[ev_date])
            yield_pct = _clean_yield(sig["dividend_yield_pct"].loc[ev_date])
            and_met = yield_pct is not None and yield_pct >= YIELD_THRESHOLD_PCT and not _is_outlier(yield_pct)
            return (code, name_by_code[code], ev_date, range_pct, yield_pct, and_met)

        if is_first_run:
            # 初回は「今日時点で既にゾーン内か」だけを見る（過去16年分を一括報告しない）
            if events and events[-1] == valid.index[-1]:
                new_events.append(_event_row(events[-1]))
        else:
            cutoff = pd.Timestamp(last_checked)
            for ev_date in events:
                if ev_date > cutoff:
                    new_events.append(_event_row(ev_date))

    def _fmt_yield(y: float | None) -> str:
        if y is None:
            return "利回り不明"
        if _is_outlier(y):
            return f"利回り{y:.1f}%[要確認: 分割またぎの可能性、AND条件対象外]"
        return f"利回り{y:.1f}%"

    # 2026-08-10フルレビューの合意: 「レンジ30%以下 かつ 利回り3%以上」のAND条件は
    # ウォークフォワードOOSで単体基準を上回った最有望な部分集合として強調表示する。
    # ただしAND不成立でもレンジ単体シグナルとしては引き続き有効なため、除外はしない。
    and_events = [e for e in new_events if e[5]]
    other_events = [e for e in new_events if not e[5]]

    print(f"\n=== 割安シグナル（閾値{THRESHOLD_PCT}%以下、前回チェック以降の新規分） ===")
    if and_events:
        print(f"  ▼特に有望（レンジ{THRESHOLD_PCT}%以下 かつ 利回り{YIELD_THRESHOLD_PCT}%以上、OOS検証で優位性確認済み）")
        for code, name, date, pct, yield_pct, _ in sorted(and_events, key=lambda x: x[3]):
            print(f"    {date.date()}  {code} {name}  レンジ位置={pct:.1f}%  {_fmt_yield(yield_pct)}")
    if other_events:
        print(f"  ▼レンジ条件のみ成立（利回り{YIELD_THRESHOLD_PCT}%未満、または利回りデータ不明）")
        for code, name, date, pct, yield_pct, _ in sorted(other_events, key=lambda x: x[3]):
            print(f"    {date.date()}  {code} {name}  レンジ位置={pct:.1f}%  {_fmt_yield(yield_pct)}")
    if not new_events:
        print("  新規シグナルなし")

    print(f"\n=== 現在のレンジ位置・配当利回り（全16銘柄、参考） ===")
    for code, name, pct, yield_pct in sorted(current_status, key=lambda x: (x[2] is None, x[2] if x[2] is not None else 999)):
        pct_str = f"{pct:.1f}%" if pct is not None else "算出不能"
        and_met = (
            pct is not None and pct <= THRESHOLD_PCT
            and yield_pct is not None and yield_pct >= YIELD_THRESHOLD_PCT
            and not _is_outlier(yield_pct)
        )
        marker = " ★★AND条件成立" if and_met else (" ★割安ゾーン" if pct is not None and pct <= THRESHOLD_PCT else "")
        print(f"  {code} {name:10s}  レンジ={pct_str:>7}  {_fmt_yield(yield_pct):>10}{marker}")

    if today_str:
        _save_last_checked_date(today_str)
        print(f"\n前回チェック日を更新: {today_str}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
