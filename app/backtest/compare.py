"""전략별 성과 비교 — 같은 캔들·같은 설정(초기 자본·수수료·슬리피지·리스크)으로 여러 전략을 돌려 한 표로 본다.

- ``run_strategies()``: 전략 목록을 같은 데이터로 차례로 백테스트한다. 각 결과는 ``BacktestEngine.run`` 그대로라
  수수료·슬리피지·리스크 규칙이 전략마다 똑같이 적용된다 (비교에서 수수료가 빠지지 않는다).
- ``summarize_by_strategy()``: 결과 행(``strategy`` / ``metrics`` / ``benchmark`` dict)을 전략별로 합친 비교표.
  여러 마켓·구간 결과도 합친다. 대시보드 작업 결과와 CLI 결과가 같은 함수를 쓴다.
- ``format_comparison()`` / ``save_comparison()``: 콘솔 표, CSV·전략별 결과 폴더 저장.

한 구간의 순위는 그 구간의 설명일 뿐이다. 구간·마켓을 바꿔도 순위가 유지되는지 봐야 과최적화를 피한다.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from app.backtest.engine import BacktestConfig, BacktestEngine, BacktestResult
from app.backtest.report import save_result
from app.core.exceptions import StrategyError
from app.exchange.models import KST, CandleInterval
from app.strategy import STRATEGIES, Strategy


def parse_strategy_names(text: str) -> list[str]:
    """``all`` 또는 쉼표 목록 → 등록된 전략 이름 목록 (순서 유지, 중복 제거). 모르는 이름은 ``StrategyError``."""
    raw = (text or "").strip().lower()
    if raw in ("", "all", "*"):
        return list(STRATEGIES)
    names = list(dict.fromkeys(n.strip() for n in raw.split(",") if n.strip()))
    unknown = [n for n in names if n not in STRATEGIES]
    if unknown:
        raise StrategyError(f"알 수 없는 전략 {unknown}. 사용 가능: {', '.join(STRATEGIES)}")
    return names


def run_strategies(
    df: pd.DataFrame,
    strategies: Sequence[Strategy],
    config: BacktestConfig | None = None,
    *,
    market: str | None = None,
    interval: CandleInterval | str | None = None,
) -> list[BacktestResult]:
    """같은 캔들·같은 설정으로 전략들을 차례로 백테스트한다 (전략마다 새 포트폴리오·리스크 상태)."""
    engine = BacktestEngine(config)
    return [engine.run(df, strategy, market=market, interval=interval) for strategy in strategies]


def result_row(result: BacktestResult) -> dict[str, Any]:
    """``summarize_by_strategy`` 입력 형태 (대시보드 ``serialize_result`` 와 같은 키)."""
    return {
        "strategy": result.strategy_name, "strategy_params": result.strategy_params, "market": result.market,
        "interval": result.interval, "metrics": result.metrics.to_dict(),
        "benchmark": result.benchmark_metrics.to_dict(),
    }


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _mean(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _realized(metrics: Mapping[str, Any]) -> float:
    """실현 손익. 이 지표가 생기기 전에 저장된 결과는 최종 자산 − 초기 자본(마지막 캔들에 전부 청산하므로 같다)."""
    value = _num(metrics.get("realized_pnl"))
    if value is not None:
        return value
    return (_num(metrics.get("final_equity")) or 0.0) - (_num(metrics.get("initial_capital")) or 0.0)


def summarize_by_strategy(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """결과 행을 전략별로 합친 비교표 — 평균 총 수익률이 높은 순. 오류 행은 건너뛴다. 값이 없으면 None (JSON 안전).

    - ``runs``: 합친 결과 수 (마켓 × 구간), ``beat_benchmark``: 단순 보유보다 수익률이 높았던 결과 수
    - ``avg_return`` / ``avg_benchmark``: 총 수익률 평균, ``avg_mdd`` / ``worst_mdd``: MDD 평균·최악
    - ``trades`` / ``win_rate``: 전체 거래 수와 전체 거래 기준 승률
    - ``avg_trade_return``: 거래 수로 가중한 거래당 평균 수익률
    - ``max_consecutive_losses``: 결과 중 최댓값, ``total_fees`` / ``realized_pnl``: 합계 (수수료 차감 후)
    """
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("error") or not row.get("metrics"):
            continue
        groups.setdefault(str(row.get("strategy")), []).append(row)
    out: list[dict[str, Any]] = []
    for name, items in groups.items():
        metrics = [r["metrics"] for r in items]
        benches = [r.get("benchmark") or {} for r in items]
        trades = sum(int(m.get("total_trades") or 0) for m in metrics)
        wins = sum(int(m.get("wins") or 0) for m in metrics)
        weighted = [(_num(m.get("avg_trade_return")), int(m.get("total_trades") or 0)) for m in metrics]
        weighted = [(v, n) for v, n in weighted if v is not None and n > 0]
        weight = sum(n for _, n in weighted)
        returns = [_num(m.get("total_return")) for m in metrics]
        bench_returns = [_num(b.get("total_return")) for b in benches]
        mdds = [_num(m.get("mdd")) for m in metrics]
        cls = STRATEGIES.get(name)
        family = cls.family if cls is not None else None
        out.append({
            "strategy": name,
            "family": family.value if family else None,
            "family_label": family.label if family else None,
            "runs": len(items),
            "avg_return": _mean(returns),
            "avg_benchmark": _mean(bench_returns),
            "beat_benchmark": sum(
                1 for s, b in zip(returns, bench_returns, strict=True) if s is not None and b is not None and s > b
            ),
            "trades": trades,
            "wins": wins,
            "win_rate": wins / trades if trades else None,
            "avg_trade_return": sum(v * n for v, n in weighted) / weight if weight else None,
            "avg_mdd": _mean(mdds),
            "worst_mdd": min((v for v in mdds if v is not None), default=None),
            "max_consecutive_losses": max((int(m.get("max_consecutive_losses") or 0) for m in metrics), default=0),
            "total_fees": sum(_num(m.get("total_fees")) or 0.0 for m in metrics),
            "realized_pnl": sum(_realized(m) for m in metrics),
        })
    out.sort(key=lambda d: d["avg_return"] if d["avg_return"] is not None else -math.inf, reverse=True)
    return out


def _pct(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value * 100:+.{digits}f}%"


def format_comparison(results: Sequence[BacktestResult]) -> str:
    """콘솔용 비교표. 모든 결과는 같은 캔들·같은 설정이어야 한다 (``run_strategies`` 결과)."""
    if not results:
        return "비교할 결과가 없습니다"
    first = results[0]
    cfg, m0 = first.config, first.metrics
    shown = ("현금 사용 비율", "손절", "익절", "추적 손절")
    risk = ", ".join(f"{k} {v}" for k, v in first.risk_config.describe().items() if k in shown)
    lines = [
        f"=== 전략 비교: {first.market} {first.interval} · {first.start.astimezone(KST):%Y-%m-%d %H:%M} ~ "
        f"{first.end.astimezone(KST):%Y-%m-%d %H:%M} KST ({m0.duration_days:.1f}일, 캔들 {m0.periods}개) ===",
        f"  모든 전략 동일 조건: 초기자본 {cfg.initial_capital:,.0f} KRW, 수수료 {cfg.fee_rate * 100:.3f}%(편도), "
        f"슬리피지 {cfg.slippage_rate * 100:.3f}%, {risk}",
        "",
        f"  {'전략':<16} {'거래':>5} {'승률':>7} {'총수익률':>9} {'거래당':>8} {'MDD':>8} {'연속손실':>5} "
        f"{'수수료':>10} {'실현손익':>12}  계열",
    ]
    rows = sorted(results, key=lambda r: r.metrics.total_return, reverse=True)
    for r in rows:
        m = r.metrics
        win = "n/a" if math.isnan(m.win_rate) else f"{m.win_rate * 100:.1f}%"
        avg_trade = None if math.isnan(m.avg_trade_return) else m.avg_trade_return
        family = STRATEGIES[r.strategy_name].family if r.strategy_name in STRATEGIES else None
        lines.append(
            f"  {r.strategy_name:<16} {m.total_trades:>5} {win:>7} {_pct(m.total_return):>9} {_pct(avg_trade):>8} "
            f"{_pct(m.mdd):>8} {m.max_consecutive_losses:>5} {m.total_fees:>10,.0f} {m.realized_pnl:>12,.0f}  "
            f"{family.label if family else '-'}"
        )
    b = first.benchmark_metrics
    lines.append(
        f"  {'단순 보유(B&H)':<14} {b.total_trades:>5} {'-':>7} {_pct(b.total_return):>9} {'-':>8} {_pct(b.mdd):>8} "
        f"{'-':>5} {b.total_fees:>10,.0f} {b.realized_pnl:>12,.0f}"
    )
    lines.append("")
    lines.append("  ※ 수익률·실현손익은 수수료·슬리피지를 뺀 값이다. 한 구간의 순위일 뿐 미래 수익을 보장하지 않는다.")
    return "\n".join(lines)


def save_comparison(results: Sequence[BacktestResult], out_dir: Path | str) -> Path:
    """``comparison.csv`` (전략별 한 줄) + 전략별 결과 폴더(``save_result`` 형식)를 저장한다."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = summarize_by_strategy(result_row(r) for r in results)
    pd.DataFrame(summary).to_csv(out / "comparison.csv", index=False, encoding="utf-8-sig")
    for r in results:
        save_result(r, out / r.strategy_name)
    return out
