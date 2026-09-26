"""연도별 최적 설정 탐색 — 한 해의 과거 캔들로 (캔들 단위 × 전략 파라미터 × 청산 규칙) 조합을 모두 백테스트해
마켓 평균 수익률이 가장 높은 조합을 고른다 (대시보드 설정 탭의 '연도별 최적 설정' 버튼).

- 파라미터 후보는 전략마다 성긴 격자(기본값 포함)로 둔다. 촘촘할수록 그 해에만 맞는 값(과최적화)을 고르기 쉽다.
- 청산 규칙(손절·익절·추적 손절)만 후보별로 바꾸고, 나머지 리스크 값(현금 사용 비율·일일 손실 한도·연속 손실 등)은
  사용자의 현재 설정 그대로 쓴다 — 백테스트 탭에서 '리스크 규칙' 을 켜고 같은 해를 돌리면 같은 숫자가 나온다.
- 목표는 마켓 평균 총 수익률 최대. 단순 보유보다 나은 조합이 없어도 그중 가장 높은 조합을 고른다.
- 결과는 지난 한 해의 사후 최적값이다. 다음 해에도 좋다는 보장이 없으므로 실행기가 다음 해 성과를 함께 계산한다.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pandas as pd

from app.backtest.engine import BacktestConfig, BacktestEngine
from app.risk.config import RiskConfig
from app.strategy import STRATEGIES, create_strategy

GRID_VERSION = 1  # 격자·청산 규칙을 바꾸면 올린다 (저장된 결과 캐시를 무효화)
INTERVALS = ("60m", "240m", "1d")


class OptimizeCancelled(Exception):  # noqa: N818 - 오류가 아니라 사용자 중단 신호
    """사용자가 탐색을 중단했다."""


@dataclass(frozen=True)
class ExitRule:
    """청산 규칙 후보 — 현재 리스크 설정의 손절·익절·추적 손절만 이 값으로 바꾼다 (None = 끔)."""

    label: str
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None

    def apply(self, risk: RiskConfig) -> RiskConfig:
        return RiskConfig(**{
            **risk.model_dump(), "stop_loss_pct": self.stop_loss_pct, "take_profit_pct": self.take_profit_pct,
            "trailing_stop_pct": self.trailing_stop_pct,
        })

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "stop_loss_pct": self.stop_loss_pct, "take_profit_pct": self.take_profit_pct,
                "trailing_stop_pct": self.trailing_stop_pct}


EXIT_RULES: tuple[ExitRule, ...] = (
    ExitRule("손절·익절·추적 손절 끔"),
    ExitRule("손절 3%", stop_loss_pct=0.03),
    ExitRule("손절 7%", stop_loss_pct=0.07),
    ExitRule("추적 손절 5%", trailing_stop_pct=0.05),
    ExitRule("추적 손절 10%", trailing_stop_pct=0.10),
    ExitRule("추적 손절 20%", trailing_stop_pct=0.20),
    ExitRule("손절 5% + 익절 10%", stop_loss_pct=0.05, take_profit_pct=0.10),
    ExitRule("손절 7% + 추적 손절 15%", stop_loss_pct=0.07, trailing_stop_pct=0.15),
)


def _product(**axes: Sequence[Any]) -> list[dict[str, Any]]:
    """축별 후보의 모든 조합. 값이 dict 인 축은 펼쳐 합친다 (여러 필드를 한 묶음으로 바꿀 때)."""
    out = []
    for combo in itertools.product(*axes.values()):
        params: dict[str, Any] = {}
        for key, value in zip(axes, combo, strict=True):
            params.update(value if isinstance(value, dict) else {key: value})
        out.append(params)
    return out


# 전략별 파라미터 후보 (기본값 포함, 성긴 격자). 여기 없는 전략은 기본 파라미터 하나로 캔들·청산 규칙만 고른다.
PARAM_GRIDS: dict[str, list[dict[str, Any]]] = {
    "ma_cross": _product(
        windows=[{"short_window": s, "long_window": long} for s, long in
                 ((5, 20), (10, 50), (20, 60), (20, 120), (50, 200), (10, 200))],
        filters=[{"volume_window": 0, "rsi_window": 0},
                 {"volume_window": 0, "rsi_window": 14, "rsi_max_for_buy": 70.0},
                 {"volume_window": 20, "volume_factor": 1.0, "rsi_window": 14, "rsi_max_for_buy": 70.0}],
    ),
    "rsi": _product(
        window=[7, 14, 21],
        levels=[{"oversold": lo, "overbought": hi} for lo, hi in ((30.0, 70.0), (25.0, 75.0), (35.0, 65.0))],
    ),
    "bollinger": _product(window=[20, 30], std_dev=[2.0, 2.5], rsi_filter=[True, False]),
    "macd": _product(
        periods=[{"fast_period": f, "slow_period": s, "signal_period": g} for f, s, g in
                 ((12, 26, 9), (8, 21, 5), (5, 35, 5), (19, 39, 9))],
        zero_line_filter=[True, False],
    ),
    "ema_cross": _product(
        periods=[{"short_period": s, "medium_period": m, "long_period": long} for s, m, long in
                 ((9, 21, 50), (5, 13, 34), (12, 26, 100), (20, 50, 200))],
        rsi_filter=[True, False],
    ),
    "volume_breakout": _product(breakout_window=[20, 55], volume_multiplier=[1.5, 2.0], exit_window=[10, 20]),
    "adx_trend": _product(adx_window=[14, 20], adx_threshold=[20.0, 25.0, 30.0]),
    "stochastic": _product(
        periods=[{"k_period": k, "d_period": d, "smooth_k": sm} for k, d, sm in ((14, 3, 1), (14, 3, 3), (21, 5, 3))],
        levels=[{"oversold": 20.0, "overbought": 80.0}, {"oversold": 30.0, "overbought": 70.0}],
    ),
    "ichimoku": [
        {"conversion_period": c, "base_period": b, "span_b_period": s, "displacement": d}
        for c, b, s, d in ((9, 26, 52, 26), (7, 22, 44, 22), (10, 30, 60, 30), (20, 60, 120, 30))
    ],
    "obv": _product(obv_window=[10, 20, 50], price_window=[0, 20, 50]),
    "cci": _product(window=[14, 20, 30],
                    levels=[{"oversold": -lv, "overbought": lv} for lv in (100.0, 150.0, 200.0)]),
    "williams_r": _product(window=[14, 21, 30],
                           levels=[{"oversold": -80.0, "overbought": -20.0}, {"oversold": -90.0, "overbought": -10.0}]),
}


def param_grid(strategy_name: str) -> list[dict[str, Any]]:
    """검증을 거친 전체 파라미터 후보 (기본값이 맨 앞, 중복 제거)."""
    candidates = [{}, *PARAM_GRIDS.get(strategy_name, [])]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for params in candidates:
        full = create_strategy(strategy_name, params).params.model_dump(mode="json")
        key = json.dumps(full, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(full)
    return out


def _clean(value: float) -> float | None:
    return None if value is None or math.isnan(value) or math.isinf(value) else float(value)


@dataclass(frozen=True)
class MarketResult:
    market: str
    total_return: float
    benchmark_return: float
    trades: int
    mdd: float
    win_rate: float | None
    fees: float

    def to_dict(self) -> dict[str, Any]:
        return {"market": self.market, "total_return": self.total_return, "benchmark_return": self.benchmark_return,
                "trades": self.trades, "mdd": self.mdd, "win_rate": self.win_rate, "fees": self.fees}


@dataclass(frozen=True)
class CandidateResult:
    interval: str
    params: dict[str, Any]
    exit: ExitRule
    markets: tuple[MarketResult, ...]

    @property
    def avg_return(self) -> float:
        return sum(m.total_return for m in self.markets) / len(self.markets)

    @property
    def avg_benchmark(self) -> float:
        return sum(m.benchmark_return for m in self.markets) / len(self.markets)

    @property
    def avg_mdd(self) -> float:
        return sum(m.mdd for m in self.markets) / len(self.markets)

    @property
    def trades(self) -> int:
        return sum(m.trades for m in self.markets)

    def sort_key(self) -> tuple[float, float, int]:
        """수익률 높은 순 → 같으면 낙폭이 작은 순 → 거래가 적은 순."""
        return (self.avg_return, self.avg_mdd, -self.trades)

    def to_dict(self) -> dict[str, Any]:
        return {
            "interval": self.interval, "params": dict(self.params), "exit": self.exit.to_dict(),
            "avg_return": self.avg_return, "avg_benchmark": self.avg_benchmark,
            "beats_benchmark": self.avg_return > self.avg_benchmark, "avg_mdd": self.avg_mdd, "trades": self.trades,
            "markets": [m.to_dict() for m in self.markets],
        }


def backtest_candidate(
    frames: Mapping[str, pd.DataFrame],
    strategy_name: str,
    params: Mapping[str, Any],
    risk: RiskConfig,
    *,
    interval: str,
    initial_capital: float,
    fee_rate: float,
    slippage_rate: float,
) -> tuple[MarketResult, ...]:
    """한 조합을 마켓마다 백테스트한다 (전략 Look-ahead 는 테스트가 보장하므로 조합마다 다시 검사하지 않는다)."""
    config = BacktestConfig(
        initial_capital=initial_capital, fee_rate=fee_rate, slippage_rate=slippage_rate,
        position_fraction=risk.position_fraction, risk=risk, check_lookahead=False,
    )
    engine = BacktestEngine(config)
    out = []
    for market, df in frames.items():
        result = engine.run(df, create_strategy(strategy_name, params), market=market, interval=interval)
        m = result.metrics
        out.append(MarketResult(
            market=market, total_return=m.total_return, benchmark_return=result.benchmark_metrics.total_return,
            trades=m.total_trades, mdd=m.mdd, win_rate=_clean(m.win_rate), fees=m.total_fees,
        ))
    return tuple(out)


@dataclass(frozen=True)
class OptimizationOutcome:
    best: CandidateResult
    top: tuple[CandidateResult, ...]
    evaluated: int


def optimize_year(
    frames: Mapping[str, Mapping[str, pd.DataFrame]],
    strategy_name: str,
    base_risk: RiskConfig,
    *,
    initial_capital: float,
    fee_rate: float,
    slippage_rate: float,
    grid: Sequence[Mapping[str, Any]] | None = None,
    exits: Sequence[ExitRule] = EXIT_RULES,
    top: int = 5,
    on_progress: Callable[[int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> OptimizationOutcome:
    """``frames``(캔들 단위 → 마켓 → 그 해 캔들)의 모든 조합을 백테스트해 마켓 평균 수익률이 가장 높은 조합을 고른다.

    단순 보유보다 나은 조합이 없어도 가장 높은 조합을 돌려준다 (``best.to_dict()['beats_benchmark']`` 로 구분).
    ``on_progress`` 는 끝낸 백테스트 수(마켓 단위)를, ``should_stop`` 이 참이면 ``OptimizeCancelled`` 를 낸다.
    """
    if strategy_name not in STRATEGIES:
        raise ValueError(f"알 수 없는 전략 '{strategy_name}'")
    usable = {interval: dict(by_market) for interval, by_market in frames.items() if by_market}
    if not usable:
        raise ValueError("백테스트할 캔들이 없습니다")
    candidates = list(grid) if grid is not None else param_grid(strategy_name)
    results: list[CandidateResult] = []
    done = 0
    for interval, by_market in usable.items():
        for params in candidates:
            for rule in exits:
                if should_stop is not None and should_stop():
                    raise OptimizeCancelled
                markets = backtest_candidate(
                    by_market, strategy_name, params, rule.apply(base_risk), interval=interval,
                    initial_capital=initial_capital, fee_rate=fee_rate, slippage_rate=slippage_rate,
                )
                results.append(CandidateResult(interval, dict(params), rule, markets))
                done += len(markets)
                if on_progress is not None:
                    on_progress(done)
    ranked = sorted(results, key=CandidateResult.sort_key, reverse=True)
    return OptimizationOutcome(best=ranked[0], top=tuple(ranked[:top]), evaluated=len(results))


def count_runs(strategy_name: str, intervals: int, markets: int, exits: int = len(EXIT_RULES)) -> int:
    """진행률 분모: 백테스트 횟수 (조합 × 마켓)."""
    return len(param_grid(strategy_name)) * exits * intervals * markets


# ---------------------------------------------------------------------------
# 로그 억제 — 조합 수백 개를 돌리면 백테스트마다 남는 '백테스트 완료'·'리스크 일일 리셋'·'리스크 잠금' 로그가
# 수십만 줄이 된다. 탐색을 도는 스레드의 기록만 버리고(ERROR 이상은 남김) 다른 스레드의 로그는 그대로 둔다.
# ---------------------------------------------------------------------------
class _ThreadQuietFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.threads: set[int] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread not in self.threads or record.levelno >= logging.ERROR


_QUIET_FILTER = _ThreadQuietFilter()
for _name in ("app.backtest.engine", "app.risk.manager"):
    logging.getLogger(_name).addFilter(_QUIET_FILTER)


@contextmanager
def quiet_backtest_logs() -> Iterator[None]:
    """이 스레드에서 도는 백테스트의 INFO·WARNING 로그를 버린다."""
    ident = threading.get_ident()
    _QUIET_FILTER.threads.add(ident)
    try:
        yield
    finally:
        _QUIET_FILTER.threads.discard(ident)
