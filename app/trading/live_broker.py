"""실제 주문 브로커 (LiveBroker) — Phase 7.

PaperBroker 와 같은 ``execute(request, price, now) -> Order`` 인터페이스로 업비트에 **시장가 주문** 을 낸다.

안전장치 (모두 통과해야 주문이 나간다):
1. ``assert_live_order_allowed(settings)`` — TRADING_MODE=LIVE 이면서 LIVE_TRADING_ENABLED=true
2. ``UpbitClient.allow_orders`` — 클라이언트 자체의 2차 잠금 (from_settings 가 같은 조건으로만 켠다)
3. ``client_id`` 중복 차단 — 같은 신호가 두 번 실행되지 않는다 (재시작 후에도 DB 이력으로 이어짐)
4. 업비트 ``identifier`` — 계정 전체에서 고유해야 하고 실패한 주문의 값도 재사용 불가 → 시도마다 접미사를 바꾼다
5. 주문 전 ``GET /v1/orders/chance`` 로 페어 상태·최소 주문 금액·잔고를 확인한다

체결 처리:
- 시장가 매수 = ``ord_type=price``(총액 KRW), 시장가 매도 = ``ord_type=market``(수량).
- 주문 응답을 받은 뒤 ``GET /v1/order`` 를 폴링해 ``done``/``cancel`` 이 될 때까지 기다린다 (기본 30초).
  시간이 지나도 미체결이면 취소 접수 후 최종 상태를 읽어 부분 체결만 반영한다.
- 체결 금액·수량·수수료는 거래소 응답(``trades``, ``executed_volume``, ``paid_fee``)을 그대로 Portfolio 에 반영한다.
- 네트워크 오류로 주문 생성 응답을 못 받았으면 identifier 로 조회해 실제로 생성됐는지 확인한다 (중복 주문 방지).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.config.settings import Settings
from app.core.exceptions import UpbitAPIError, UpbitError, UpbitNetworkError
from app.exchange.models import Account, OrderInfo
from app.exchange.upbit_client import UpbitClient
from app.trading.live_guard import assert_live_order_allowed
from app.trading.market_state import PriceState
from app.trading.orders import Order, OrderRequest, OrderStatus, OrderType
from app.trading.portfolio import Portfolio, PortfolioError, Position, Side

log = logging.getLogger(__name__)

MAX_IDENTIFIER_LENGTH = 64


def make_identifier(client_id: str, attempt: int = 1) -> str:
    """업비트 identifier(≤64자, 계정 내 고유). client_id 가 길면 해시로 줄이고 시도 번호를 붙인다."""
    suffix = f"-r{attempt}" if attempt > 1 else ""
    base = client_id
    if len(base) + len(suffix) > MAX_IDENTIFIER_LENGTH:
        digest = hashlib.sha1(client_id.encode("utf-8")).hexdigest()[:16]
        base = f"{client_id[: MAX_IDENTIFIER_LENGTH - len(suffix) - 17]}-{digest}"
    return base + suffix


def portfolio_from_accounts(
    accounts: Sequence[Account], markets: Sequence[str], *, fee_rate: float, initial_cash: float | None = None,
    now: datetime | None = None,
) -> Portfolio:
    """업비트 잔고로 Portfolio 를 만든다. 현금 = KRW 주문 가능 잔고, 포지션 = 거래 대상 마켓의 보유 코인."""
    now = now or datetime.now(UTC)
    krw = next((a for a in accounts if a.currency == "KRW"), None)
    cash = float(krw.balance) if krw else 0.0
    portfolio = Portfolio(max(initial_cash or cash, 1e-9), fee_rate=fee_rate)
    portfolio.cash = cash
    by_currency = {a.currency: a for a in accounts}
    for market in markets:
        base = market.split("-", 1)[1]
        acc = by_currency.get(base)
        if acc is None or acc.balance <= 0:
            continue
        qty = float(acc.balance)
        avg = float(acc.avg_buy_price)
        portfolio.positions[market] = Position(
            market=market, quantity=qty, avg_price=avg, opened_at=now, entry_amount=qty * avg, entry_fee=0.0,
        )
    return portfolio


class LiveBroker:
    """실제 업비트 주문. ``settings`` 의 LIVE 이중 플래그와 클라이언트 잠금을 모두 통과해야 동작한다."""

    mode = "live"

    def __init__(
        self,
        client: UpbitClient,
        portfolio: Portfolio,
        settings: Settings,
        *,
        processed_client_ids: set[str] | None = None,
        poll_interval: float = 0.5,
        fill_timeout: float = 30.0,
        max_attempts: int = 2,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.portfolio = portfolio
        self.settings = settings
        self.processed: set[str] = set(processed_client_ids or ())
        self.poll_interval = poll_interval
        self.fill_timeout = fill_timeout
        self.max_attempts = max_attempts
        self._sleep = sleep
        self.last_chance: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    async def execute(self, request: OrderRequest, price: PriceState | None, now: datetime | None = None) -> Order:
        now = now or datetime.now(UTC)
        assert_live_order_allowed(self.settings)  # 1차 잠금: 설정
        client_id = request.resolved_client_id(self.mode)
        order = Order(
            id=str(uuid.uuid4()), client_id=client_id, mode=self.mode, market=request.market, side=request.side,
            order_type=OrderType.MARKET, amount=request.amount, quantity=request.quantity, status=OrderStatus.NEW,
            created_at=now, reason=request.reason, strategy=request.strategy, signal_time=request.signal_time,
        )
        if client_id in self.processed:
            return self._reject(order, "중복 주문 (같은 신호가 이미 처리됨)")

        try:
            params = await self._build_params(request, order)
        except (UpbitError, PortfolioError, ValueError) as exc:
            return self._reject(order, f"주문 준비 실패: {exc}")

        info: OrderInfo | None = None
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            identifier = make_identifier(client_id, attempt)
            params["identifier"] = identifier
            try:
                info = await self.client.create_order(params)
                break
            except UpbitNetworkError as exc:
                # 응답을 못 받았을 뿐 주문이 생성됐을 수 있다 → identifier 로 확인 (중복 주문 방지)
                last_error = f"네트워크 오류: {exc}"
                log.warning("주문 응답 없음(%s) → identifier %s 로 확인", exc, identifier)
                info = await self._find_by_identifier(identifier)
                if info is not None:
                    break
            except UpbitAPIError as exc:
                last_error = f"거래소 거부: {exc}"
                if exc.name == "duplicated_identifier":
                    log.warning("identifier 중복(%s) → 재시도", identifier)
                    continue
                break  # 잔고 부족·최소 금액 미달·권한 오류 등은 재시도해도 같다
        if info is None:
            return self._reject(order, last_error or "주문 생성 실패")

        order.exchange_order_id = info.uuid
        self.processed.add(client_id)  # 거래소에 주문이 생긴 순간부터 같은 신호는 다시 내지 않는다
        try:
            final = await self._wait_for_fill(info)
        except UpbitError as exc:
            order.status = OrderStatus.REJECTED
            order.error = f"체결 확인 실패 (주문 uuid {info.uuid} 는 거래소에 남아 있을 수 있음): {exc}"
            return order
        return self._apply_fill(order, final, now)

    # ------------------------------------------------------------------
    async def _build_params(self, request: OrderRequest, order: Order) -> dict[str, Any]:
        chance = await self.client.get_order_chance(request.market)
        self.last_chance = {
            "market_state": chance.market_state, "bid_fee": str(chance.bid_fee), "ask_fee": str(chance.ask_fee),
        }
        if chance.market_state not in (None, "active"):
            raise ValueError(f"{request.market} 거래 불가 상태: {chance.market_state}")
        if request.side is Side.BUY:
            if request.amount is None or request.amount <= 0:
                raise ValueError("매수 금액이 없습니다")
            available = float(chance.bid_account.balance)
            amount = min(request.amount, available)
            min_total = float(chance.min_total_bid or 0)
            if amount < min_total:
                raise ValueError(
                    f"매수 금액 {amount:,.0f} KRW 가 최소 주문 금액 {min_total:,.0f} KRW 미만 (가용 {available:,.0f})"
                )
            if "price" not in chance.bid_types:
                raise ValueError(f"{request.market} 는 시장가 매수(price)를 지원하지 않음: {chance.bid_types}")
            order.amount = amount
            return self.client.market_buy_params(request.market, amount)
        position = self.portfolio.position(request.market)
        quantity = request.quantity if request.quantity is not None else (position.quantity if position else 0.0)
        available = float(chance.ask_account.balance)
        quantity = min(quantity, available)
        if quantity <= 0:
            raise ValueError("매도할 수량이 없습니다 (거래소 잔고 0)")
        if "market" not in chance.ask_types:
            raise ValueError(f"{request.market} 는 시장가 매도(market)를 지원하지 않음: {chance.ask_types}")
        order.quantity = quantity
        return self.client.market_sell_params(request.market, quantity)

    async def _find_by_identifier(self, identifier: str) -> OrderInfo | None:
        try:
            return await self.client.get_order(identifier=identifier)
        except UpbitAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        except UpbitNetworkError:
            return None

    async def _wait_for_fill(self, info: OrderInfo) -> OrderInfo:
        elapsed = 0.0
        current = info
        while not current.is_final and elapsed < self.fill_timeout:
            await self._sleep(self.poll_interval)
            elapsed += self.poll_interval
            current = await self.client.get_order(uuid=info.uuid)
        if not current.is_final:
            log.warning("주문 %s 가 %.0f초 내 체결되지 않아 취소 접수", info.uuid, self.fill_timeout)
            try:
                await self.client.cancel_order(uuid=info.uuid)
            except UpbitAPIError as exc:
                log.warning("취소 접수 실패(이미 체결됐을 수 있음): %s", exc)
            await self._sleep(self.poll_interval)
            current = await self.client.get_order(uuid=info.uuid)
        if not current.trades and current.executed_volume > 0:
            current = await self.client.get_order(uuid=info.uuid)  # 목록 응답에는 trades 가 없을 수 있다
        return current

    def _apply_fill(self, order: Order, info: OrderInfo, now: datetime) -> Order:
        executed = float(info.executed_volume)
        if executed <= 0:
            order.status = OrderStatus.REJECTED
            order.error = f"체결 없음 (거래소 상태 {info.state}, uuid {info.uuid})"
            return order
        funds = float(info.executed_funds) if info.trades else 0.0
        avg_price = funds / executed if funds > 0 else float(info.price or 0) or 0.0
        fee = float(info.paid_fee)
        reason = f"{order.reason} [upbit {info.uuid[:8]}]".strip()
        try:
            if order.side is Side.BUY:
                fill = self.portfolio.buy(
                    order.market, avg_price, time=now, quantity=executed, reason=reason, fee=fee, enforce_limits=False,
                )
            else:
                fill = self.portfolio.sell(
                    order.market, avg_price, time=now, quantity=executed, reason=reason, fee=fee, enforce_limits=False,
                )
        except PortfolioError as exc:
            order.status = OrderStatus.REJECTED
            order.error = f"체결됐지만 내부 계좌 반영 실패: {exc} (거래소 uuid {info.uuid})"
            return order
        order.status = OrderStatus.FILLED
        order.filled_at = now
        order.fill_price = fill.price
        order.filled_quantity = fill.quantity
        order.fee = fill.fee
        order.fills.append(fill)
        order.reason = reason
        if info.state == "cancel":
            order.reason += " (부분 체결 후 취소)"
        return order

    @staticmethod
    def _reject(order: Order, error: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.error = error
        return order

    # ------------------------------------------------------------------
    async def reconcile(self, markets: Sequence[str]) -> dict[str, Any]:
        """거래소 잔고와 내부 계좌를 비교해 차이를 보고하고 내부 계좌를 거래소 기준으로 맞춘다."""
        accounts = await self.client.get_accounts()
        exchange = portfolio_from_accounts(accounts, markets, fee_rate=self.portfolio.fee_rate)
        diff: dict[str, Any] = {"cash": {"internal": self.portfolio.cash, "exchange": exchange.cash}}
        for market in set(self.portfolio.positions) | set(exchange.positions):
            mine = self.portfolio.position(market)
            theirs = exchange.position(market)
            diff[market] = {
                "internal_qty": mine.quantity if mine else 0.0,
                "exchange_qty": theirs.quantity if theirs else 0.0,
            }
        self.portfolio.cash = exchange.cash
        for market in list(self.portfolio.positions):
            if market not in exchange.positions:
                del self.portfolio.positions[market]
        for market, pos in exchange.positions.items():
            mine = self.portfolio.position(market)
            if mine is None:
                self.portfolio.positions[market] = pos
            else:
                mine.quantity = pos.quantity
                mine.avg_price = pos.avg_price
                mine.entry_amount = pos.quantity * pos.avg_price
        return diff


def decimal_str(value: float | Decimal, places: int = 8) -> str:
    return f"{Decimal(str(value)):.{places}f}"
