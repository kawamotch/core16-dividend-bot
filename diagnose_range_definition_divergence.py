# -*- coding: utf-8 -*-
"""
【診断専用、読み取り専用の新規スクリプト】

2026-08-17、EDINET新方式のバックテスト(backtest_threshold_comparison_edinet.py等)で
旧IRBANK方式に比べ超過リターンが大幅に縮小(+68.29pt→+7.76pt)した根本原因を特定する
（review-panel結論に基づく）。

対象銘柄（signal_agreement_rateが低く乖離の大きい銘柄を選ぶ）: 花王(4452、現在唯一の
ライブシグナル)・三井物産(8031、mean_abs_diff最大級)。

やること:
1. 両方式のrange_high/range_low/current_pbr/range_position_ptcを日次で計算
2. 「片方だけ閾値30%以下」になっている日を洗い出し、その前後で両方式のレンジ高安が
   どう推移しているかを具体的に確認する
3. 先読みバイアスの疑い（IRBANK方式の期次レンジ高安が、その時点でまだ市場に知られて
   いなかったはずの将来情報を含んでいないか）を、各期のdisclosure_dateと実際の
   価格推移を突き合わせて確認する
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from bot.pbr_signal import build_period_records, compute_daily_signal
from edinet_signal_adapter import compute_daily_signal_edinet

DATA_CACHE = Path(__file__).parent / "data_cache"
TARGET_CODES = ["4452", "8031"]  # 花王・三井物産
THRESHOLD = 30


def _load_irbank_sig(code: str) -> pd.DataFrame:
    with open(DATA_CACHE / "irbank_pbr_range.json", encoding="utf-8") as f:
        pbr_data = json.load(f)
    price_path = DATA_CACHE / "yfinance_prices" / f"{code}.csv"
    price_df = pd.read_csv(price_path, index_col="Date", parse_dates=True)
    if price_df.index.tz is not None:
        price_df.index = price_df.index.tz_localize(None)
    period_df = build_period_records(pbr_data["tickers"][code]["periods"])
    sig = compute_daily_signal(price_df[["Close"]], period_df)
    return sig, period_df


def main() -> int:
    report_lines: list[str] = []

    for code in TARGET_CODES:
        report_lines.append(f"\n{'=' * 70}\n銘柄コード {code}\n{'=' * 70}")

        irbank_sig, period_df = _load_irbank_sig(code)
        edinet_sig = compute_daily_signal_edinet(code)

        merged = pd.DataFrame(index=irbank_sig.index)
        merged["irbank_range_low"] = irbank_sig["range_low"]
        merged["irbank_range_high"] = irbank_sig["range_high"]
        merged["irbank_current_pbr"] = irbank_sig["current_pbr"]
        merged["irbank_pos"] = irbank_sig["range_position_pct"]
        merged["edinet_range_low"] = edinet_sig["range_low"].reindex(merged.index)
        merged["edinet_range_high"] = edinet_sig["range_high"].reindex(merged.index)
        merged["edinet_current_pbr"] = edinet_sig["current_pbr"].reindex(merged.index)
        merged["edinet_pos"] = edinet_sig["range_position_pct"].reindex(merged.index)
        merged = merged.dropna()

        # 年1回(各年12月最終営業日)のスナップショットで両方式のレンジ高安を比較
        report_lines.append("\n--- 年次スナップショット（レンジ高安・現在PBR・レンジ位置%の比較） ---")
        yearly = merged.groupby(merged.index.year).tail(1)
        for date, row in yearly.iterrows():
            report_lines.append(
                f"{date.date()}: "
                f"IRBANK[低={row['irbank_range_low']:.3f} 高={row['irbank_range_high']:.3f} "
                f"現在={row['irbank_current_pbr']:.3f} 位置={row['irbank_pos']:.1f}%]  "
                f"EDINET[低={row['edinet_range_low']:.3f} 高={row['edinet_range_high']:.3f} "
                f"現在={row['edinet_current_pbr']:.3f} 位置={row['edinet_pos']:.1f}%]"
            )

        # シグナル不一致日の抽出(片方だけ30%以下)
        irbank_signal = merged["irbank_pos"] <= THRESHOLD
        edinet_signal = merged["edinet_pos"] <= THRESHOLD
        disagree = merged[irbank_signal != edinet_signal]
        report_lines.append(f"\n--- シグナル不一致日数: {len(disagree)} / {len(merged)} ---")

        # 不一致の「立ち上がり」(前日は一致していたが今日から不一致になった日)だけを抜粋
        disagree_flag = (irbank_signal != edinet_signal)
        transitions = disagree_flag & ~disagree_flag.shift(1, fill_value=False)
        transition_dates = merged.index[transitions]
        report_lines.append(f"--- 不一致の立ち上がりイベント数: {len(transition_dates)} ---")
        for d in transition_dates[:15]:
            row = merged.loc[d]
            which = "IRBANKのみ買い" if irbank_signal.loc[d] else "EDINETのみ買い"
            report_lines.append(
                f"  {d.date()} [{which}]: "
                f"IRBANK[低={row['irbank_range_low']:.3f} 現在={row['irbank_current_pbr']:.3f} 位置={row['irbank_pos']:.1f}%]  "
                f"EDINET[低={row['edinet_range_low']:.3f} 現在={row['edinet_current_pbr']:.3f} 位置={row['edinet_pos']:.1f}%]"
            )

        # IRBANKの各期のdisclosure_dateと期間そのもの(先読み疑いの確認)
        report_lines.append("\n--- IRBANK各期のend_date・disclosure_date一覧（先読み確認用） ---")
        for _, p in period_df.iterrows():
            report_lines.append(
                f"  期末={p['end_date'].date()}  開示日(45日後)={p['disclosure_date'].date()}  "
                f"PBR高={p['pbr_high']}  PBR安={p['pbr_low']}  BPS(逆算)={p['bps']}"
            )

    output_text = "\n".join(report_lines)
    out_path = DATA_CACHE / "diagnose_range_definition_divergence_result.txt"
    out_path.write_text(output_text, encoding="utf-8")
    print(f"完了。結果を {out_path} へ保存しました（{len(output_text)}文字）。")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
