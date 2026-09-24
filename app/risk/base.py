"""리스크 관리 인터페이스와 최소 구현.

전체 규칙(거래당 상한·일일 손실·연속 손실·손절·익절 등)은 ``app.risk.manager.RiskManager`` 에 있다.
``BasicRiskManager`` 는 보유 여부·예산만 보는 최소판으로 테스트·비교용으로 남겨 둔다.

(원문)

신호 → ``RiskManager.evaluate()`` → 승인/거부 + 주문 크기. 승인된 것만 브로커로 간다.
Phase 5 의 ``BasicRiskManager`` 는 다음만 본다:
- BUY: 이미 보유 중이면 거부(피라미딩 없음), 예산(현금 × position_fraction)이 최소 주문 금액 미만이면 거부
- SELL: 보유하지 않으면 거부
- HOLD: 항상 거부
Phase 6 에서 일일 손실 한도, 최대 연속 손실, 최대 포지션 수, 손절·익절, 비정상 가격 감지를 더한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.strategy.base import Action, Signal
from app.trading.market_state import PriceState
from app.trading.portfolio import Portfolio


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    amount: float | None = None  # 매수 예산 (KRW)
    quantity: float | None = None  # 매도 수량 (None = 전량)


class RiskPolicy(Protocol):
    """엔진이 기대하는 최소 인터페이스. 구현: ``BasicRiskManager``(간단), ``app.risk.manager.RiskManager``(전체)."""

    def evaluate(
        self, signal: Signal, portfolio: Portfolio, price: PriceState | None, now: datetime, equity: float | None = None
    ) -> RiskDecision: ...


class BasicRiskManager:
    def __init__(self, *, position_fraction: float = 1.0, min_order_amount: float = 5000.0) -> None:
        if not 0 < position_fraction <= 1:
            raise ValueError("position_fraction 은 0 초과 1 이하여야 합니다")
        self.position_fraction = position_fraction
        self.min_order_amount = min_order_amount

    def evaluate(
        self, signal: Signal, portfolio: Portfolio, price: PriceState | None, now: datetime, equity: float | None = None
    ) -> RiskDecision:
        if signal.action is Action.BUY:
            if portfolio.has_position(signal.market):
                return RiskDecision(False, "이미 보유 중 (추가 매수 없음)")
            budget = portfolio.cash * self.position_fraction
            if budget < self.min_order_amount:
                return RiskDecision(False, f"매수 예산 {budget:,.0f} KRW 가 최소 주문 금액 미만")
            return RiskDecision(True, "기본 검사 통과", amount=budget)
        if signal.action is Action.SELL:
            if not portfolio.has_position(signal.market):
                return RiskDecision(False, "보유 포지션 없음")
            return RiskDecision(True, "기본 검사 통과", quantity=None)
        return RiskDecision(False, "HOLD 신호")
