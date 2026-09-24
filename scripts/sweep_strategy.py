r"""전략 파라미터 스윕 — 최근 N년 캔들로 (마켓 × 캔들 단위 × 파라미터 × 리스크 오버레이) 백테스트를
돌려 **연도별로** 단순 보유(B&H)와 비교한다.
한 구간의 최고 숫자가 아니라 "여러 해·여러 마켓에서 꾸준히 이기는 조합"을 찾기 위한 도구.

    .venv\Scripts\python.exe scripts\sweep_strategy.py --markets KRW-BTC,KRW-ETH,KRW-XRP --intervals 240m,1d --years 5
    .venv\Scripts\python.exe scripts\sweep_strategy.py --overlays --top 6   # 상위 조합에 손절·추적 오버레이 검사

결과: data/sweeps/<시각>_runs.csv(조합·마켓별 전체) + <시각>_summary.csv(조합별 집계) + 콘솔 상위 표.
실제 주문 없음. 수수료 0.05%·슬리피지 0.05% 반영.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from app.backtest import BacktestConfig, BacktestEngine, load_candles  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.core.exceptions import TraderError  # noqa: E402
from app.exchange.models import KST, CandleInterval  # noqa: E402
from app.risk.config import RiskConfig  # noqa: E402
from app.strategy import create_strategy  # noqa: E402

FEE, SLIPPAGE = 0.0005, 0.0005

# ---------------------------------------------------------------- 후보 조합 (일부러 성긴 격자 — 과최적화 방지)
FILTER_SETS = {
    "필터없음": {"volume_window": 0, "volume_factor": 1.0, "rsi_window": 0, "rsi_max_for_buy": 70.0},
    "거래량": {"volume_window": 20, "volume_factor": 1.0, "rsi_window": 0, "rsi_max_for_buy": 70.0},
    "RSI70": {"volume_window": 0, "volume_factor": 1.0, "rsi_window": 14, "rsi_max_for_buy": 70.0},
    "RSI80": {"volume_window": 0, "volume_factor": 1.0, "rsi_window": 14, "rsi_max_for_buy": 80.0},
}
MA_SHORTS = (10, 20, 30, 50)
MA_LONGS = (60, 100, 150, 200)
RSI_WINDOWS = (7, 14, 21)
RSI_LEVELS = ((30.0, 70.0), (25.0, 75.0), (35.0, 65.0))

OVERLAYS = {
    "오버레이없음": {},
    "추적10%": {"trailing_stop_pct": 0.10},
    "추적15%": {"trailing_stop_pct": 0.15},
    "추적20%": {"trailing_stop_pct": 0.20},
    "손절8%": {"stop_loss_pct": 0.08},
    "손절12%": {"stop_loss_pct": 0.12},
    "손절12%+추적15%": {"stop_loss_pct": 0.12, "trailing_stop_pct": 0.15},
}


def variants() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for (short, long), (fname, filt) in itertools.product(itertools.product(MA_SHORTS, MA_LONGS), FILTER_SETS.items()):
        if short >= long:
            continue
        out.append({"strategy": "ma_cross", "label": f"SMA{short}/{long} {fname}",
                    "params": {"short_window": short, "long_window": long, **filt}})
    for window, (lo, hi) in itertools.product(RSI_WINDOWS, RSI_LEVELS):
        out.append({"strategy": "rsi", "label": f"RSI{window} {lo:g}/{hi:g}",
                    "params": {"window": window, "oversold": lo, "overbought": hi}})
    return out


# ---------------------------------------------------------------- 연도별 비교
def yearly_returns(equity: pd.Series) -> dict[int, float]:
    """연도별 수익률 (연초 값 = 직전 연도 마지막 값 → 자산이 이어진다)."""
    eq = equity.copy()
    idx = pd.DatetimeIndex(eq.index)
    idx = idx.tz_localize(UTC) if idx.tz is None else idx
    eq.index = idx.tz_convert(KST)
    years = sorted(set(eq.index.year))
    out: dict[int, float] = {}
    prev_last: float | None = None
    for year in years:
        chunk = eq[eq.index.year == year]
        start = prev_last if prev_last is not None else float(chunk.iloc[0])
        out[year] = float(chunk.iloc[-1]) / start - 1 if start else 0.0
        prev_last = float(chunk.iloc[-1])
    return out


def run_one(df: pd.DataFrame, variant: dict[str, Any], market: str, interval: CandleInterval,
            overlay: dict[str, Any]) -> dict[str, Any]:
    strategy = create_strategy(variant["strategy"], variant["params"])
    risk = RiskConfig.unrestricted(**overlay)
    config = BacktestConfig(initial_capital=1_000_000, fee_rate=FEE, slippage_rate=SLIPPAGE,
                            position_fraction=1.0, risk=risk, check_lookahead=False)
    result = BacktestEngine(config).run(df, strategy, market=market, interval=interval)
    strat_years = yearly_returns(result.equity)
    bh_years = yearly_returns(result.benchmark_equity)
    segments = {y: (strat_years[y], bh_years.get(y, 0.0)) for y in strat_years}
    beats = sum(1 for s, b in segments.values() if s > b)
    m, b = result.metrics, result.benchmark_metrics
    return {
        "market": market, "interval": interval.value, "strategy": variant["strategy"], "label": variant["label"],
        "params": variant["params"], "overlay": overlay,
        "total_return": m.total_return, "bh_return": b.total_return, "excess": m.total_return - b.total_return,
        "mdd": m.mdd, "bh_mdd": b.mdd, "trades": m.total_trades, "win_rate": m.win_rate,
        "profit_factor": m.profit_factor, "fees": m.total_fees, "sharpe": m.sharpe,
        "segments": segments, "seg_beats": beats, "seg_total": len(segments),
        "worst_excess": min((s - bh for s, bh in segments.values()), default=0.0),
    }


def summarize(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """조합(캔들 단위 + 라벨 + 오버레이)별로 마켓을 합쳐 집계한다."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for r in rows:
        key = (r["interval"], r["label"], _overlay_name(r["overlay"]))
        groups.setdefault(key, []).append(r)
    out = []
    for (interval, label, overlay), items in groups.items():
        seg_beats = sum(r["seg_beats"] for r in items)
        seg_total = sum(r["seg_total"] for r in items)
        out.append({
            "interval": interval, "label": label, "overlay": overlay, "markets": len(items),
            "beat_ratio": seg_beats / seg_total if seg_total else 0.0, "seg_beats": seg_beats, "seg_total": seg_total,
            "avg_return": sum(r["total_return"] for r in items) / len(items),
            "avg_bh": sum(r["bh_return"] for r in items) / len(items),
            "avg_excess": sum(r["excess"] for r in items) / len(items),
            "min_excess": min(r["excess"] for r in items),
            "worst_year_excess": min(r["worst_excess"] for r in items),
            "avg_mdd": sum(r["mdd"] for r in items) / len(items),
            "trades": sum(r["trades"] for r in items),
            "avg_pf": sum((r["profit_factor"] or 0.0) for r in items) / len(items),
            "markets_beaten": sum(1 for r in items if r["excess"] > 0),
            "params": items[0]["params"], "strategy": items[0]["strategy"], "risk": items[0]["overlay"],
        })
    df = pd.DataFrame(out)
    # 점수: 연도별로 이긴 비율 → 마켓 전체에서 이긴 수 → 평균 초과수익 (최악 연도가 -30% 이하면 뒤로)
    df["score"] = (
        df["beat_ratio"] * 100 + df["markets_beaten"] * 10 + df["avg_excess"] * 10
        + df["worst_year_excess"].clip(upper=0) * 20
    )
    return df.sort_values("score", ascending=False).reset_index(drop=True)


