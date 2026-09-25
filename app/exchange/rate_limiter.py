"""요청 수 제한(Rate Limit) 준수.

공식 문서 https://docs.upbit.com/kr/reference/rate-limits (2026-09-08 갱신본) 기준:

- Quotation(시세) REST: ``market`` / ``candle`` / ``trade`` / ``ticker`` / ``orderbook`` 그룹이
  각각 초당 10회, **IP 단위**로 측정된다.
- Exchange(거래·자산) REST: ``default`` 초당 30회, ``order`` 초당 12회, ``order-test`` 초당 8회,
  ``order-cancel-all`` 2초당 1회, **포켓 단위**로 측정된다.
- 응답 헤더 ``Remaining-Req: group=default; min=1800; sec=29`` (``min`` 은 deprecated, ``sec`` 만 사용).
- 429: 다음 초 경계까지 대기 후 재시도. 418: 누적 위반으로 일시 차단 → 자동 재시도 금지.

구현: 그룹별 슬라이딩 윈도우 카운터로 클라이언트 쪽에서 먼저 속도를 조절하고(기본 한도 -1 여유),
서버가 알려준 잔여 요청 수가 0 이거나 429 를 받으면 해당 그룹을 잠시 막는다.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

REMAINING_REQ_HEADER = "Remaining-Req"
RETRY_AFTER_HEADER = "Retry-After"  # 공식 문서(2026-09-25 확인)에는 없는 헤더 — 오면 따르고, 없어도 정상

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RateLimitRule:
    group: str
    max_requests: int
    per_seconds: float = 1.0


#: 공식 문서 기준 그룹별 한도
RATE_LIMIT_RULES: dict[str, RateLimitRule] = {
    # Quotation REST (IP 단위)
    "market": RateLimitRule("market", 10),
    "candle": RateLimitRule("candle", 10),
    "trade": RateLimitRule("trade", 10),
    "ticker": RateLimitRule("ticker", 10),
    "orderbook": RateLimitRule("orderbook", 10),
    # Exchange REST (포켓 단위)
    "default": RateLimitRule("default", 30),
    "order": RateLimitRule("order", 12),
    "order-test": RateLimitRule("order-test", 8),
    "order-cancel-all": RateLimitRule("order-cancel-all", 1, 2.0),
}


def rate_limit_group_for(method: str, path: str) -> str:
    """API 경로를 Rate Limit 그룹으로 매핑한다.

    Phase 1 에서 실제로 쓰는 것은 시세 그룹과 ``default`` 뿐이다. 주문 관련 매핑은
    Phase 7 에서 주문 API 문서와 함께 다시 검증한다.
    """
    method = method.upper()
    p = path.lower()
    if p.startswith("/v1/market"):
        return "market"
    if p.startswith("/v1/candles"):
        return "candle"
    if p.startswith("/v1/trades"):
        return "trade"
    if p.startswith("/v1/ticker"):
        return "ticker"
    if p.startswith("/v1/orderbook"):
        return "orderbook"
    if p.startswith("/v1/orders/test"):
        return "order-test"
    if p.startswith("/v1/orders/open") and method == "DELETE":
        return "order-cancel-all"
    if p.startswith("/v1/orders/cancel_and_new"):
        return "order"
    if p == "/v1/orders" and method == "POST":
        return "order"
    return "default"


@dataclass(frozen=True)
class RemainingReq:
    group: str
    minute: int | None  # 문서상 deprecated — 참조하지 않는다 (2026-09-25 확인)
    second: int | None  # 현재 잔여 요청 수. 0 이면 잠시 뒤 다시


def parse_remaining_req(value: str | None) -> RemainingReq | None:
    """``group=default; min=1800; sec=29`` 형식을 해석한다. 형식이 다르면 None."""
    if not value:
        return None
    fields: dict[str, str] = {}
    for part in value.split(";"):
        if "=" in part:
            key, val = part.split("=", 1)
            fields[key.strip().lower()] = val.strip()
    group = fields.get("group")
    if not group:
        return None

    def to_int(text: str | None) -> int | None:
        if text is None:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    return RemainingReq(group=group, minute=to_int(fields.get("min")), second=to_int(fields.get("sec")))


def parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` 헤더(초·밀리초 숫자 또는 HTTP 날짜)를 초로 해석한다. 없거나 형식이 다르면 None.

    공식 문서에는 이 헤더가 없다(429 는 "다음 초 경계까지 대기", 418 은 "응답의 차단 시간 정보 확인"). 서버가 주면
    그 값을 따르고, 없으면 호출자가 기본 대기(429: 1초, 418: blocked_cooldown)를 쓴다 (감사 MEDIUM-14).
    """
    if not value:
        return None
    text = value.strip()
    try:
        number = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    if number < 0:
        return None
    if number > 1000:  # 밀리초로 보이는 값
        number /= 1000.0
    return number


