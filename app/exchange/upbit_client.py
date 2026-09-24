"""업비트 REST API 클라이언트 (Phase 1: 페어 목록·현재가·캔들·잔고 조회).

역할 분리:
- REST(이 모듈): 스냅샷 조회, 과거 캔들, 계좌·주문 상태 조회. 폴링용.
- WebSocket(``websocket.py``, Phase 2): 실시간 체결·호가·티커 스트림.

안전:
- 이 모듈에는 주문 생성/취소 메서드가 **없다**. Phase 7 에서 별도 모듈로 추가되며,
  그 모듈은 ``app.trading.live_guard`` 를 반드시 거친다.
- 인증이 필요한 호출(``auth=True``)만 JWT 를 만든다. 키가 없으면 요청 전에 ``ConfigError``.
- Authorization 헤더·키 값은 로그에 남기지 않는다.

장애 대응:
- 타임아웃·연결 오류·5xx·429 는 GET 에 한해 지수 백오프로 재시도한다 (기본 3회).
- 4xx(400/401/403/404) 와 418(차단) 은 재시도하지 않고 즉시 예외를 던진다.
- 응답 헤더 ``Remaining-Req`` 를 Rate Limiter 에 반영한다.

공식 문서: https://docs.upbit.com/kr/reference (엔드포인트별 URL 은 각 메서드 docstring 참고)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.config.settings import Settings
from app.core.exceptions import (
    ConfigError,
    LiveTradingDisabledError,
    UpbitAPIError,
    UpbitError,
    UpbitNetworkError,
    UpbitRateLimitError,
    UpbitResponseError,
    make_api_error,
)
from app.exchange.auth import QueryParams, UpbitAuth, build_query_string, normalize_params
from app.exchange.models import Account, Candle, CandleInterval, Market, OrderChance, OrderInfo, Ticker
from app.exchange.rate_limiter import RateLimiter, rate_limit_group_for

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.upbit.com"
USER_AGENT = "upbit-auto-trader/0.1"
#: 캔들 API 가 한 번에 돌려주는 최대 개수 (공식 문서: 최대 200개)
MAX_CANDLE_COUNT = 200

Sleeper = Callable[[float], Awaitable[None]]


def encode_query(params: Sequence[tuple[str, Any]]) -> str:
    """전송용 쿼리 문자열. 문서 규칙대로 모든 값을 URL 인코딩하되 배열 파라미터 이름의 ``[]`` 는 남긴다."""
    parts: list[str] = []
    for key, value in params:
        values = value if isinstance(value, list) else [value]
        for item in values:
            parts.append(f"{quote(str(key), safe='[]')}={quote(str(item), safe='')}")
    return "&".join(parts)


def format_to_param(to: datetime | str | None) -> str | None:
    """캔들 ``to`` 파라미터(ISO 8601). 시간대 없는 datetime 은 모호하므로 거부한다."""
    if to is None:
        return None
    if isinstance(to, str):
        return to
    if to.tzinfo is None:
        raise ValueError("`to` 는 시간대가 있는 datetime 이어야 합니다 (예: datetime.now(UTC))")
    return to.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class UpbitClient:
    """비동기 업비트 REST 클라이언트. ``async with UpbitClient(...) as client:`` 로 사용한다."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        access_key: str | None = None,
        secret_key: str | None = None,
        timeout: float = 10.0,
        max_retries: int = 3,
        rate_limiter: RateLimiter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleeper = asyncio.sleep,
        retry_base_delay: float = 0.5,
        allow_orders: bool = False,
    ) -> None:
        self._auth = UpbitAuth(access_key, secret_key) if access_key and secret_key else None
        # 실제 주문 API(생성·취소)는 이 플래그가 True 일 때만 호출된다. from_settings 는 LIVE 이중 플래그가
        # 모두 켜진 경우에만 True 로 만든다 (app.trading.live_guard 와 별개의 2차 잠금).
        self._allow_orders = allow_orders
        self._max_retries = max_retries
        self._limiter = rate_limiter or RateLimiter()
        self._sleep = sleep
        self._retry_base_delay = retry_base_delay
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> UpbitClient:
        access = settings.upbit_access_key.get_secret_value() if settings.upbit_access_key else None
        secret = settings.upbit_secret_key.get_secret_value() if settings.upbit_secret_key else None
        kwargs: dict[str, Any] = {
            "base_url": settings.upbit_api_url,
            "access_key": access,
            "secret_key": secret,
            "timeout": settings.http_timeout_seconds,
            "max_retries": settings.http_max_retries,
            "allow_orders": settings.is_live_trading_allowed,
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    @property
    def has_auth(self) -> bool:
        return self._auth is not None

    @property
    def orders_allowed(self) -> bool:
        return self._allow_orders

    def _require_orders_allowed(self, what: str) -> None:
        if not self._allow_orders:
            raise LiveTradingDisabledError(
                f"{what} 차단: 이 클라이언트는 실제 주문이 허용되지 않았습니다 "
                "(TRADING_MODE=LIVE 와 LIVE_TRADING_ENABLED=true 가 모두 필요)"
            )

    @property
    def rate_limiter(self) -> RateLimiter:
        return self._limiter

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> UpbitClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # 시세 (Quotation, 인증 불필요)
    # ------------------------------------------------------------------
    async def get_markets(self, *, is_details: bool = False) -> list[Market]:
        """페어 목록 조회 ``GET /v1/market/all`` (https://docs.upbit.com/kr/reference/list-trading-pairs)."""
        data = await self._request("GET", "/v1/market/all", params={"is_details": is_details})
        return self._parse_list(Market, data)

    async def get_tickers(self, markets: Sequence[str] | str) -> list[Ticker]:
        """페어 단위 현재가 조회 ``GET /v1/ticker?markets=KRW-BTC,KRW-ETH``
        (https://docs.upbit.com/kr/reference/list-tickers). Rate Limit 그룹 ``ticker`` 초당 10회."""
        codes = [markets] if isinstance(markets, str) else list(markets)
        if not codes:
            raise ValueError("markets 가 비어 있습니다")
        data = await self._request("GET", "/v1/ticker", params={"markets": ",".join(codes)})
        return self._parse_list(Ticker, data)

    async def get_ticker(self, market: str) -> Ticker:
        tickers = await self.get_tickers([market])
        if not tickers:
            raise UpbitResponseError(f"{market} 현재가 응답이 비어 있습니다")
        return tickers[0]

    async def get_candles(
        self,
        market: str,
        interval: CandleInterval | str,
        *,
        count: int = MAX_CANDLE_COUNT,
        to: datetime | str | None = None,
        converting_price_unit: str | None = None,
    ) -> list[Candle]:
        """캔들 조회. 업비트 응답 순서 그대로 **최신순** 으로 돌려준다.

        - 초봉  ``GET /v1/candles/seconds``
        - 분봉  ``GET /v1/candles/minutes/{1,3,5,10,15,30,60,240}``
        - 일봉  ``GET /v1/candles/days`` (``converting_price_unit`` 로 KRW 환산 종가 요청 가능)
        - 주/월/연봉 ``GET /v1/candles/{weeks,months,years}``
        ``count`` 최대 200, ``to`` 는 "지정 시각 이전" 캔들부터 조회한다.
        """
        interval = CandleInterval.parse(interval)
        if not 1 <= count <= MAX_CANDLE_COUNT:
            raise ValueError(f"count 는 1~{MAX_CANDLE_COUNT} 사이여야 합니다: {count}")
        params: dict[str, Any] = {"market": market, "to": format_to_param(to), "count": count}
        if interval is CandleInterval.D1 and converting_price_unit:
            params["converting_price_unit"] = converting_price_unit
        data = await self._request("GET", interval.path, params=params)
        return self._parse_list(Candle, data)

    async def get_candles_minutes(
        self, market: str, unit: int = 1, *, count: int = MAX_CANDLE_COUNT, to: datetime | str | None = None
    ) -> list[Candle]:
        return await self.get_candles(market, CandleInterval.parse(f"{unit}m"), count=count, to=to)

    async def get_candles_days(
        self,
        market: str,
        *,
        count: int = MAX_CANDLE_COUNT,
        to: datetime | str | None = None,
        converting_price_unit: str | None = None,
    ) -> list[Candle]:
        return await self.get_candles(
            market, CandleInterval.D1, count=count, to=to, converting_price_unit=converting_price_unit
        )

    async def get_candles_range(
        self,
        market: str,
        interval: CandleInterval | str,
        *,
        start: datetime,
        end: datetime | None = None,
        max_requests: int = 500,
    ) -> list[Candle]:
        """``[start, end]`` 구간의 캔들을 200개씩 ``to`` 를 옮겨가며 모두 받아 **과거→최신** 순으로 돌려준다.

        백테스트용 과거 데이터 수집에 쓴다. Rate Limit(캔들 그룹 초당 10회)은 자동으로 지켜진다.
        """
        interval = CandleInterval.parse(interval)
        if start.tzinfo is None or (end is not None and end.tzinfo is None):
            raise ValueError("start/end 는 시간대가 있는 datetime 이어야 합니다")
        end = end or datetime.now(UTC)
        if start >= end:
            raise ValueError("start 는 end 보다 앞서야 합니다")

        collected: dict[datetime, Candle] = {}
        to: datetime | None = end
        previous_oldest: datetime | None = None
        for _ in range(max_requests):
            batch = await self.get_candles(market, interval, count=MAX_CANDLE_COUNT, to=to)
            if not batch:
                break
            for candle in batch:
                if start <= candle.candle_date_time_utc <= end:
                    collected[candle.candle_date_time_utc] = candle
            oldest = min(c.candle_date_time_utc for c in batch)
            if oldest <= start or len(batch) < MAX_CANDLE_COUNT:
                break
            if previous_oldest is not None and oldest >= previous_oldest:
                break  # 더 이상 과거로 진행하지 못함 (무한 루프 방지)
            previous_oldest = oldest
            to = oldest
        return sorted(collected.values(), key=lambda c: c.candle_date_time_utc)

    # ------------------------------------------------------------------
    # 자산 (Exchange, 인증 필요: [자산조회] 권한)
    # ------------------------------------------------------------------
    async def get_accounts(self) -> list[Account]:
        """포켓 잔고 조회 ``GET /v1/accounts`` (https://docs.upbit.com/kr/reference/get-balance).
        Rate Limit 그룹 ``default`` 초당 30회(포켓 단위)."""
        data = await self._request("GET", "/v1/accounts", auth=True)
        return self._parse_list(Account, data)

    # ------------------------------------------------------------------
    # 주문 (Exchange, 인증 필요: 조회는 [주문조회], 생성·취소는 [주문하기] 권한)
    # ------------------------------------------------------------------
    @staticmethod
    def market_buy_params(market: str, amount_krw: float, identifier: str | None = None) -> dict[str, Any]:
        """시장가 매수: ``ord_type=price``, ``price`` 에 매수 총액(KRW). volume 은 넣지 않는다."""
        if amount_krw <= 0:
            raise ValueError("매수 금액은 0보다 커야 합니다")
        params: dict[str, Any] = {"market": market, "side": "bid", "ord_type": "price", "price": f"{amount_krw:.0f}"}
        if identifier:
            params["identifier"] = identifier
        return params

    @staticmethod
    def market_sell_params(market: str, volume: float, identifier: str | None = None) -> dict[str, Any]:
        """시장가 매도: ``ord_type=market``, ``volume`` 에 수량. price 는 넣지 않는다."""
        if volume <= 0:
            raise ValueError("매도 수량은 0보다 커야 합니다")
        params: dict[str, Any] = {"market": market, "side": "ask", "ord_type": "market", "volume": f"{volume:.8f}"}
        if identifier:
            params["identifier"] = identifier
        return params

    async def get_order_chance(self, market: str) -> OrderChance:
        """페어별 주문 가능 정보 ``GET /v1/orders/chance`` (수수료율, 최소 주문 금액, 잔고)."""
        data = await self._request("GET", "/v1/orders/chance", params={"market": market}, auth=True)
        return self._parse_one(OrderChance, data)

    async def get_order(self, *, uuid: str | None = None, identifier: str | None = None) -> OrderInfo:
        """개별 주문 조회 ``GET /v1/order`` (체결 목록 포함)."""
        if not uuid and not identifier:
            raise ValueError("uuid 또는 identifier 가 필요합니다")
        params = {"uuid": uuid} if uuid else {"identifier": identifier}
        data = await self._request("GET", "/v1/order", params=params, auth=True)
        return self._parse_one(OrderInfo, data)

    async def get_open_orders(
        self, market: str | None = None, *, states: Sequence[str] = ("wait", "watch"), limit: int = 100
    ) -> list[OrderInfo]:
        """체결 대기 주문 ``GET /v1/orders/open``."""
        params: list[tuple[str, Any]] = []
        if market:
            params.append(("market", market))
        params.append(("states[]", list(states)))
        params.append(("limit", limit))
        data = await self._request("GET", "/v1/orders/open", params=params, auth=True)
        return self._parse_list(OrderInfo, data)

    async def get_closed_orders(
        self, market: str | None = None, *, states: Sequence[str] = ("done", "cancel"), limit: int = 100
    ) -> list[OrderInfo]:
        """종료 주문 ``GET /v1/orders/closed``."""
        params: list[tuple[str, Any]] = []
        if market:
            params.append(("market", market))
        params.append(("states[]", list(states)))
        params.append(("limit", limit))
        data = await self._request("GET", "/v1/orders/closed", params=params, auth=True)
        return self._parse_list(OrderInfo, data)

    async def test_order(self, params: Mapping[str, Any]) -> OrderInfo:
        """주문 생성 테스트 ``POST /v1/orders/test`` — 실제 주문을 만들지 않고 파라미터·잔고·권한을 검증한다."""
        body = {k: v for k, v in params.items() if v is not None}
        data = await self._request("POST", "/v1/orders/test", json_body=body, auth=True)
        return self._parse_one(OrderInfo, data)

    async def create_order(self, params: Mapping[str, Any]) -> OrderInfo:
        """실제 주문 생성 ``POST /v1/orders``. ``allow_orders`` 가 아니면 요청 전에 차단된다."""
        self._require_orders_allowed("주문 생성")
        body = {k: v for k, v in params.items() if v is not None}
        data = await self._request("POST", "/v1/orders", json_body=body, auth=True)
        return self._parse_one(OrderInfo, data)

    async def cancel_order(self, *, uuid: str | None = None, identifier: str | None = None) -> OrderInfo:
        """주문 취소 접수 ``DELETE /v1/order``."""
        self._require_orders_allowed("주문 취소")
        if not uuid and not identifier:
            raise ValueError("uuid 또는 identifier 가 필요합니다")
        params = {"uuid": uuid} if uuid else {"identifier": identifier}
        data = await self._request("DELETE", "/v1/order", params=params, auth=True)
        return self._parse_one(OrderInfo, data)

    # ------------------------------------------------------------------
    # 내부 구현
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_one(model: type[Any], data: Any) -> Any:
        if not isinstance(data, dict):
            raise UpbitResponseError(f"{model.__name__} 객체를 기대했지만 다른 형식이 왔습니다: {type(data).__name__}")
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise UpbitResponseError(f"{model.__name__} 응답 해석 실패: {exc}") from exc

    @staticmethod
    def _parse_list(model: type[Any], data: Any) -> list[Any]:
        if not isinstance(data, list):
            raise UpbitResponseError(f"{model.__name__} 목록을 기대했지만 다른 형식이 왔습니다: {type(data).__name__}")
        try:
            return [model.model_validate(item) for item in data]
        except ValidationError as exc:
            raise UpbitResponseError(f"{model.__name__} 응답 해석 실패: {exc}") from exc

    def _require_auth(self) -> UpbitAuth:
        if self._auth is None:
            raise ConfigError(
                "인증이 필요한 API 입니다. .env 에 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 를 설정하세요."
            )
        return self._auth

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: QueryParams | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool = False,
        retry: bool | None = None,
    ) -> Any:
        method = method.upper()
        if retry is None:
            retry = method == "GET"  # 주문처럼 멱등하지 않은 요청은 기본적으로 재시도하지 않는다.

        normalized = normalize_params(params)
        query_string = build_query_string(normalized)  # 해시용 (인코딩 전, 순서 유지)
        url = f"{path}?{encode_query(normalized)}" if normalized else path
        group = rate_limit_group_for(method, path)
        # 인증 여부는 재시도 전에 확정한다: 키가 없으면 네트워크 요청 없이 즉시 실패.
        auth_handler = self._require_auth() if auth else None

        attempt = 0
        while True:
            attempt += 1
            headers: dict[str, str] = {}
            if auth_handler is not None:
                body_qs = build_query_string(json_body) if json_body else ""
                headers.update(auth_handler.authorization_header(query_string or body_qs))

            await self._limiter.acquire(group)
            started = time.perf_counter()
            error: UpbitError
            try:
                response = await self._client.request(method, url, json=json_body, headers=headers)
            except httpx.TimeoutException as exc:
                error = UpbitNetworkError(f"{method} {path} 타임아웃: {exc!r}")
            except httpx.HTTPError as exc:
                error = UpbitNetworkError(f"{method} {path} 네트워크 오류: {exc!r}")
            else:
                elapsed_ms = (time.perf_counter() - started) * 1000
                remaining = self._limiter.update_from_headers(response.headers)
                log.debug(
                    "%s %s -> %s (%.0f ms, remaining=%s)",
                    method, path, response.status_code, elapsed_ms,
                    f"{remaining.group}:{remaining.second}" if remaining else "-",
                )
                if response.is_success:
                    return self._decode_body(response)
                error = self._make_error(response)
                if isinstance(error, UpbitRateLimitError):
                    self._limiter.penalize(group, 1.0)

            retryable = isinstance(error, UpbitNetworkError) or (
                isinstance(error, UpbitAPIError) and error.retryable
            )
            if retry and retryable and attempt <= self._max_retries:
                delay = self._retry_delay(attempt, error)
                log.warning(
                    "%s %s 실패(%s) → %.1f초 후 재시도 %d/%d",
                    method, path, error, delay, attempt, self._max_retries,
                )
                await self._sleep(delay)
                continue
            log.error("%s %s 최종 실패: %s", method, path, error)
            raise error

    def _retry_delay(self, attempt: int, error: UpbitError) -> float:
        if isinstance(error, UpbitRateLimitError):
            return 1.0  # 문서 권고: 다음 초 경계까지 대기
        return min(self._retry_base_delay * (2 ** (attempt - 1)), 8.0)

    @staticmethod
    def _decode_body(response: httpx.Response) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise UpbitResponseError(f"JSON 이 아닌 응답: {response.text[:200]!r}") from exc

    @staticmethod
    def _make_error(response: httpx.Response) -> UpbitAPIError:
        name: str | int | None = None
        message = response.text[:300]
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            err = payload["error"]
            name = err.get("name")
            message = str(err.get("message", message))
        return make_api_error(
            response.status_code, name, message, remaining_req=response.headers.get("Remaining-Req")
        )
