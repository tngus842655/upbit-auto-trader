"""알림 관리자 — 이벤트 필터·중복 억제·백그라운드 큐·재시도.

원칙: 알림은 매매를 절대 늦추거나 멈추게 하지 않는다.
- ``emit`` 은 동기·비차단(큐에 넣고 바로 돌아온다). 큐가 차면 가장 오래된 것을 버린다.
- 전송은 워커 태스크가 채널별로 순서대로 하고, 실패는 재시도(기본 2회, 1초·3초) 후 ``on_failure`` 콜백으로만 알린다.
  예외는 절대 밖으로 나가지 않는다.
- 같은 ``key`` 의 이벤트(예: 같은 마켓의 캔들 조회 실패)는 쿨다운 동안 한 번만 보낸다 — 오류 폭주 방지.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from app.notify.base import (
    ALL_KINDS,
    BURST_KINDS,
    PRIORITY_KINDS,
    EventKind,
    NotificationEvent,
    Notifier,
    NotifyError,
)

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
FailureHook = Callable[[NotificationEvent, str], None]


@dataclass
class NotifyStats:
    queued: int = 0
    sent: int = 0
    failed: int = 0
    dropped: int = 0  # 큐가 가득 차서 버림
    suppressed: int = 0  # 쿨다운·비활성 종류로 걸러짐


class NotificationManager:
    def __init__(
        self,
        notifiers: Sequence[Notifier],
        *,
        enabled: Iterable[EventKind] | None = None,
        error_cooldown_seconds: float = 300.0,
        max_queue: int = 500,
        retries: int = 2,
        retry_delays: Sequence[float] = (1.0, 3.0),
        on_failure: FailureHook | None = None,
        clock: Clock | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.notifiers = list(notifiers)
        self.enabled: frozenset[EventKind] = frozenset(ALL_KINDS if enabled is None else enabled)
        self.error_cooldown_seconds = error_cooldown_seconds
        self.retries = retries
        self.retry_delays = list(retry_delays)
        self.on_failure = on_failure
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.stats = NotifyStats()
        self._queue: asyncio.Queue[NotificationEvent] = asyncio.Queue(maxsize=max_queue)
        self._worker: asyncio.Task[None] | None = None
        self._last_keyed: dict[str, datetime] = {}
        self.history: list[NotificationEvent] = []  # 최근 발송 이벤트 (상태 표시용, 최대 50)

    # ------------------------------------------------------------------
    @property
    def channels(self) -> list[str]:
        return [n.name for n in self.notifiers]

    def is_enabled(self, kind: EventKind) -> bool:
        return kind in self.enabled and bool(self.notifiers)

    def emit(self, event: NotificationEvent) -> bool:
        """이벤트를 큐에 넣는다. 걸러지거나 버려지면 False. 예외를 내지 않는다."""
        if not self.is_enabled(event.kind):
            self.stats.suppressed += 1
            return False
        key = event.key
        if key is None and event.kind in BURST_KINDS:
            # 거부·오류는 장애 중 1초마다 반복될 수 있다 → 마켓·종류·사유 단위로 자동 쿨다운 (감사 MEDIUM-11)
            key = event.auto_key()
        if key:
            last = self._last_keyed.get(key)
            now = self.clock()
            if last is not None and (now - last).total_seconds() < self.error_cooldown_seconds:
                self.stats.suppressed += 1
                return False
            self._last_keyed[key] = now
        if self._queue.full():
            self._evict_one()
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover - 위에서 자리를 비웠으므로 거의 없음
            self.stats.dropped += 1
            return False
        self.stats.queued += 1
        return True

    def _evict_one(self) -> None:
        """큐가 찼을 때 자리 하나를 비운다 — 낮은 우선순위(거부·오류·정보) 중 가장 오래된 것, 없으면 가장 오래된 것."""
        items: list[NotificationEvent] = []
        with contextlib.suppress(asyncio.QueueEmpty):
            while True:
                items.append(self._queue.get_nowait())
                self._queue.task_done()
        victim = next((i for i, e in enumerate(items) if e.kind not in PRIORITY_KINDS), 0 if items else None)
        if victim is not None:
            dropped = items.pop(victim)
            self.stats.dropped += 1
            log.warning("알림 큐 가득 참 → 버림: [%s] %s", dropped.label, dropped.title)
        for item in items:
            self._queue.put_nowait(item)

    async def start(self) -> None:
        if self._worker is None and self.notifiers:
            self._worker = asyncio.create_task(self._run(), name="notify-worker")

    async def close(self, drain_seconds: float = 10.0) -> None:
        """남은 큐를 ``drain_seconds`` 안에서 비우고 워커·채널을 닫는다."""
        if self._worker is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._queue.join(), drain_seconds)
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker
            self._worker = None
        for notifier in self.notifiers:
            with contextlib.suppress(Exception):
                await notifier.aclose()

    async def flush(self, drain_seconds: float = 10.0) -> None:
        """큐가 빌 때까지 기다린다 (테스트·종료 직전용)."""
        if self._worker is None:
            while not self._queue.empty():
                event = self._queue.get_nowait()
                try:
                    await self._deliver(event)
                finally:
                    self._queue.task_done()
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._queue.join(), drain_seconds)

    async def send_now(self, event: NotificationEvent) -> dict[str, str | None]:
        """큐를 거치지 않고 즉시 채널별로 보낸다(알림 테스트용). 채널 → 오류 메시지(None 이면 성공)."""
        results: dict[str, str | None] = {}
        for notifier in self.notifiers:
            results[notifier.name] = await self._send_with_retry(notifier, event)
        return results

    def stats_dict(self) -> dict[str, int | list[str]]:
        return {**asdict(self.stats), "channels": self.channels}

    # ------------------------------------------------------------------
    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._deliver(event)
            except Exception as exc:  # noqa: BLE001 - 워커는 어떤 경우에도 죽지 않는다
                log.warning("알림 전송 중 예외: %s", exc)
            finally:
                self._queue.task_done()

    async def _deliver(self, event: NotificationEvent) -> None:
        for notifier in self.notifiers:
            error = await self._send_with_retry(notifier, event)
            if error is None:
                self.stats.sent += 1
            else:
                self.stats.failed += 1
                log.warning("알림 실패 [%s] %s: %s", notifier.name, event.label, error)
                if self.on_failure is not None:
                    with contextlib.suppress(Exception):
                        self.on_failure(event, f"{notifier.name}: {error}")
        self.history.append(event)
        del self.history[:-50]

    async def _send_with_retry(self, notifier: Notifier, event: NotificationEvent) -> str | None:
        attempts = self.retries + 1
        for attempt in range(attempts):
            try:
                await notifier.send(event)
                return None
            except NotifyError as exc:
                if not exc.retryable or attempt == attempts - 1:
                    return str(exc)
            except Exception as exc:  # noqa: BLE001 - 채널 구현 오류도 매매를 막지 않는다
                return f"{type(exc).__name__}: {exc}"
            delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)] if self.retry_delays else 0.0
            if delay > 0:
                await self.sleep(delay)
        return "재시도 초과"  # pragma: no cover
