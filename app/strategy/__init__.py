"""전략 패키지 — 이름으로 전략을 찾아 만든다.

새 전략 추가 방법:
1. ``Strategy`` 를 상속해 ``name``, ``Params``, ``warmup_periods``, ``evaluate()`` 구현 (예: ``ma_cross.py``)
2. 아래 ``STRATEGIES`` 에 등록
3. ``tests/test_strategy.py`` 의 공통 검사(Look-ahead, 워밍업 HOLD)에 포함
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.exceptions import StrategyError
from app.strategy.base import Action, Signal, Strategy, StrategyParams, check_no_lookahead
from app.strategy.ma_cross import MovingAverageCrossStrategy
from app.strategy.rsi import RSIStrategy

STRATEGIES: dict[str, type[Strategy]] = {
    MovingAverageCrossStrategy.name: MovingAverageCrossStrategy,
    RSIStrategy.name: RSIStrategy,
}


def available_strategies() -> dict[str, str]:
    return {name: cls.description for name, cls in STRATEGIES.items()}


def create_strategy(name: str, params: Mapping[str, Any] | None = None) -> Strategy:
    key = (name or "").strip().lower()
    cls = STRATEGIES.get(key)
    if cls is None:
        raise StrategyError(f"알 수 없는 전략 '{name}'. 사용 가능: {', '.join(STRATEGIES)}")
    return cls(params)


__all__ = [
    "STRATEGIES",
    "Action",
    "MovingAverageCrossStrategy",
    "RSIStrategy",
    "Signal",
    "Strategy",
    "StrategyParams",
    "available_strategies",
    "check_no_lookahead",
    "create_strategy",
]
