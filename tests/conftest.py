"""공용 픽스처.

- ``make_settings``: ``.env`` 를 읽지 않는 Settings 팩토리 (테스트 격리).
- ``FakeClock`` / ``client_factory``: 실제 네트워크·실제 대기 없이 클라이언트를 검증한다.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.config.settings import Settings
from app.exchange.rate_limiter import RateLimiter
from app.exchange.upbit_client import UpbitClient

TEST_ACCESS_KEY = "test-access-key-0123456789"
TEST_SECRET_KEY = "test-secret-key-abcdefghijklmnopqrstuvwxyz"


@pytest.fixture(autouse=True)
def _reset_default_risk_store() -> None:
    """RiskManager 의 프로세스 전역 메모리 저장소가 테스트 사이에 새지 않게 비운다."""
    from app.risk.state_store import default_risk_store

    default_risk_store().clear()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """개발자 PC 의 실제 환경변수가 테스트에 스며들지 않도록 제거한다."""
    for name in (
        "UPBIT_ACCESS_KEY",
        "UPBIT_SECRET_KEY",
        "TRADING_MODE",
        "LIVE_TRADING_ENABLED",
        "MARKETS",
        "UPBIT_API_URL",
        "LOG_LEVEL",
        "LOG_DIR",
        "DATABASE_URL",
        "HTTP_TIMEOUT_SECONDS",
        "HTTP_MAX_RETRIES",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    def _make(**overrides: Any) -> Settings:
        return Settings(_env_file=None, **overrides)

    return _make


class FakeClock:
    """단조 증가 시계. ``advance()`` 로만 흐른다."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ClientHarness:
    """MockTransport 로 감싼 클라이언트와, 기록된 요청·대기 시간."""

    def __init__(self, client: UpbitClient, clock: FakeClock) -> None:
        self.client = client
        self.clock = clock
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []


@pytest.fixture
def client_factory() -> Callable[..., ClientHarness]:
    """``handler(request) -> httpx.Response`` 를 받아 네트워크 없는 클라이언트를 만든다."""

    def _make(
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        with_auth: bool = False,
        max_retries: int = 3,
    ) -> ClientHarness:
        clock = FakeClock()
        harness: ClientHarness | None = None

        async def fake_sleep(seconds: float) -> None:
            assert harness is not None
            harness.sleeps.append(seconds)
            clock.advance(seconds)

        def recording_handler(request: httpx.Request) -> httpx.Response:
            assert harness is not None
            harness.requests.append(request)
            return handler(request)

        limiter = RateLimiter(clock=clock, sleep=fake_sleep)
        client = UpbitClient(
            access_key=TEST_ACCESS_KEY if with_auth else None,
            secret_key=TEST_SECRET_KEY if with_auth else None,
            max_retries=max_retries,
            rate_limiter=limiter,
            transport=httpx.MockTransport(recording_handler),
            sleep=fake_sleep,
        )
        harness = ClientHarness(client, clock)
        return harness

    return _make


def json_response(data: Any, status_code: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status_code, json=data, headers=headers or {})
