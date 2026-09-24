"""주문 모델과 가상 브로커(PaperBroker).

- ``OrderRequest`` → ``Order`` (상태: NEW → FILLED | REJECTED). 실거래(Phase 7)도 같은 ``Order`` 스키마를 쓴다.
- ``client_id`` 는 같은 신호가 두 번 실행되는 것을 막는 멱등 키다
  (기본: ``{mode}:{market}:{side}:{signal_time}``). 재시작 후 DB 에서 처리 이력을 읽어와 이어서 막는다.
- PaperBroker 체결 가격: 매수는 최우선 매도호가(best ask), 매도는 최우선 매수호가(best bid)를 쓰고,
  호가가 없거나 오래됐으면 마지막 체결가로 대체한다. 그 위에 슬리피지를 더하고 수수료는 Portfolio 가 뺀다.
- 가격이 없거나 ``max_price_age`` 보다 오래됐으면 체결하지 않고 거부한다 (비정상 시세 방어).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.trading.market_state import PriceState
from app.trading.portfolio import Fill, Portfolio, PortfolioError, Side


class OrderStatus(StrEnum):
    NEW = "NEW"
    FILLED = "FILLED"
    REJECTED = "REJECTED"


class OrderType(StrEnum):
    MARKET = "market"


@dataclass(frozen=True)
class OrderRequest:
    market: str
    side: Side
    amount: float | None = None  # 매수 예산 (KRW, 수수료 포함)
    quantity: float | None = None  # 매도 수량 (None = 전량)
    reason: str = ""
    strategy: str = ""
    signal_time: datetime | None = None
    client_id: str | None = None

    def resolved_client_id(self, mode: str) -> str:
        if self.client_id:
            return self.client_id
        stamp = self.signal_time.isoformat() if self.signal_time else uuid.uuid4().hex
        return f"{mode}:{self.market}:{self.side.value}:{stamp}"


@dataclass
class Order:
    id: str
    client_id: str
    mode: str
    market: str
    side: Side
    order_type: OrderType
    amount: float | None
    quantity: float | None
    status: OrderStatus
    created_at: datetime
    reason: str = ""
    strategy: str = ""
    signal_time: datetime | None = None
    filled_at: datetime | None = None
    fill_price: float | None = None
    filled_quantity: float | None = None
    fee: float = 0.0
    error: str | None = None
    exchange_order_id: str | None = None
    fills: list[Fill] = field(default_factory=list)

    @property
    def is_filled(self) -> bool:
        return self.status is OrderStatus.FILLED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "client_id": self.client_id, "mode": self.mode, "market": self.market,
            "side": self.side.value, "order_type": self.order_type.value, "amount": self.amount,
            "quantity": self.quantity, "status": self.status.value, "created_at": self.created_at.isoformat(),
            "reason": self.reason, "strategy": self.strategy,
            "signal_time": self.signal_time.isoformat() if self.signal_time else None,
            "filled_at": self.filled_at.isoformat() if self.filled_at else None, "fill_price": self.fill_price,
            "filled_quantity": self.filled_quantity, "fee": self.fee, "error": self.error,
        }


class PaperBroker:
    """가상 체결. 실제 API 를 절대 호출하지 않는다."""

    mode = "paper"

    def __init__(
        self,
        portfolio: Portfolio,
        *,
        slippage_rate: float = 0.0005,
        max_price_age_seconds: float = 30.0,
        processed_client_ids: set[str] | None = None,
    ) -> None:
        if not 0 <= slippage_rate < 0.1:
            raise ValueError(f"slippage_rate 가 비정상입니다: {slippage_rate}")
        self.portfolio = portfolio
        self.slippage_rate = slippage_rate
        self.max_price_age = max_price_age_seconds
        self.processed: set[str] = set(processed_client_ids or ())

    def _reference_price(self, side: Side, price: PriceState | None, now: datetime) -> tuple[float | None, str]:
        if price is None:
            return None, "가격 정보 없음"
        if price.book_time and (now - price.book_time).total_seconds() <= self.max_price_age:
            ref = price.best_ask if side is Side.BUY else price.best_bid
            if ref:
                return ref, "orderbook"
        if price.last_price and price.last_time and (now - price.last_time).total_seconds() <= self.max_price_age:
            return price.last_price, "last_trade"
        return None, f"시세가 {self.max_price_age:.0f}초 이상 오래됨"

    async def execute(self, request: OrderRequest, price: PriceState | None, now: datetime | None = None) -> Order:
        """브로커 공통 인터페이스 (LiveBroker 와 동일). 가상 체결은 즉시 끝나지만 실제 브로커는 폴링하므로 비동기다."""
        now = now or datetime.now(UTC)
        client_id = request.resolved_client_id(self.mode)
        order = Order(
            id=str(uuid.uuid4()), client_id=client_id, mode=self.mode, market=request.market, side=request.side,
            order_type=OrderType.MARKET, amount=request.amount, quantity=request.quantity, status=OrderStatus.NEW,
            created_at=now, reason=request.reason, strategy=request.strategy, signal_time=request.signal_time,
        )
        if client_id in self.processed:
            return self._reject(order, "중복 주문 (같은 신호가 이미 처리됨)")
        ref, source = self._reference_price(request.side, price, now)
        if ref is None:
            return self._reject(order, f"체결 불가: {source}")

        try:
            if request.side is Side.BUY:
                fill_price = ref * (1 + self.slippage_rate)
                fill = self.portfolio.buy(
                    request.market, fill_price, time=now, amount=request.amount, reason=request.reason,
                )
            else:
                fill_price = ref * (1 - self.slippage_rate)
                fill = self.portfolio.sell(
                    request.market, fill_price, time=now, quantity=request.quantity, reason=request.reason,
                )
        except PortfolioError as exc:
            return self._reject(order, str(exc))

        self.processed.add(client_id)
        order.status = OrderStatus.FILLED
        order.filled_at = now
        order.fill_price = fill.price
        order.filled_quantity = fill.quantity
        order.fee = fill.fee
        order.fills.append(fill)
        order.reason = f"{request.reason} [{source}]".strip()
        return order

    def _reject(self, order: Order, error: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.error = error
        # 거부된 주문도 같은 client_id 로 재시도할 수 있게 processed 에 넣지 않는다.
        return order
