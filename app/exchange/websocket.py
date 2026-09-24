"""업비트 WebSocket 클라이언트 (Phase 2: 실시간 현재가·체결·호가·캔들).

역할 분리: REST(``upbit_client.py``)는 스냅샷·과거 데이터·계좌 조회, 이 모듈은 실시간 스트림 수신만 담당한다.
주문·자산 변동(Private 스트림 ``myOrder``/``myAsset``)은 구조만 준비해 두고 Phase 7 에서 검증한다.

공식 문서 https://docs.upbit.com/kr/reference/websocket-guide (2026-09-23 갱신본) 기준:
- Public ``wss://api.upbit.com/websocket/v1`` (ticker/trade/orderbook/candle),
  Private ``.../websocket/v1/private`` (JWT 헤더)
- 요청은 JSON 배열: ``[{"ticket": ...}, {"type": ..., "codes": [...]}, ..., {"format": "DEFAULT"}]``
- 120초 동안 송수신이 없으면 서버가 연결을 끊는다 → 라이브러리 PING 프레임(기본 30초)으로 유지
- 에러는 ``{"error": {"name", "message"}}`` 로 온다. 요청 형식 오류(WRONG_FORMAT 등)는 재연결해도 같으므로 즉시 중단
- Rate Limit: 연결 초당 5회(IP), 데이터 요청 메시지 초당 5회·분당 100회(커넥션) → 재연결 간격 최소 1초, 지수 백오프

장애 대응: 연결 끊김·타임아웃·네트워크 오류 시 지수 백오프(1s → 2s → 4s … 최대 30s)로 자동 재연결하고
구독을 다시 보낸다. 재연결 후에는 스냅샷이 다시 오므로 소비자는 ``stream_type`` 으로 구분할 수 있다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import websockets
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, WebSocketException

from app.core.exceptions import UpbitError, UpbitResponseError
from app.exchange.auth import UpbitAuth
from app.exchange.models import CandleInterval
from app.exchange.ws_models import WsDataMessage, WsErrorMessage, WsMessage, WsStatus, parse_ws_message

log = logging.getLogger(__name__)

PUBLIC_WS_URL = "wss://api.upbit.com/websocket/v1"
PRIVATE_WS_URL = "wss://api.upbit.com/websocket/v1/private"
USER_AGENT = "upbit-auto-trader/0.2"

PUBLIC_TYPES = frozenset({"ticker", "trade", "orderbook"})
PRIVATE_TYPES = frozenset({"myOrder", "myAsset"})
FORMATS = frozenset({"DEFAULT", "SIMPLE", "JSON_LIST", "SIMPLE_LIST"})
ORDERBOOK_UNITS = frozenset({1, 5, 15, 30})


class UpbitWebSocketError(UpbitError):
    """서버가 보낸 요청 오류 또는 재연결 한도 초과."""

    def __init__(self, name: str, message: str) -> None:
        self.name = name
        self.message = message
        super().__init__(f"[{name}] {message}")


# ----------------------------------------------------------------------
# 구독 요청
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Subscription:
    """요청 배열의 Data Type Object 하나."""

    type: str
    codes: tuple[str, ...] = ()
    is_only_snapshot: bool = False
    is_only_realtime: bool = False
    level: float | None = None  # orderbook 모아보기 단위 (KRW 마켓만)

    def __post_init__(self) -> None:
        if not (self.type in PUBLIC_TYPES or self.type in PRIVATE_TYPES or self.type.startswith("candle.")):
            raise ValueError(f"지원하지 않는 구독 타입: {self.type}")
        if self.type.startswith("candle."):
            CandleInterval.parse(self.type.split(".", 1)[1])  # 1s,1m,...,240m 만 유효
        codes = tuple(c.strip().upper() for c in self.codes if c and c.strip())
        if self.type not in PRIVATE_TYPES and not codes:
            raise ValueError(f"{self.type} 구독에는 codes 가 필요합니다")
        if self.is_only_snapshot and self.is_only_realtime:
            raise ValueError("is_only_snapshot 과 is_only_realtime 을 동시에 켤 수 없습니다")
        if self.level is not None and self.type != "orderbook":
            raise ValueError("level 은 orderbook 구독에서만 사용합니다")
        object.__setattr__(self, "codes", codes)

    @classmethod
    def ticker(cls, codes: Sequence[str], **kwargs: Any) -> Subscription:
        return cls("ticker", tuple(codes), **kwargs)

    @classmethod
    def trade(cls, codes: Sequence[str], **kwargs: Any) -> Subscription:
        return cls("trade", tuple(codes), **kwargs)

    @classmethod
    def orderbook(cls, codes: Sequence[str], *, units: int | None = None, **kwargs: Any) -> Subscription:
        """``units`` 를 주면 ``KRW-BTC.15`` 처럼 호가 쌍 개수를 지정한다 (1, 5, 15, 30)."""
        if units is not None:
            if units not in ORDERBOOK_UNITS:
                raise ValueError(f"호가 개수는 {sorted(ORDERBOOK_UNITS)} 중 하나여야 합니다: {units}")
            codes = [f"{c.upper()}.{units}" if "." not in c else c.upper() for c in codes]
        return cls("orderbook", tuple(codes), **kwargs)

    @classmethod
    def candle(cls, codes: Sequence[str], interval: CandleInterval | str, **kwargs: Any) -> Subscription:
        parsed = CandleInterval.parse(interval)
        if parsed.value in ("1d", "1w", "1M", "1y"):
            raise ValueError("WebSocket 캔들은 초봉·분봉(1s~240m)만 지원합니다")
        return cls(f"candle.{parsed.value}", tuple(codes), **kwargs)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type}
        if self.codes:
            payload["codes"] = list(self.codes)
        if self.is_only_snapshot:
            payload["is_only_snapshot"] = True
        if self.is_only_realtime:
            payload["is_only_realtime"] = True
        if self.level is not None:
            payload["level"] = self.level
        return payload


def build_request(subscriptions: Sequence[Subscription], *, ticket: str | None = None, fmt: str = "DEFAULT") -> str:
    """요청 메시지(JSON 배열 문자열)를 만든다: Ticket Object + Data Type Objects + Format Object."""
    if not subscriptions:
        raise ValueError("구독 목록이 비어 있습니다")
    if fmt not in FORMATS:
        raise ValueError(f"format 은 {sorted(FORMATS)} 중 하나여야 합니다: {fmt}")
    message: list[dict[str, Any]] = [{"ticket": ticket or str(uuid.uuid4())}]
    message.extend(sub.to_payload() for sub in subscriptions)
    message.append({"format": fmt})
    return json.dumps(message, ensure_ascii=False)


# ----------------------------------------------------------------------
# 연결 추상화 (테스트에서 가짜로 교체)
# ----------------------------------------------------------------------
class WsConnection(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


Connector = Callable[[str, dict[str, str]], AbstractAsyncContextManager[WsConnection]]


def default_connector(
    *, ping_interval: float = 30.0, ping_timeout: float = 10.0, open_timeout: float = 10.0
) -> Connector:
    """``websockets`` 라이브러리 연결. PING 프레임을 주기적으로 보내 120초 유휴 종료를 막는다."""

    def _connect(url: str, headers: dict[str, str]) -> AbstractAsyncContextManager[WsConnection]:
        return ws_connect(
            url,
            additional_headers=headers,
            user_agent_header=USER_AGENT,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
            open_timeout=open_timeout,
            compression="deflate",
            max_queue=1024,
        )

    return _connect


# ----------------------------------------------------------------------
# 클라이언트
# ----------------------------------------------------------------------
class ConnectionState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    CLOSED = "CLOSED"


@dataclass
class StreamStats:
    connects: int = 0
    reconnects: int = 0
    messages: int = 0
    parse_errors: int = 0
    connected_at: float | None = None
    last_message_at: float | None = None
    last_error: str | None = None
    extra: dict[str, int] = field(default_factory=dict)


class UpbitWebSocket:
    """구독 목록을 받아 메시지를 비동기로 흘려보낸다.

    사용 예::

        ws = UpbitWebSocket([Subscription.trade(["KRW-BTC"])])
        async for msg in ws.stream():
            ...
    """

    def __init__(
        self,
        subscriptions: Sequence[Subscription],
        *,
        url: str | None = None,
        auth: UpbitAuth | None = None,
        fmt: str = "DEFAULT",
        ticket: str | None = None,
        connector: Connector | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        reconnect_base_delay: float = 1.0,
        reconnect_max_delay: float = 30.0,
        max_reconnects: int | None = None,
        recv_timeout: float = 120.0,
    ) -> None:
        self._subscriptions = tuple(subscriptions)
        if not self._subscriptions:
            raise ValueError("구독 목록이 비어 있습니다")
        needs_private = any(s.type in PRIVATE_TYPES for s in self._subscriptions)
        if needs_private and auth is None:
            raise ValueError("Private 스트림(myOrder/myAsset)에는 인증(auth)이 필요합니다")
        self._url = url or (PRIVATE_WS_URL if needs_private else PUBLIC_WS_URL)
        self._auth = auth
        self._fmt = fmt
        self._ticket = ticket
        self._connector = connector or default_connector()
        self._sleep = sleep
        self._clock = clock
        self._base_delay = reconnect_base_delay
        self._max_delay = reconnect_max_delay
        self._max_reconnects = max_reconnects
        self._recv_timeout = recv_timeout
        self._closing = False
        self._conn: WsConnection | None = None
        self.state = ConnectionState.DISCONNECTED
        self.stats = StreamStats()

    @property
    def url(self) -> str:
        return self._url

    @property
    def subscriptions(self) -> tuple[Subscription, ...]:
        return self._subscriptions

    def request_message(self) -> str:
        return build_request(self._subscriptions, ticket=self._ticket, fmt=self._fmt)

    def _headers(self) -> dict[str, str]:
        # WebSocket 인증 토큰에는 query_hash 가 없다 (파라미터 없는 요청과 동일).
        return self._auth.authorization_header() if self._auth else {}

    def _reconnect_delay(self, attempt: int) -> float:
        return min(self._base_delay * (2 ** (attempt - 1)), self._max_delay)

    async def close(self) -> None:
        """소비자가 스트림을 끝낼 때 호출. 진행 중인 ``recv`` 를 깨워 ``stream()`` 이 정상 종료되게 한다."""
        self._closing = True
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001 - 종료 중 오류는 무시
                log.debug("WebSocket close 중 오류 무시", exc_info=True)
        self.state = ConnectionState.CLOSED

    async def stream(self) -> AsyncIterator[WsMessage]:
        """연결 → 구독 요청 → 메시지 yield. 끊기면 자동 재연결. 치명적 요청 오류는 예외로 올린다."""
        attempt = 0
        try:
            while not self._closing:
                self.state = ConnectionState.CONNECTING if attempt == 0 else ConnectionState.RECONNECTING
                try:
                    async with self._connector(self._url, self._headers()) as conn:
                        self._conn = conn
                        self.state = ConnectionState.CONNECTED
                        self.stats.connects += 1
                        self.stats.connected_at = self._clock()
                        if attempt:
                            self.stats.reconnects += 1
                        log.info(
                            "WebSocket 연결됨: %s (구독 %d건, 재연결 %d회)",
                            self._url, len(self._subscriptions), attempt,
                        )
                        await conn.send(self.request_message())
                        attempt = 0
                        async for message in self._receive_loop(conn):
                            yield message
                        if self._closing:
                            return
                        raise ConnectionClosed(None, None)  # 서버가 정상 종료 → 재연결
                except (ConnectionClosed, InvalidHandshake, WebSocketException, OSError, TimeoutError) as exc:
                    if self._closing:
                        return
                    self._conn = None
                    attempt += 1
                    self.stats.last_error = repr(exc)
                    if self._max_reconnects is not None and attempt > self._max_reconnects:
                        self.state = ConnectionState.DISCONNECTED
                        detail = f"재연결 {self._max_reconnects}회 실패: {exc!r}"
                        raise UpbitWebSocketError("RECONNECT_EXHAUSTED", detail) from exc
                    delay = self._reconnect_delay(attempt)
                    self.state = ConnectionState.RECONNECTING
                    log.warning("WebSocket 끊김(%r) → %.1f초 후 재연결 (%d회째)", exc, delay, attempt)
                    await self._sleep(delay)
        finally:
            self._conn = None
            if self.state is not ConnectionState.CLOSED:
                self.state = ConnectionState.DISCONNECTED if self._closing else self.state

    async def _receive_loop(self, conn: WsConnection) -> AsyncIterator[WsMessage]:
        while not self._closing:
            try:
                raw = await asyncio.wait_for(conn.recv(), timeout=self._recv_timeout)
            except TimeoutError:
                log.warning("WebSocket %.0f초 동안 수신 없음 → 재연결", self._recv_timeout)
                return
            try:
                message = parse_ws_message(raw)
            except UpbitResponseError as exc:
                self.stats.parse_errors += 1
                log.warning("WebSocket 메시지 해석 실패(무시): %s", exc)
                continue

            if isinstance(message, WsStatus):
                log.debug("WebSocket 상태: %s", message.status)
                continue
            if isinstance(message, WsErrorMessage):
                self.stats.last_error = f"{message.name}: {message.message}"
                if message.is_fatal:
                    log.error("WebSocket 요청 오류(중단): %s - %s", message.name, message.message)
                    raise UpbitWebSocketError(message.name, message.message)
                log.warning("WebSocket 오류(%s) → 재연결: %s", message.name, message.message)
                return  # TOO_MANY_REQUEST 등 → 백오프 후 재연결
            self.stats.messages += 1
            self.stats.last_message_at = self._clock()
            if isinstance(message, WsDataMessage):
                self.stats.extra[message.type] = self.stats.extra.get(message.type, 0) + 1
            yield message


__all__ = [
    "PRIVATE_WS_URL",
    "PUBLIC_WS_URL",
    "ConnectionState",
    "StreamStats",
    "Subscription",
    "UpbitWebSocket",
    "UpbitWebSocketError",
    "build_request",
    "default_connector",
    "websockets",
]
