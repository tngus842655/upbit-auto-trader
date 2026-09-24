"""알림 (Phase 9) — 이벤트 → 채널(Telegram / Discord / 로그).

새 채널 추가: ``Notifier`` 프로토콜(``name``, ``send``, ``aclose``)을 구현하고
``build_notification_manager`` 에 한 줄 추가하면 된다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.notify.base import (
    ALL_KINDS,
    EMOJI,
    LABELS,
    EventKind,
    NotificationEvent,
    Notifier,
    NotifyError,
    format_event,
    parse_event_kinds,
)
from app.notify.discord import DiscordNotifier
from app.notify.log import LogNotifier
from app.notify.manager import FailureHook, NotificationManager, NotifyStats
from app.notify.telegram import TelegramNotifier, discover_chats, get_me

if TYPE_CHECKING:
    from app.config.settings import Settings

__all__ = [
    "ALL_KINDS", "EMOJI", "LABELS", "DiscordNotifier", "EventKind", "FailureHook", "LogNotifier",
    "NotificationEvent", "NotificationManager", "Notifier", "NotifyError", "NotifyStats", "TelegramNotifier",
    "build_notification_manager", "discover_chats", "format_event", "get_me", "parse_event_kinds",
]


def build_notification_manager(
    settings: Settings, *, on_failure: FailureHook | None = None, include_log: bool | None = None
) -> NotificationManager | None:
    """.env 설정으로 채널을 조립한다. 채널이 하나도 없으면 None (엔진은 알림 없이 돈다)."""
    notifiers: list[Notifier] = []
    if settings.telegram_bot_token and settings.telegram_chat_id:
        notifiers.append(
            TelegramNotifier(
                settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id,
                timeout=settings.http_timeout_seconds,
            )
        )
    if settings.discord_webhook_url:
        notifiers.append(
            DiscordNotifier(settings.discord_webhook_url.get_secret_value(), timeout=settings.http_timeout_seconds)
        )
    if settings.notify_log if include_log is None else include_log:
        notifiers.append(LogNotifier())
    if not notifiers:
        return None
    return NotificationManager(
        notifiers,
        enabled=settings.notify_event_kinds(),
        error_cooldown_seconds=settings.notify_error_cooldown_seconds,
        on_failure=on_failure,
    )
