"""모의 계좌(Portfolio): 백테스트(Phase 4)와 Paper Trading(Phase 5)이 공유하는 현금·포지션·수수료·거래 기록.

- 현물 매수 전용(롱 온리). 업비트 현물에는 공매도가 없다.
- 금액은 KRW float, 수량은 소수점 8자리(업비트 수량 단위)로 내림한다.
- 수수료: 매수는 매수 금액 × 수수료율을 현금에서 추가 차감, 매도는 매도 대금에서 차감 (업비트 KRW 마켓 방식).
  업비트 KRW 마켓 기본 수수료는 0.05% (0.0005), BTC 마켓은 0.25%.
- 최소 주문 금액 5,000 KRW (docs/upbit-api-notes.md). 전량 청산은 잔량이 작아도 허용한다(시뮬레이션 단순화).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.core.exceptions import TraderError

QUANTITY_DECIMALS = 8
DEFAULT_FEE_RATE = 0.0005
DEFAULT_MIN_ORDER_AMOUNT = 5000.0


class PortfolioError(TraderError):
    """체결 불가(현금 부족, 최소 주문 금액 미달, 포지션 없음 등)."""


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


def floor_quantity(quantity: float) -> float:
    scale = 10**QUANTITY_DECIMALS
    return math.floor(quantity * scale + 1e-9) / scale


@dataclass(frozen=True)
class Fill:
    """체결 1건."""

    time: datetime
    market: str
    side: Side
    price: float
    quantity: float
    amount: float  # price × quantity
    fee: float
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "time": self.time.isoformat(), "market": self.market, "side": self.side.value, "price": self.price,
            "quantity": self.quantity, "amount": self.amount, "fee": self.fee, "reason": self.reason,
        }


@dataclass
class Position:
    market: str
    quantity: float
    avg_price: float
    opened_at: datetime
    entry_amount: float  # 누적 매수 금액
    entry_fee: float  # 누적 매수 수수료
    # False: avg_price 가 진짜 매수 단가가 아니라 봇이 처음 본 시세(기준가)다 — 포켓 이전·입금 등으로 들어와
    # 거래소 avg_buy_price 가 0 인 코인 (감사 MEDIUM-2). 손절·익절·손익은 이 기준가 기준으로 계산된다.
    cost_known: bool = True

    @property
    def cost_basis(self) -> float:
        return self.entry_amount + self.entry_fee

    def market_value(self, price: float) -> float:
        return self.quantity * price

    def unrealized_pnl(self, price: float) -> float:
        return self.market_value(price) - self.cost_basis

    def unrealized_pnl_pct(self, price: float) -> float:
        return self.unrealized_pnl(price) / self.cost_basis if self.cost_basis else 0.0


@dataclass(frozen=True)
class Trade:
    """왕복 거래 1건 (매수 → 매도)."""

    market: str
    entry_time: datetime
    entry_price: float
    quantity: float
    entry_amount: float
    entry_fee: float
    exit_time: datetime
    exit_price: float
    exit_amount: float
    exit_fee: float
    exit_reason: str
    pnl: float
    pnl_pct: float

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    @property
    def holding_seconds(self) -> float:
        return (self.exit_time - self.entry_time).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": self.market, "entry_time": self.entry_time.isoformat(), "entry_price": self.entry_price,
            "quantity": self.quantity, "entry_amount": self.entry_amount, "entry_fee": self.entry_fee,
            "exit_time": self.exit_time.isoformat(), "exit_price": self.exit_price, "exit_amount": self.exit_amount,
            "exit_fee": self.exit_fee, "exit_reason": self.exit_reason, "pnl": self.pnl, "pnl_pct": self.pnl_pct,
            "holding_seconds": self.holding_seconds,
        }


@dataclass
class Portfolio:
    initial_cash: float
    fee_rate: float = DEFAULT_FEE_RATE
    min_order_amount: float = DEFAULT_MIN_ORDER_AMOUNT
    cash: float = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    fees_paid: float = 0.0
    # 최소 주문 금액 미만이라 포지션에서 제외한 거래소 잔고 (마켓 → 수량). 거래소가 매도를 거부하는 수량이라
    # 매매 대상이 아니다.
    dust: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash 는 0보다 커야 합니다")
        if not 0 <= self.fee_rate < 0.1:
            raise ValueError(f"fee_rate 가 비정상입니다: {self.fee_rate}")
        self.cash = float(self.initial_cash)

    # ------------------------------------------------------------------
    def position(self, market: str) -> Position | None:
        return self.positions.get(market)

    def has_position(self, market: str) -> bool:
        return market in self.positions

    def equity(self, prices: Mapping[str, float]) -> float:
        """현금 + 보유 자산 평가액. 보유 마켓의 가격이 없으면 오류."""
        value = self.cash
        for market, pos in self.positions.items():
            if market not in prices:
                raise PortfolioError(f"{market} 평가 가격이 없습니다")
            value += pos.market_value(prices[market])
        return value

    # ------------------------------------------------------------------
    def buy(
        self,
        market: str,
        price: float,
        *,
        time: datetime,
        amount: float | None = None,
        quantity: float | None = None,
        reason: str = "",
        fee: float | None = None,
        enforce_limits: bool = True,
    ) -> Fill:
        """시장가 매수 시뮬레이션. ``amount``(KRW, 수수료 포함 예산) 또는 ``quantity`` 중 하나로 크기를 정한다.

        실제 거래소 체결을 반영할 때는 ``fee`` 에 실제 수수료를 주고 ``enforce_limits=False`` 로 호출한다
        (거래소가 이미 검증한 체결이므로 최소 금액·현금 부족 검사를 하지 않는다).
        """
        if price <= 0:
            raise PortfolioError(f"매수 가격이 비정상입니다: {price}")
        if quantity is None:
            budget = self.cash if amount is None else min(amount, self.cash)
            quantity = floor_quantity(budget / (price * (1 + self.fee_rate)))
        else:
            quantity = floor_quantity(quantity)
        cost = quantity * price
        fee = cost * self.fee_rate if fee is None else fee
        if quantity <= 0:
            raise PortfolioError("매수 수량이 0 입니다")
        if enforce_limits and cost < self.min_order_amount:
            raise PortfolioError(f"최소 주문 금액({self.min_order_amount:,.0f} KRW) 미달: {cost:,.0f} KRW")
        if enforce_limits and cost + fee > self.cash + 1e-6:
            raise PortfolioError(f"현금 부족: 필요 {cost + fee:,.0f} KRW, 보유 {self.cash:,.0f} KRW")

        self.cash -= cost + fee
        self.fees_paid += fee
        pos = self.positions.get(market)
        if pos is None:
            self.positions[market] = Position(market, quantity, price, time, cost, fee)
        else:
            total_qty = pos.quantity + quantity
            pos.avg_price = (pos.avg_price * pos.quantity + cost) / total_qty
            pos.quantity = total_qty
            pos.entry_amount += cost
            pos.entry_fee += fee
        fill = Fill(time, market, Side.BUY, price, quantity, cost, fee, reason)
        self.fills.append(fill)
        return fill

    def sell(
        self,
        market: str,
        price: float,
        *,
        time: datetime,
        quantity: float | None = None,
        reason: str = "",
        fee: float | None = None,
        enforce_limits: bool = True,
    ) -> Fill:
        """시장가 매도 시뮬레이션. ``quantity`` 생략 시 전량. 실제 체결 반영은 ``fee``/``enforce_limits`` 참고."""
        pos = self.positions.get(market)
        if pos is None or pos.quantity <= 0:
            raise PortfolioError(f"{market} 포지션이 없습니다")
        if price <= 0:
            raise PortfolioError(f"매도 가격이 비정상입니다: {price}")
        full = quantity is None or quantity >= pos.quantity - 1e-12
        quantity = pos.quantity if full else floor_quantity(quantity)
        if quantity <= 0:
            raise PortfolioError("매도 수량이 0 입니다")
        proceeds = quantity * price
        fee = proceeds * self.fee_rate if fee is None else fee
        if enforce_limits and not full and proceeds < self.min_order_amount:
            raise PortfolioError(f"최소 주문 금액({self.min_order_amount:,.0f} KRW) 미달: {proceeds:,.0f} KRW")

        fraction = quantity / pos.quantity
        entry_amount = pos.entry_amount * fraction
        entry_fee = pos.entry_fee * fraction
        pnl = proceeds - fee - entry_amount - entry_fee
        cost_basis = entry_amount + entry_fee
        trade = Trade(
            market=market, entry_time=pos.opened_at, entry_price=pos.avg_price, quantity=quantity,
            entry_amount=entry_amount, entry_fee=entry_fee, exit_time=time, exit_price=price,
            exit_amount=proceeds, exit_fee=fee, exit_reason=reason, pnl=pnl,
            pnl_pct=pnl / cost_basis if cost_basis else 0.0,
        )
        self.cash += proceeds - fee
        self.fees_paid += fee
        if full:
            del self.positions[market]
        else:
            pos.quantity -= quantity
            pos.entry_amount -= entry_amount
            pos.entry_fee -= entry_fee
        fill = Fill(time, market, Side.SELL, price, quantity, proceeds, fee, reason)
        self.fills.append(fill)
        self.trades.append(trade)
        return fill

    # ------------------------------------------------------------------
    def summary(self, prices: Mapping[str, float] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "initial_cash": self.initial_cash,
            "cash": self.cash,
            "fees_paid": self.fees_paid,
            "open_positions": {
                m: {"quantity": p.quantity, "avg_price": p.avg_price, "cost_basis": p.cost_basis,
                    "cost_known": p.cost_known}
                for m, p in self.positions.items()
            },
            "closed_trades": len(self.trades),
            "realized_pnl": sum(t.pnl for t in self.trades),
        }
        if prices is not None:
            out["equity"] = self.equity(prices)
        return out
