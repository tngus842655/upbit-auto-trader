"""Discord 채널 — 채널 웹훅(https://discord.com/developers/docs/resources/webhook#execute-webhook).

준비: Discord 채널 설정 > 연동 > 웹훅 만들기 → URL 복사. URL 자체가 비밀이므로 오류 메시지에서 걸러낸다.
"""

from __future__ import annotations

import httpx

from app.notify.base import NotificationEvent, NotifyError, format_event

MAX_LENGTH = 2000  # Discord content 길이 한도
USERNAME = "upbit-auto-trader"


class DiscordNotifier:
    name = "discord"

    def __init__(
        self, webhook_url: str, *, timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if not webhook_url or not webhook_url.startswith("https://"):
            raise ValueError("Discord 웹훅 URL 은 https:// 로 시작해야 합니다")
        self._url = webhook_url
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    def _scrub(self, text: str) -> str:
        return text.replace(self._url, "<webhook>")

    async def send(self, event: NotificationEvent) -> None:
        payload = {"content": format_event(event, max_length=MAX_LENGTH), "username": USERNAME}
        try:
            response = await self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise NotifyError(self._scrub(f"네트워크 오류 {type(exc).__name__}: {exc}")) from None
        if response.status_code == 429:
            raise NotifyError(self._scrub(f"요청 제한(429): {response.text[:200]}"), retryable=True)
        if response.status_code >= 400:
            retryable = response.status_code >= 500
            raise NotifyError(self._scrub(f"HTTP {response.status_code}: {response.text[:200]}"), retryable=retryable)

    async def aclose(self) -> None:
        await self._client.aclose()