class SlidingWindowLimiter:
    """한 그룹의 슬라이딩 윈도우 카운터. ``acquire()`` 는 한도 안에 들어올 때까지 기다린다."""

    def __init__(
        self,
        rule: RateLimitRule,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
        safety_margin: int = 1,
    ) -> None:
        self.rule = rule
        # 서버와 우리의 1초 경계가 어긋날 수 있으므로 기본적으로 한도보다 1회 적게 보낸다.
        self.limit = (
            rule.max_requests - safety_margin
            if rule.max_requests > safety_margin
            else rule.max_requests
        )
        self._clock = clock
        self._sleep = sleep
        self._events: deque[float] = deque()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    def _evict(self, now: float) -> None:
        while self._events and now - self._events[0] >= self.rule.per_seconds:
            self._events.popleft()

    @property
    def used(self) -> int:
        """현재 윈도우 안에서 사용한 요청 수."""
        self._evict(self._clock())
        return len(self._events)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                self._evict(now)
                if now < self._blocked_until:
                    wait = self._blocked_until - now
                elif len(self._events) < self.limit:
                    self._events.append(now)
                    return
                else:
                    wait = self.rule.per_seconds - (now - self._events[0])
                await self._sleep(max(wait, 0.001))

    def block_for(self, seconds: float) -> None:
        """서버가 한도 소진/초과를 알려왔을 때 일정 시간 요청을 막는다."""
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)


class RateLimiter:
    """그룹별 ``SlidingWindowLimiter`` 를 관리한다. 클라이언트 하나가 하나를 공유한다."""

    def __init__(
        self,
        *,
        rules: Mapping[str, RateLimitRule] | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
        safety_margin: int = 1,
        exhausted_cooldown: float = 1.0,
    ) -> None:
        self._rules = dict(rules or RATE_LIMIT_RULES)
        self._clock = clock
        self._sleep = sleep
        self._safety_margin = safety_margin
        self.exhausted_cooldown = exhausted_cooldown
        self._limiters: dict[str, SlidingWindowLimiter] = {}

    def limiter(self, group: str) -> SlidingWindowLimiter:
        limiter = self._limiters.get(group)
        if limiter is None:
            rule = self._rules.get(group)
            if rule is None:
                # 문서에 없는 그룹 이름이 헤더로 오면 가장 보수적인(초당 10회) 규칙을 적용한다.
                rule = RateLimitRule(group, 10)
            limiter = SlidingWindowLimiter(
                rule, clock=self._clock, sleep=self._sleep, safety_margin=self._safety_margin
            )
            self._limiters[group] = limiter
        return limiter

    async def acquire(self, group: str) -> None:
        await self.limiter(group).acquire()

    def update_from_headers(self, headers: Mapping[str, str]) -> RemainingReq | None:
        """응답 헤더의 잔여 요청 수를 반영한다. ``sec=0`` 이면 그 그룹을 잠시 막는다."""
        remaining = parse_remaining_req(headers.get(REMAINING_REQ_HEADER))
        if remaining is not None and remaining.second is not None and remaining.second <= 0:
            self.limiter(remaining.group).block_for(self.exhausted_cooldown)
        return remaining

    def penalize(self, group: str, seconds: float) -> None:
        self.limiter(group).block_for(seconds)
