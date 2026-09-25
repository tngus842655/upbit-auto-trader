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
- 네트워크 오류로 주문 생성 응답을 못 받았으면 **같은 identifier 로만** 백오프 재조회해 생성 여부를 확인한다.
  끝내 확인이 안 되면 ``UNKNOWN`` 으로 남기고, 절대 새 identifier 로 다시 주문하지 않는다
  (중복 주문 방지, 감사 CRITICAL-1).
  재조회가 전부 404(order_not_found) 이면 — 수 초 간격으로 반복한 뒤에만 — 미생성으로 판정한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
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
from app.trading.portfolio import DEFAULT_MIN_ORDER_AMOUNT, Portfolio, PortfolioError, Position, Side

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
    now: datetime | None = None, reference_prices: Mapping[str, float] | None = None,
    min_order_amount: float = DEFAULT_MIN_ORDER_AMOUNT,
) -> Portfolio:
    """업비트 잔고로 Portfolio 를 만든다. 현금 = KRW 잔고(주문에 묶인 locked 포함), 포지션 = 거래 대상 마켓의 보유 코인
    (역시 locked 포함 — 시장가 매도가 접수돼 수량이 locked 로 옮겨간 순간에도 포지션은 그대로다, 감사 MEDIUM-3).

    감사 MEDIUM-2:
    - 평가액(``reference_prices`` 의 현재가, 없으면 평균 매수가 기준)이 최소 주문 금액 미만인 **먼지 잔고**는
      포지션에 넣지 않고 ``portfolio.dust`` 에 둔다 — 거래소가 매도를 거부(under_min_total_ask)하는 수량이 마켓을
      점유하고 손절 매도를 1초마다 되풀이하지 않게.
    - ``avg_buy_price`` 가 0 인 코인(포켓 이전·입금 등, 매수 단가를 모름)은 현재가를 기준가로 삼고
      ``cost_known=False`` 로 표시한다. 현재가도 없으면 기준가 0 으로 두되, ``RiskManager.check_exits`` 가 처음 본
      시세를 기준가로 채운다.
    """
    now = now or datetime.now(UTC)
    krw = next((a for a in accounts if a.currency == "KRW"), None)
    cash = float(krw.total) if krw else 0.0
    portfolio = Portfolio(max(initial_cash or cash, 1e-9), fee_rate=fee_rate, min_order_amount=min_order_amount)
    portfolio.cash = cash
    by_currency = {a.currency: a for a in accounts}
    for market in markets:
        base = market.split("-", 1)[1]
        acc = by_currency.get(base)
        if acc is None or acc.total <= 0:
            continue
        qty = float(acc.total)
        avg = float(acc.avg_buy_price)
        reference = float((reference_prices or {}).get(market) or 0.0)
        valuation = reference or avg
        if valuation > 0 and qty * valuation < min_order_amount:
            portfolio.dust[market] = qty
            log.warning("%s 잔고 %.8f (평가 %.0f원) 은 최소 주문 금액 %.0f원 미만 → 포지션에서 제외", market, qty,
                        qty * valuation, min_order_amount)
            continue
        cost_known = avg > 0
        price = avg if cost_known else reference
        if not cost_known:
            log.warning("%s 평균 매수가 0 (포켓 이전 등) → 기준가 %s, 손절·익절·손익은 기준가 기준", market,
                        f"{price:,.0f}원" if price else "미정 (첫 시세로 채움)")
        portfolio.positions[market] = Position(
            market=market, quantity=qty, avg_price=price, opened_at=now, entry_amount=qty * price, entry_fee=0.0,
            cost_known=cost_known,
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
        lookup_attempts: int = 5,
        lookup_backoff: float = 1.0,
        trades_lookup_attempts: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.portfolio = portfolio
        self.settings = settings
        self.processed: set[str] = set(processed_client_ids or ())
        self.poll_interval = poll_interval
        self.fill_timeout = fill_timeout
        self.max_attempts = max_attempts
        self.lookup_attempts = max(1, lookup_attempts)
        self.lookup_backoff = lookup_backoff
        self.trades_lookup_attempts = max(0, trades_lookup_attempts)  # 체결 목록이 비어 있을 때 다시 받는 횟수
        # 주문 실행·미확정 주문 확정·잔고 동기화는 모두 내부 계좌를 바꾼다. 시세 루프의 청산 주문과 메인 루프의 동기화가
        # 겹치면(주문 체결 대기 중 동기화가 잔고 스냅샷으로 포지션을 지우거나 되살림) 왕복 기록이 빠지므로 직렬화한다
        # (감사 MEDIUM-3).
        self._lock = asyncio.Lock()
        self._sleep = sleep
        self.last_chance: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    async def execute(self, request: OrderRequest, price: PriceState | None, now: datetime | None = None) -> Order:
        async with self._lock:
            return await self._execute_locked(request, price, now)

    async def _execute_locked(
        self, request: OrderRequest, price: PriceState | None, now: datetime | None = None
    ) -> Order:
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
            order.exchange_identifier = identifier
            try:
                info = await self.client.create_order(params)
                break
            except UpbitNetworkError as exc:
                # 응답을 못 받았을 뿐 주문이 생성됐을 수 있다 → 같은 identifier 로만 확인한다.
                # 새 identifier 로 재주문하면 중복 매수/매도가 되므로 절대 하지 않는다 (감사 CRITICAL-1).
                log.warning("주문 응답 없음(%s) → identifier %s 로 생성 여부 확인", exc, identifier)
                found, confirmed_missing = await self._confirm_by_identifier(identifier)
                if found is not None:
                    info = found
                    break
                if confirmed_missing:
                    return self._reject(order, f"네트워크 오류 뒤 주문 미생성 확인 (identifier {identifier}): {exc}")
                self.processed.add(client_id)  # 확인될 때까지 같은 신호를 다시 내지 않는다
                order.status = OrderStatus.UNKNOWN
                order.error = (
                    "주문 응답 없음, 생성 여부 확인 실패 — 거래소에 주문이 남아 있을 수 있음 "
                    f"(identifier {identifier}, 운영자 확인 필요): {exc}"
                )
                log.error("주문 상태 미확인 %s %s: %s", order.market, order.side.value, order.error)
                return order
            except UpbitAPIError as exc:
                last_error = f"거래소 거부: {exc}"
                if exc.name == "duplicated_identifier":
                    log.warning("identifier 중복(%s) → 재시도", identifier)
                    continue
                break  # 잔고 부족·최소 금액 미달·권한 오류 등은 재시도해도 같다
        if info is None:
            return self._reject(order, last_error or "주문 생성 실패")

        order.exchange_order_id = info.uuid
        order.status = OrderStatus.SUBMITTED
        self.processed.add(client_id)  # 거래소에 주문이 생긴 순간부터 같은 신호는 다시 내지 않는다
        try:
            final = await self._wait_for_fill(info)
        except UpbitError as exc:
            # 주문은 거래소에 있다. 체결 여부를 모르므로 REJECTED 로 축약하지 않고 UNKNOWN 으로 남겨
            # 후속 확정한다 (감사 HIGH-1)
            order.status = OrderStatus.UNKNOWN
            order.error = f"체결 확인 실패 — 거래소 주문 uuid {info.uuid} 는 남아 있음, 후속 조회로 확정: {exc}"
            log.error("주문 상태 미확인 %s %s: %s", order.market, order.side.value, order.error)
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

    async def _confirm_by_identifier(self, identifier: str) -> tuple[OrderInfo | None, bool]:
        """네트워크 오류 뒤 주문 존재 확인 → (주문, 미생성 확정 여부).

        - 조회가 되면 그 주문을 돌려준다 (재주문 없이 이어서 처리).
        - 모든 시도가 404(order_not_found) 이면 미생성으로 확정한다 — 반드시 백오프 간격을 두고 반복한 뒤에만.
        - 조회 자체가 실패(타임아웃·기타 오류)한 시도가 하나라도 있으면 확정하지 않는다 → (None, False).
        """
        delay = self.lookup_backoff
        not_found = 0
        for attempt in range(1, self.lookup_attempts + 1):
            try:
                return await self.client.get_order(identifier=identifier), False
            except UpbitAPIError as exc:
                if exc.status_code == 404:
                    not_found += 1
                else:
                    log.warning("identifier %s 조회 실패(%s)", identifier, exc)
            except UpbitError as exc:
                log.warning("identifier %s 조회 실패(%s)", identifier, exc)
            if attempt < self.lookup_attempts:
                await self._sleep(delay)
                delay = min(delay * 2, 15.0)
        return None, not_found == self.lookup_attempts

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
        return await self._refresh_trades(current)

    async def _refresh_trades(self, info: OrderInfo) -> OrderInfo:
        """체결이 있는데 체결 목록이 비어 있으면(체결 직후 지연·목록 응답) 개별 조회로 몇 번 다시 받는다."""
        current = info
        for _ in range(self.trades_lookup_attempts):
            if current.trades or current.executed_volume <= 0:
                break
            await self._sleep(self.poll_interval)
            try:
                current = await self.client.get_order(uuid=info.uuid)
            except UpbitError as exc:
                log.warning("주문 %s 체결 목록 재조회 실패: %s", info.uuid, exc)
                break
        return current

    def _apply_fill(self, order: Order, info: OrderInfo, now: datetime) -> Order:
        executed = float(info.executed_volume)
        if executed <= 0:
            order.status = OrderStatus.CANCELLED if info.state == "cancel" else OrderStatus.REJECTED
            order.error = f"체결 없음 (거래소 상태 {info.state}, uuid {info.uuid})"
            return order
        funds = info.fill_funds()
        if funds is None or funds <= 0:
            # 체결 목록이 없고 주문 종류로도 체결 금액을 알 수 없다 (시장가 매도·부분 체결 뒤 취소).
            # 시장가 매수의 price(총액)를 단가로 쓰면 평균가·현금이 크게 틀리므로(감사 MEDIUM-1) 반영하지 않고
            # UNKNOWN 으로 남겨 후속 조회로 확정한다.
            order.status = OrderStatus.UNKNOWN
            order.error = (
                f"체결 금액 미확인 — 체결 수량 {executed:.8f} 인데 체결 목록이 비어 있음 "
                f"(uuid {info.uuid}, {info.ord_type}/{info.state}), 후속 조회로 확정"
            )
            log.warning("주문 %s %s: %s", order.market, order.side.value, order.error)
            return order
        avg_price = float(funds) / executed
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
        order.status = OrderStatus.PARTIAL if info.state == "cancel" else OrderStatus.FILLED
        order.filled_at = now
        order.fill_price = fill.price
        order.filled_quantity = fill.quantity
        order.fee = fill.fee
        order.fills.append(fill)
        order.reason = reason
        if info.state == "cancel":
            order.reason += " (부분 체결 후 취소)"
        return order

    async def resolve_order(self, order: Order, now: datetime | None = None) -> Order | None:
        async with self._lock:
            return await self._resolve_locked(order, now)

    async def _resolve_locked(self, order: Order, now: datetime | None = None) -> Order | None:
        """UNKNOWN 주문의 후속 확정 — uuid(없으면 identifier)로 조회해 최종 상태면 체결을 반영한 Order 를 돌려준다.

        - 아직 체결 대기면: fill_timeout 이 지났을 때 취소를 접수하고 None (다음 호출에서 확정).
        - identifier 조회가 404 면 미생성 확정 → REJECTED. 조회 자체가 실패하면 None (UNKNOWN 유지).
        """
        now = now or datetime.now(UTC)
        try:
            if order.exchange_order_id:
                info = await self.client.get_order(uuid=order.exchange_order_id)
            elif order.exchange_identifier:
                info = await self.client.get_order(identifier=order.exchange_identifier)
            else:
                return self._reject(order, "확정 불가: 거래소 uuid·identifier 없음")
        except UpbitAPIError as exc:
            if exc.status_code == 404 and not order.exchange_order_id:
                return self._reject(
                    order, f"네트워크 오류 뒤 주문 미생성 확인 (identifier {order.exchange_identifier})"
                )
            log.warning("주문 %s 확정 조회 실패: %s", order.id, exc)
            return None
        except UpbitError as exc:
            log.warning("주문 %s 확정 조회 실패: %s", order.id, exc)
            return None
        order.exchange_order_id = info.uuid
        if not info.is_final:
            age = (now - order.created_at).total_seconds()
            if age >= self.fill_timeout:
                log.warning("주문 %s 가 %.0f초째 미체결 → 취소 접수", info.uuid, age)
                with contextlib.suppress(UpbitError):
                    await self.client.cancel_order(uuid=info.uuid)
            return None
        info = await self._refresh_trades(info)
        final = self._apply_fill(order, info, now)
        if final.status is OrderStatus.UNKNOWN:
            return None  # 체결 금액을 아직 모른다 → 다음 조회에서 다시 (시한이 지나면 엔진이 잔고 동기화로 정리)
        return final

    @staticmethod
    def _reject(order: Order, error: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.error = error
        return order

    # ------------------------------------------------------------------
    async def reconcile(
        self, markets: Sequence[str], prices: Mapping[str, float] | None = None,
        exclude: Collection[str] | None = None,
    ) -> dict[str, Any]:
        """거래소 잔고와 내부 계좌를 비교해 차이를 보고하고 내부 계좌를 거래소 기준으로 맞춘다.

        ``prices``(현재 시세)는 먼지 잔고 판정과 평균 매수가 0 인 코인의 기준가에 쓴다. 이미 기준가를 정한 포지션은
        그 기준가를 유지한다(동기화 때마다 시세로 바뀌지 않게). 결과 diff 에 ``dust``·``cost_unknown`` 을 넣는다.
        ``exclude`` 는 미확정(UNKNOWN) 주문이 남아 있는 마켓 — 체결이 내부에 반영되기 전이라 거래소 스냅샷으로 덮으면
        나중에 확정될 체결이 이중 반영되거나(포지션 없음 → REJECTED) 왕복 기록이 빠진다. 그 마켓의 포지션과 현금은
        손대지 않고 diff 의 ``skipped`` 로만 알린다 (감사 MEDIUM-3). 주문 실행과 같은 락 안에서 돈다.
        """
        async with self._lock:
            return await self._reconcile_locked(markets, prices, set(exclude or ()))

    async def _reconcile_locked(
        self, markets: Sequence[str], prices: Mapping[str, float] | None, exclude: set[str]
    ) -> dict[str, Any]:
        accounts = await self.client.get_accounts()
        references: dict[str, float] = dict(prices or {})
        for market, mine in self.portfolio.positions.items():
            if not mine.cost_known and mine.avg_price > 0:
                references[market] = mine.avg_price
        exchange = portfolio_from_accounts(
            accounts, markets, fee_rate=self.portfolio.fee_rate, reference_prices=references,
            min_order_amount=self.portfolio.min_order_amount,
        )
        diff: dict[str, Any] = {"cash": {"internal": self.portfolio.cash, "exchange": exchange.cash}}
        for market in set(self.portfolio.positions) | set(exchange.positions):
            mine = self.portfolio.position(market)
            theirs = exchange.position(market)
            diff[market] = {
                "internal_qty": mine.quantity if mine else 0.0,
                "exchange_qty": theirs.quantity if theirs else 0.0,
            }
        if exchange.dust:
            diff["dust"] = dict(exchange.dust)
        unknown = [m for m, p in exchange.positions.items() if not p.cost_known]
        if unknown:
            diff["cost_unknown"] = unknown
        if exclude:
            diff["skipped"] = sorted(exclude)
            log.info("잔고 동기화: 미확정 주문 마켓 %s 와 현금은 건너뜀", sorted(exclude))
        else:
            self.portfolio.cash = exchange.cash
        self.portfolio.dust = {m: q for m, q in exchange.dust.items() if m not in exclude}
        for market in list(self.portfolio.positions):
            if market not in exchange.positions and market not in exclude:
                del self.portfolio.positions[market]
        for market, pos in exchange.positions.items():
            if market in exclude:
                continue
            mine = self.portfolio.position(market)
            if mine is None:
                self.portfolio.positions[market] = pos
            else:
                mine.quantity = pos.quantity
                mine.avg_price = pos.avg_price
                mine.cost_known = pos.cost_known
                mine.entry_amount = pos.quantity * pos.avg_price
        return diff


def decimal_str(value: float | Decimal, places: int = 8) -> str:
    return f"{Decimal(str(value)):.{places}f}"
