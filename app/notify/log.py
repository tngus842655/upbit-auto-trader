"""로그 채널 — 알림 본문을 로그(콘솔·파일)에 남긴다.

채널 토큰이 없어도 알림 파이프라인이 도는지 확인할 수 있고, 실제 채널이 있어도 발송 기록이 로그에 함께 남는다.
"""

from __future__ import annotations

import logging

from app.notify.base import NotificationEvent, format_event

log = logging.getLogger("app.notify")


class LogNotifier:
    name = "log"

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._log = logger or log

    async def send(self, event: NotificationEvent) -> None:
        self._log.info("알림 %s", format_event(event).replace("\n", " | "))

    async def aclose(self) -> None:
        return None
