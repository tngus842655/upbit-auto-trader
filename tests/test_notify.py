"""알림(Phase 9) 테스트 — 채널·관리자·설정·엔진 연동. 실제 네트워크 없음(httpx MockTransport)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.exceptions import MarketDataError
from app.notify import (
    DiscordNotifier,
    EventKind,
    NotificationEvent,
    NotificationManager,
    NotifyError,
    TelegramNotifier,
    build_notification_manager,
    discover_chats,
    format_event,
    get_me,
    parse_event_kinds,
)
from app.risk import RiskConfig
from tests.test_engine import MARKET, T0, Harness
from tests.test_strategy_data import make_candle


class FakeNotifier:
    def __init__(self, name: str = "fake", *, fail_times: int = 0, retryable: bool = True) -> None:
        self.name = name
        self.sent: list[NotificationEvent] = []
        self.fail_times = fail_times
        self.retryable = retryable
        self.closed = False

    async def send(self, event: NotificationEvent) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise NotifyError(f"{self.name} 실패", retryable=self.retryable)
        self.sent.append(event)

    async def aclose(self) -> None:
        self.closed = True


def make_event(kind: EventKind = EventKind.BUY, title: str = "KRW-BTC", *, key: str | None = None,
               message: str = "시장가 매수 100,000원") -> NotificationEvent:
    return NotificationEvent(kind=kind, title=title, message=message, mode="paper",
                             time=datetime(2026, 9, 24, 6, 0, tzinfo=UTC), key=key)


def test_format_event_and_kind_parsing() -> None:
    text = format_event(make_event())
    assert text.splitlines() == ["🟢 [PAPER] 매수 — KRW-BTC", "시장가 매수 100,000원", "09-24 15:00:00 KST"]
    assert len(format_event(make_event(message="x" * 5000), max_length=100)) == 100
    assert parse_event_kinds("all") == list(EventKind)
    assert parse_event_kinds("off") == [] and parse_event_kinds("") == [] and parse_event_kinds(None) == list(EventKind)
    assert parse_event_kinds("buy, STOP_LOSS,buy") == [EventKind.BUY, EventKind.STOP_LOSS]
    with pytest.raises(ValueError, match="알 수 없는"):
        parse_event_kinds("buy,bogus")
    assert make_event().to_dict()["label"] == "매수"


async def test_manager_filters_dedupes_and_delivers() -> None:
    fake = FakeNotifier()
    now = datetime(2026, 9, 24, tzinfo=UTC)
    m = NotificationManager([fake], enabled=[EventKind.BUY, EventKind.API_ERROR], error_cooldown_seconds=60,
                            clock=lambda: now)
    assert m.emit(make_event()) is True
    assert m.emit(make_event(EventKind.SELL)) is False  # 비활성 종류
    assert m.emit(make_event(EventKind.API_ERROR, key="candle:KRW-BTC")) is True
    assert m.emit(make_event(EventKind.API_ERROR, key="candle:KRW-BTC")) is False  # 쿨다운 안 반복
    now += timedelta(seconds=61)
    assert m.emit(make_event(EventKind.API_ERROR, key="candle:KRW-BTC")) is True
    await m.start()
    await m.flush()
    await m.close()
    assert [e.kind for e in fake.sent] == [EventKind.BUY, EventKind.API_ERROR, EventKind.API_ERROR]
    assert m.stats.sent == 3 and m.stats.suppressed == 2 and m.stats.queued == 3 and fake.closed
    assert m.stats_dict()["channels"] == ["fake"] and len(m.history) == 3


async def test_manager_without_channels_is_noop() -> None:
    m = NotificationManager([])
    assert m.emit(make_event()) is False and m.is_enabled(EventKind.BUY) is False
    await m.start()
    await m.close()


async def test_manager_queue_overflow_drops_oldest() -> None:
    fake = FakeNotifier()
    m = NotificationManager([fake], max_queue=2)
    for i in range(3):
        m.emit(make_event(title=f"e{i}"))
    assert m.stats.dropped == 1
    await m.flush()  # 워커 없이도 flush 가 직접 전송한다
    assert [e.title for e in fake.sent] == ["e1", "e2"]


async def test_manager_retries_and_reports_failures() -> None:
    flaky = FakeNotifier("flaky", fail_times=2)  # 두 번 실패 후 성공 (재시도 2회 안)
    broken = FakeNotifier("broken", fail_times=99, retryable=False)  # 재시도 없이 실패
    failures: list[str] = []
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    m = NotificationManager([flaky, broken], on_failure=lambda _e, err: failures.append(err), sleep=fake_sleep)
    m.emit(make_event())
    await m.flush()
    assert len(flaky.sent) == 1 and broken.sent == []
    assert slept == [1.0, 3.0]
    assert failures == ["broken: broken 실패"]
    assert m.stats.sent == 1 and m.stats.failed == 1
    results = await m.send_now(make_event())
    assert results == {"flaky": None, "broken": "broken 실패"}


async def test_telegram_payload_and_token_scrub() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"ok": True, "result": {}})
        if len(calls) == 2:
            return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        return httpx.Response(502, text="bad gateway")

    n = TelegramNotifier("123:SECRET", "777", transport=httpx.MockTransport(handler))
    await n.send(make_event())
    assert calls[0].url.path == "/bot123:SECRET/sendMessage"
    body = json.loads(calls[0].content)
    assert body["chat_id"] == "777" and body["text"].startswith("🟢 [PAPER] 매수 — KRW-BTC")
    with pytest.raises(NotifyError) as info:
        await n.send(make_event())
    assert "SECRET" not in str(info.value) and info.value.retryable is False
    with pytest.raises(NotifyError) as info:
        await n.send(make_event())
    assert info.value.retryable is True
    with pytest.raises(ValueError):
        TelegramNotifier("", "1")
    await n.aclose()


async def test_discover_chats_lists_channels_groups_and_private() -> None:
    updates = {"ok": True, "result": [
        {"update_id": 1, "my_chat_member": {"chat": {"id": -1001234567890, "type": "channel", "title": "매매 알림"}}},
        {"update_id": 2, "channel_post": {"chat": {"id": -1001234567890, "type": "channel", "title": "매매 알림"},
                                          "text": "봇 추가 테스트"}},
        {"update_id": 3, "message": {"chat": {"id": 777, "type": "private", "first_name": "수현", "last_name": "N"},
                                     "text": "/start"}},
        {"update_id": 4, "edited_message": {"chat": {"id": -555, "type": "group", "title": "그룹"}}},
        {"update_id": 5, "poll": {"id": "x"}},  # chat 없는 업데이트는 무시
    ]}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=updates)

    chats = await discover_chats("123:SECRET", transport=httpx.MockTransport(handler))
    assert seen[0].url.path == "/bot123:SECRET/getUpdates"
    assert [c["id"] for c in chats] == [-1001234567890, 777, -555]
    assert chats[0] == {"id": -1001234567890, "type": "channel", "title": "매매 알림", "last_text": "봇 추가 테스트"}
    assert chats[1]["title"] == "수현 N" and chats[1]["last_text"] == "/start"

    for status, text in ((401, "Unauthorized"), (409, "webhook")):
        transport = httpx.MockTransport(lambda _r, s=status, t=text: httpx.Response(s, text=t))
        with pytest.raises(NotifyError) as info:
            await discover_chats("123:SECRET", transport=transport)
        assert "SECRET" not in str(info.value) and info.value.retryable is False
    with pytest.raises(NotifyError):
        await discover_chats("")


async def test_get_me_returns_bot_identity_and_hides_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bot123:SECRET/getMe"
        return httpx.Response(200, json={"ok": True, "result": {"id": 42, "is_bot": True, "first_name": "알림봇",
                                                                 "username": "nsh_upbit_alert_bot"}})

    me = await get_me("123:SECRET", transport=httpx.MockTransport(handler))
    assert me == {"id": 42, "username": "nsh_upbit_alert_bot", "name": "알림봇"}
    with pytest.raises(NotifyError) as info:
        await get_me("123:SECRET", transport=httpx.MockTransport(lambda _r: httpx.Response(401, text="Unauthorized")))
    assert "SECRET" not in str(info.value) and info.value.retryable is False


async def test_discord_payload_and_errors() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(204) if len(calls) == 1 else httpx.Response(500, text="boom")

    n = DiscordNotifier("https://discord.com/api/webhooks/1/abc", transport=httpx.MockTransport(handler))
    await n.send(make_event(EventKind.BOT_STOP, "정상 종료"))
    assert json.loads(calls[0].content)["content"].startswith("⏹️ [PAPER] 봇 중지 — 정상 종료")
    with pytest.raises(NotifyError) as info:
        await n.send(make_event())
    assert info.value.retryable is True and "abc" not in str(info.value)
    with pytest.raises(ValueError):
        DiscordNotifier("http://insecure.example")
    await n.aclose()


def test_settings_notify_fields(make_settings) -> None:
    s = make_settings()
    assert s.notify_channels == ["log"] and s.notify_event_kinds() == list(EventKind)
    s = make_settings(notify_events="buy,sell", telegram_bot_token="1:x", telegram_chat_id="5",
                      discord_webhook_url="https://discord.com/api/webhooks/1/a", notify_log=False)
    assert s.notify_channels == ["telegram", "discord"]
    assert s.notify_event_kinds() == [EventKind.BUY, EventKind.SELL]
    assert "1:x" not in json.dumps(s.summary(), ensure_ascii=False, default=str)
    m = build_notification_manager(s)
    assert m is not None and m.channels == ["telegram", "discord"] and m.enabled == {EventKind.BUY, EventKind.SELL}
    assert build_notification_manager(make_settings(notify_log=False)) is None
    assert build_notification_manager(make_settings()).channels == ["log"]
    assert make_settings(notify_events="off").notify_event_kinds() == []
    with pytest.raises(Exception, match="알 수 없는"):
        make_settings(notify_events="bogus")


async def test_engine_emits_trade_events(make_settings) -> None:
    h = Harness(make_settings, actions={T0 + timedelta(hours=10): "BUY"})
    fake = FakeNotifier()
    h.engine.notifier = NotificationManager([fake])
    await h.engine.warmup()
    h.now = T0 + timedelta(hours=11, seconds=5)
    h.feed_prices(ask=110.0, bid=109.0)
    await h.engine.process_closed_candles(h.now)
    await h.engine.notifier.flush()
    assert [e.kind for e in fake.sent] == [EventKind.BUY, EventKind.ORDER_FILLED]
    assert fake.sent[0].title == MARKET and "시장가 매수" in fake.sent[0].message
    assert fake.sent[1].title.startswith(MARKET) and "@ 110" in fake.sent[1].message and "현금" in fake.sent[1].message
    assert all(e.mode == "paper" and e.time == h.now for e in fake.sent)

    # 손절 → 손절 이벤트 + 체결(손익 포함)
    h.now += timedelta(seconds=2)
    h.feed_prices(ask=103.0, bid=102.0)
    orders = await h.engine.check_exits(h.now, force=True)
    assert len(orders) == 1 and orders[0].is_filled
    await h.engine.notifier.flush()
    kinds = [e.kind for e in fake.sent]
    assert kinds == [EventKind.BUY, EventKind.ORDER_FILLED, EventKind.STOP_LOSS, EventKind.ORDER_FILLED]
    assert "기준가" in fake.sent[2].message and "손익 -" in fake.sent[3].message


async def test_engine_emits_daily_loss_limit(make_settings) -> None:
    h = Harness(make_settings, actions={T0 + timedelta(hours=10): "BUY"})
    h.engine.risk.config = RiskConfig.unrestricted(position_fraction=0.5, daily_loss_limit_pct=0.02)
    fake = FakeNotifier()
    h.engine.notifier = NotificationManager([fake], enabled=[EventKind.DAILY_LOSS_LIMIT, EventKind.RISK_HALT])
    await h.engine.warmup()
    h.now = T0 + timedelta(hours=11, seconds=5)
    h.feed_prices(ask=110.0, bid=109.0)
    await h.engine.process_closed_candles(h.now)
    assert h.portfolio.has_position(MARKET)
    h.now += timedelta(seconds=2)
    h.feed_prices(ask=103.0, bid=102.0)  # 평가액 -3% → 일일 손실 잠금
    await h.engine.check_exits(h.now, force=True)
    h.repo.enqueue_command("halt", {"reason": "점검"})
    h.engine.poll_commands()
    await h.engine.notifier.flush()
    assert [e.kind for e in fake.sent] == [EventKind.DAILY_LOSS_LIMIT, EventKind.RISK_HALT]
    assert "점검" in fake.sent[1].message


async def test_engine_api_error_event_is_deduped(make_settings) -> None:
    h = Harness(make_settings)
    fake = FakeNotifier()
    h.engine.notifier = NotificationManager([fake], error_cooldown_seconds=300, clock=lambda: h.now)
    await h.engine.warmup()

    async def boom(*_args, **_kwargs):
        raise MarketDataError("네트워크 끊김")

    h.client.get_candles = boom  # type: ignore[method-assign]
    h.now = T0 + timedelta(hours=11, seconds=5)
    await h.engine.process_closed_candles(h.now)
    h.now += timedelta(seconds=60)
    await h.engine.process_closed_candles(h.now)
    await h.engine.notifier.flush()
    assert [e.kind for e in fake.sent] == [EventKind.API_ERROR]
    assert fake.sent[0].key == f"candle:{MARKET}" and "네트워크 끊김" in fake.sent[0].message
    assert h.engine.notifier.stats.suppressed == 1


async def test_engine_run_sends_start_and_stop(make_settings) -> None:
    h = Harness(make_settings)
    h.engine.clock = lambda: datetime.now(UTC)
    h.engine.command_poll_interval = 0.05
    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    h.client.candles = [make_candle(base - timedelta(hours=i), 100.0) for i in range(1, 12)]
    fake = FakeNotifier()
    h.engine.notifier = NotificationManager([fake])

    async def stop_soon() -> None:
        await asyncio.sleep(0.3)
        h.repo.enqueue_command("stop")

    stopper = asyncio.create_task(stop_soon())
    await h.engine.run(duration_seconds=5)
    await stopper
    kinds = [e.kind for e in fake.sent]
    assert kinds[0] == EventKind.BOT_START and kinds[-1] == EventKind.BOT_STOP
    assert "정상 종료" in fake.sent[-1].title and "현금" in fake.sent[-1].message
    assert h.engine.stats.extra["notifications"]["sent"] == len(fake.sent)
    assert fake.closed
