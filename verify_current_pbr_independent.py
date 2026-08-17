# -*- coding: utf-8 -*-
"""
【正しさの独立検証、読み取り専用の新規スクリプト】

compute_edinet_pbr_range.py（新方式）が算出する「現在のPBR」(Close / BVPS) が、
IRBANK逆算値との比較（新旧の一致度）ではなく、どちらの計算にも使っていない
第三の独立した情報源（証券会社・金融情報サイトの公表PBR）と突き合わせて
実際に正しいかを検証する。

背景（2026-08-17、ユーザー指摘）: 「新旧の乖離が小さい/大きい」は旧方式(IRBANK)が
正しいという未検証の前提の上に立っており、正しさの証明にはならない
（tasks/lessons.md 2026-08-17分参照）。

このスクリプトはconfig変更・既存ファイル変更を一切行わない。新方式の計算過程を
再利用するのみで、compute_edinet_pbr_range.pyのロジックそのものは変更しない。

使い方:
    core16_dividend_botディレクトリで `python verify_current_pbr_independent.py` を実行する。
    16銘柄の「新方式が計算した直近PBR」を一覧出力するので、その値を外部の独立した
    情報源（証券会社の銘柄ページ等）の公表PBRと突き合わせて確認する。
"""
from __future__ import annotations

import json
from pathlib import Path

from compute_edinet_pbr_range import EDINET_DATA_PATH, compute_new_method_daily
from universe import CORE16_UNIVERSE


def main() -> int:
    edinet_data = json.loads(EDINET_DATA_PATH.read_text(encoding="utf-8"))

    results = {}
    for t in CORE16_UNIVERSE:
        code, name = t["code"], t["name"]
        new_df = compute_new_method_daily(code, edinet_data)
        if new_df is None or new_df["current_pbr"].dropna().empty:
            print(f"  {code} {name}: データ不足によりスキップ")
            continue
        latest = new_df["current_pbr"].dropna().iloc[-1]
        latest_date = new_df["current_pbr"].dropna().index[-1]
        latest_bvps = new_df["bvps"].dropna().iloc[-1]
        latest_close = new_df.loc[latest_date, "Close"]
        results[code] = {
            "name": name,
            "date": str(latest_date.date()),
            "close": float(latest_close),
            "bvps_used": float(latest_bvps),
            "current_pbr_new_method": float(latest),
        }
        print(f"  {code} {name}: {latest_date.date()} Close={latest_close:.1f} BVPS={latest_bvps:.2f} PBR(新方式)={latest:.3f}")

    out_path = Path(__file__).parent / "data_cache" / "verify_current_pbr_independent_result.json"
    tmp_path = out_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)
    print(f"\n完了。結果を {out_path} へ保存しました。")
    return 0


if __name__ == "__main__":
    exit(main())
