"""전략 패키지 — 이름으로 전략을 찾아 만든다.

새 전략 추가 방법:
1. ``Strategy`` 를 상속해 ``name``, ``description``, ``family``, ``Params``, ``warmup_periods``, ``evaluate()`` 구현
   (예: ``ma_cross.py``, ``macd.py``). 파라미터는 ``param()`` 으로 선언하면 대시보드 폼에 이름·설명이 보인다.
2. 아래 ``STRATEGIES`` 에 등록
3. ``tests/test_strategy.py`` 의 공통 검사(Look-ahead, 워밍업 HOLD)는 등록된 전략 전부에 자동 적용된다
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.exceptions import StrategyError
from app.strategy.adx_trend import ADXTrendStrategy
from app.strategy.base import Action, Signal, Strategy, StrategyFamily, StrategyParams, check_no_lookahead
from app.strategy.bollinger import BollingerStrategy
from app.strategy.cci import CCIStrategy
from app.strategy.ema_cross import EMACrossStrategy
from app.strategy.ichimoku import IchimokuStrategy
from app.strategy.ma_cross import MovingAverageCrossStrategy
from app.strategy.macd import MACDStrategy
from app.strategy.obv import OBVStrategy
from app.strategy.rsi import RSIStrategy
from app.strategy.stochastic import StochasticStrategy
from app.strategy.volume_breakout import VolumeBreakoutStrategy
from app.strategy.williams_r import WilliamsRStrategy

# 등록 순서 = 대시보드 드롭다운 순서 (계열별로 묶어 보여 준다)
STRATEGIES: dict[str, type[Strategy]] = {
    MovingAverageCrossStrategy.name: MovingAverageCrossStrategy,
    RSIStrategy.name: RSIStrategy,
    BollingerStrategy.name: BollingerStrategy,
    MACDStrategy.name: MACDStrategy,
    EMACrossStrategy.name: EMACrossStrategy,
    VolumeBreakoutStrategy.name: VolumeBreakoutStrategy,
    ADXTrendStrategy.name: ADXTrendStrategy,
    StochasticStrategy.name: StochasticStrategy,
    IchimokuStrategy.name: IchimokuStrategy,
    OBVStrategy.name: OBVStrategy,
    CCIStrategy.name: CCIStrategy,
    WilliamsRStrategy.name: WilliamsRStrategy,
}


def available_strategies() -> dict[str, str]:
    return {name: cls.description for name, cls in STRATEGIES.items()}


def create_strategy(name: str, params: Mapping[str, Any] | None = None) -> Strategy:
    key = (name or "").strip().lower()
    cls = STRATEGIES.get(key)
    if cls is None:
        raise StrategyError(f"알 수 없는 전략 '{name}'. 사용 가능: {', '.join(STRATEGIES)}")
    return cls(params)


def strategies_by_family() -> dict[StrategyFamily, list[str]]:
    """계열별 전략 이름 (계열 선언 순서, 계열 안에서는 등록 순서). 향후 Strategy Router 가 후보를 고를 때 쓴다."""
    out: dict[StrategyFamily, list[str]] = {family: [] for family in StrategyFamily}
    for name, cls in STRATEGIES.items():
        if cls.family is not None:
            out[cls.family].append(name)
    return out


def strategy_catalog() -> list[dict[str, Any]]:
    """대시보드·CLI 표시용 전략 목록: 이름, 설명, 계열, 매수·매도 규칙, 기본 파라미터의 워밍업 캔들 수."""
    return [
        {
            "name": name,
            "description": cls.description,
            "family": cls.family.value if cls.family else None,
            "family_label": cls.family.label if cls.family else None,
            "rules": cls.rules,
            "warmup_periods": cls().warmup_periods,
        }
        for name, cls in STRATEGIES.items()
    ]


__all__ = [
    "STRATEGIES",
    "ADXTrendStrategy",
    "Action",
    "BollingerStrategy",
    "CCIStrategy",
    "EMACrossStrategy",
    "IchimokuStrategy",
    "MACDStrategy",
    "MovingAverageCrossStrategy",
    "OBVStrategy",
    "RSIStrategy",
    "Signal",
    "StochasticStrategy",
    "Strategy",
    "StrategyFamily",
    "StrategyParams",
    "VolumeBreakoutStrategy",
    "WilliamsRStrategy",
    "available_strategies",
    "check_no_lookahead",
    "create_strategy",
    "strategies_by_family",
    "strategy_catalog",
]