def _overlay_name(overlay: dict[str, Any]) -> str:
    for name, spec in OVERLAYS.items():
        if spec == overlay:
            return name
    return str(overlay)


def fmt_pct(v: float) -> str:
    return f"{v * 100:+.1f}%"


def print_table(df: pd.DataFrame, top: int) -> None:
    cols = ["interval", "label", "overlay", "beat_ratio", "seg_beats", "seg_total", "markets_beaten", "avg_return",
            "avg_bh", "avg_excess", "min_excess", "worst_year_excess", "avg_mdd", "trades", "avg_pf"]
    head = (f"{'캔들':<5} {'조합':<22} {'오버레이':<14} {'연도승':>7} {'마켓승':>5} {'평균수익':>9} {'평균B&H':>9} "
            f"{'초과':>8} {'최소초과':>8} {'최악연도':>8} {'MDD':>7} {'거래':>5} {'PF':>5}")
    print(head)
    for _, r in df[cols].head(top).iterrows():
        seg = f"{int(r['seg_beats']):>3}/{int(r['seg_total']):<3}"
        print(f"{r['interval']:<5} {r['label']:<22} {r['overlay']:<14} {seg} {int(r['markets_beaten']):>5} "
              f"{fmt_pct(r['avg_return']):>9} {fmt_pct(r['avg_bh']):>9} {fmt_pct(r['avg_excess']):>8} "
              f"{fmt_pct(r['min_excess']):>8} {fmt_pct(r['worst_year_excess']):>8} {fmt_pct(r['avg_mdd']):>7} "
              f"{int(r['trades']):>5} {r['avg_pf']:>5.2f}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="전략 파라미터 스윕 (연도별 B&H 비교)")
    parser.add_argument("--markets", default="KRW-BTC,KRW-ETH,KRW-XRP")
    parser.add_argument("--intervals", default="240m,1d")
    parser.add_argument("--years", type=float, default=5.0)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--overlays", action="store_true", help="상위 조합에 손절·추적 손절 오버레이 검사")
    parser.add_argument("--overlay-top", type=int, default=6)
    parser.add_argument("--only", default="", help="라벨 부분 문자열로 조합 제한 (예: 'SMA20/100')")
    parser.add_argument("--out-dir", default="data/sweeps")
    args = parser.parse_args()

    settings = get_settings()
    markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
    intervals = [CandleInterval.parse(i.strip()) for i in args.intervals.split(",") if i.strip()]
    end = datetime.now(UTC)
    start = end - timedelta(days=365.25 * args.years)
    span = f"{start.astimezone(KST):%Y-%m-%d} ~ {end.astimezone(KST):%Y-%m-%d}"
    print(f"=== 스윕: {', '.join(markets)} × {', '.join(i.value for i in intervals)} · {span} · "
          f"수수료 {FEE:.2%} 슬리피지 {SLIPPAGE:.2%} ===")

    frames: dict[tuple[str, str], pd.DataFrame] = {}
    for market in markets:
        for interval in intervals:
            try:
                df = await load_candles(settings, market, interval, start, end)
            except TraderError as exc:
                print(f"  [건너뜀] {market} {interval.value}: {exc}")
                continue
            frames[(market, interval.value)] = df
            print(f"  캔들 {market} {interval.value}: {len(df)}개 ({df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d})")

    todo = [v for v in variants() if not args.only or args.only in v["label"]]
    total = len(todo) * len(frames)
    print(f"  1단계: 조합 {len(todo)}개 × 데이터 {len(frames)}개 = {total}회")
    rows: list[dict[str, Any]] = []
    t0 = time.monotonic()
    done = 0
    for variant in todo:
        for (market, interval_value), df in frames.items():
            try:
                rows.append(run_one(df, variant, market, CandleInterval.parse(interval_value), {}))
            except Exception as exc:  # noqa: BLE001 - 한 조합의 실패는 건너뛴다
                print(f"  [실패] {variant['label']} {market} {interval_value}: {exc}")
            done += 1
            if done % 25 == 0 or done == total:
                print(f"  진행 {done}/{total} ({time.monotonic() - t0:.0f}초)", flush=True)

    summary = summarize(rows)
    print("\n=== 1단계 결과 (연도별로 단순 보유를 이긴 비율 순) ===")
    print_table(summary, args.top)

    if args.overlays:
        top_keys = summary.head(args.overlay_top)[["interval", "label"]].values.tolist()
        lookup = {v["label"]: v for v in todo}
        print(f"\n  2단계: 상위 {len(top_keys)}개 조합 × 오버레이 {len(OVERLAYS) - 1}개")
        for interval_value, label in top_keys:
            variant = lookup[label]
            for name, overlay in OVERLAYS.items():
                if not overlay:
                    continue
                for (market, iv), df in frames.items():
                    if iv != interval_value:
                        continue
                    try:
                        rows.append(run_one(df, variant, market, CandleInterval.parse(iv), overlay))
                    except Exception as exc:  # noqa: BLE001
                        print(f"  [실패] {label} {name} {market}: {exc}")
        summary = summarize(rows)
        print("\n=== 2단계 결과 (오버레이 포함) ===")
        print_table(summary, args.top)

    save_outputs(Path(args.out_dir), rows, summary)
    return 0


def save_outputs(out_dir: Path, rows: list[dict[str, Any]], summary: pd.DataFrame) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    runs_df = pd.DataFrame([
        {**{k: v for k, v in r.items() if k != "segments"},
         "segments": {y: (round(s, 4), round(b, 4)) for y, (s, b) in r["segments"].items()}}
        for r in rows
    ])
    runs_df.to_csv(out_dir / f"{stamp}_runs.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(out_dir / f"{stamp}_summary.csv", index=False, encoding="utf-8-sig")
    print(f"\n저장: {out_dir / (stamp + '_runs.csv')}, {out_dir / (stamp + '_summary.csv')}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
