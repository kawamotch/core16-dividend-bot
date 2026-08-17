# -*- coding: utf-8 -*-
"""
backtest_holding_drawdown.py のEDINET新方式版（2026-08-17、Phase B結論確認用）。

分析ロジック（立ち上がりエッジ方式の買いイベント、含み損益率のドローダウン計測）は
元スクリプトと完全に同一。唯一の違いは`_load_stock_series()`のデータ源で、
IRBANK(bot.pbr_signal)ではなくEDINETベースの新方式（edinet_signal_adapter）を使う。
既存ファイルは変更しない。

使い方:
    core16_dividend_botディレクトリで `python backtest_holding_drawdown_edinet.py` を実行する。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from edinet_signal_adapter import compute_daily_signal_edinet
from universe import CORE16_UNIVERSE

DATA_CACHE = Path(__file__).parent / "data_cache"
OUTPUT_PATH = DATA_CACHE / "holding_drawdown_edinet_result.json"

THRESHOLD_PCT = 30
MIN_HOLDINGS_FOR_STEADY_STATE = 8


def _load_stock_series(code: str) -> pd.DataFrame | None:
    """1銘柄分の (Adj Close, range_position_pct) を日次で返す。データが無ければNone。"""
    return compute_daily_signal_edinet(code)


def _buy_events(sig: pd.DataFrame, threshold: float) -> list[pd.Timestamp]:
    in_zone = sig["range_position_pct"] <= threshold
    events = []
    was_armed = True
    for date, flag in in_zone.items():
        if bool(flag) and was_armed:
            events.append(date)
            was_armed = False
        elif not bool(flag):
            was_armed = True
    return events


def _build_portfolio_timeline() -> tuple[pd.DataFrame, dict]:
    per_stock_events: dict[str, list[pd.Timestamp]] = {}
    price_series: dict[str, pd.Series] = {}

    for ticker in CORE16_UNIVERSE:
        code = ticker["code"]
        sig = _load_stock_series(code)
        if sig is None:
            continue
        valid = sig.dropna(subset=["range_position_pct"])
        if valid.empty:
            continue
        events = _buy_events(sig, THRESHOLD_PCT)
        per_stock_events[code] = events
        price_series[code] = sig["Adj Close"]

    all_dates = sorted(set().union(*[s.index for s in price_series.values()]))
    calendar = pd.DatetimeIndex(all_dates)

    shares_held = {code: 0 for code in per_stock_events}
    cash_invested_cum = 0.0
    market_value_series = []
    cost_basis_series = []
    n_holdings_series = []

    price_reindexed = {
        code: s.reindex(calendar).ffill() for code, s in price_series.items()
    }
    event_set = {code: set(evts) for code, evts in per_stock_events.items()}

    for date in calendar:
        for code, evts in event_set.items():
            if date in evts:
                price_today = price_reindexed[code].loc[date]
                if pd.notna(price_today) and price_today > 0:
                    shares_held[code] += 1
                    cash_invested_cum += float(price_today)

        mv = 0.0
        for code, n in shares_held.items():
            if n == 0:
                continue
            p = price_reindexed[code].loc[date]
            if pd.notna(p):
                mv += n * float(p)

        market_value_series.append(mv)
        cost_basis_series.append(cash_invested_cum)
        n_holdings_series.append(sum(1 for n in shares_held.values() if n > 0))

    timeline = pd.DataFrame(
        {
            "market_value": market_value_series,
            "cost_basis": cost_basis_series,
            "n_holdings": n_holdings_series,
        },
        index=calendar,
    )
    meta = {
        "n_stocks_with_events": len(per_stock_events),
        "total_buy_events": sum(len(v) for v in per_stock_events.values()),
        "buy_events_per_stock": {code: len(v) for code, v in per_stock_events.items()},
    }
    return timeline, meta


def _drawdown_stats(timeline: pd.DataFrame) -> dict:
    active = timeline[timeline["cost_basis"] > 0].copy()
    active["unrealized_pnl_pct"] = (active["market_value"] / active["cost_basis"] - 1.0) * 100
    active["peak_pnl_pct"] = active["unrealized_pnl_pct"].cummax()
    active["drawdown_from_peak_pt"] = active["unrealized_pnl_pct"] - active["peak_pnl_pct"]

    worst_idx = active["drawdown_from_peak_pt"].idxmin()
    worst_row = active.loc[worst_idx]

    overall = {
        "max_drawdown_from_peak_pt": round(float(active["drawdown_from_peak_pt"].min()), 2),
        "max_drawdown_date": str(worst_idx.date()),
        "unrealized_pnl_pct_at_max_dd": round(float(worst_row["unrealized_pnl_pct"]), 2),
        "ever_min_unrealized_pnl_pct": round(float(active["unrealized_pnl_pct"].min()), 2),
        "ever_min_date": str(active["unrealized_pnl_pct"].idxmin().date()),
        "final_unrealized_pnl_pct": round(float(active["unrealized_pnl_pct"].iloc[-1]), 2),
        "final_date": str(active.index[-1].date()),
    }

    steady = active[active["n_holdings"] >= MIN_HOLDINGS_FOR_STEADY_STATE].copy()
    if steady.empty:
        steady_stats = {"note": f"n_holdings>={MIN_HOLDINGS_FOR_STEADY_STATE}に達した期間なし"}
    else:
        steady["peak_pnl_pct_steady"] = steady["unrealized_pnl_pct"].cummax()
        steady["drawdown_from_peak_pt_steady"] = steady["unrealized_pnl_pct"] - steady["peak_pnl_pct_steady"]
        steady_worst_idx = steady["drawdown_from_peak_pt_steady"].idxmin()
        steady_stats = {
            "start_date": str(steady.index[0].date()),
            "max_drawdown_from_peak_pt": round(float(steady["drawdown_from_peak_pt_steady"].min()), 2),
            "max_drawdown_date": str(steady_worst_idx.date()),
            "ever_min_unrealized_pnl_pct": round(float(steady["unrealized_pnl_pct"].min()), 2),
        }

    yearly = []
    for year, grp in active.groupby(active.index.year):
        yearly.append(
            {
                "year": int(year),
                "n_holdings_end": int(grp["n_holdings"].iloc[-1]),
                "unrealized_pnl_pct_end": round(float(grp["unrealized_pnl_pct"].iloc[-1]), 2),
                "min_unrealized_pnl_pct_in_year": round(float(grp["unrealized_pnl_pct"].min()), 2),
            }
        )

    return {
        "overall_from_first_buy": overall,
        f"steady_state_n_holdings_gte_{MIN_HOLDINGS_FOR_STEADY_STATE}": steady_stats,
        "yearly_snapshot": yearly,
    }


def main() -> int:
    print(f"買いイベント(EDINET新方式、立ち上がりエッジ、閾値{THRESHOLD_PCT}%以下)を集計中...")
    timeline, meta = _build_portfolio_timeline()
    print(f"銘柄数(イベントあり): {meta['n_stocks_with_events']}  総買いイベント数: {meta['total_buy_events']}")
    for code, n in meta["buy_events_per_stock"].items():
        print(f"  {code}: {n}件")

    print("\nドローダウン計算中...")
    dd = _drawdown_stats(timeline)

    print("\n=== 全期間（初回買付以降） ===")
    o = dd["overall_from_first_buy"]
    print(f"  最大DD（ピークからの含み損益後退）: {o['max_drawdown_from_peak_pt']}pt（{o['max_drawdown_date']}時点、その日の含み損益={o['unrealized_pnl_pct_at_max_dd']}%）")
    print(f"  含み損益の史上最悪値: {o['ever_min_unrealized_pnl_pct']}%（{o['ever_min_date']}）")
    print(f"  直近の含み損益: {o['final_unrealized_pnl_pct']}%（{o['final_date']}）")

    steady_key = f"steady_state_n_holdings_gte_{MIN_HOLDINGS_FOR_STEADY_STATE}"
    print(f"\n=== 定常状態（保有銘柄数>={MIN_HOLDINGS_FOR_STEADY_STATE}になって以降） ===")
    print(json.dumps(dd[steady_key], ensure_ascii=False, indent=2))

    print("\n=== 年次スナップショット ===")
    for row in dd["yearly_snapshot"]:
        print(f"  {row['year']}: 保有{row['n_holdings_end']}銘柄  年末含み損益={row['unrealized_pnl_pct_end']}%  年中最悪={row['min_unrealized_pnl_pct_in_year']}%")

    result = {"meta": meta, "drawdown": dd, "threshold_pct": THRESHOLD_PCT}
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n結果を保存: {OUTPUT_PATH}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
