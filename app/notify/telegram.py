"""Telegram 채널 — Bot API ``sendMessage`` (https://core.telegram.org/bots/api#sendmessage).

준비: @BotFather 로 봇을 만들어 토큰을 받고, 봇에게 아무 메시지나 보낸 뒤 ``getUpdates`` 로 chat_id 를 확인한다.
토큰은 URL 경로에 들어가므로 오류 메시지·로그에 URL 이 새지 않도록 여기서 직접 걸러낸다.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.notify.base import NotificationEvent, NotifyError, format_event

TELEGRAM_API_URL = "https://api.telegram.org"
MAX_LENGTH = 4096  # Telegram 메시지 길이 한도


class TelegramNotifier:
    name = "telegram"

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        timeout: float = 10.0,
        api_url: str = TELEGRAM_API_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token or not chat_id:
            raise ValueError("Telegram 토큰과 chat_id 가 모두 필요합니다")
        self._token = token
        self.chat_id = str(chat_id)
        self._client = httpx.AsyncClient(base_url=api_url, timeout=timeout, transport=transport)

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***")

    async def send(self, event: NotificationEvent) -> None:
        payload = {
            "chat_id": self.chat_id,
            "text": format_event(event, max_length=MAX_LENGTH),
            "disable_web_page_preview": True,
        }
        try:
            response = await self._client.post(f"/bot{self._token}/sendMessage", json=payload)
        except httpx.HTTPError as exc:
            # 원인 예외를 연결하지 않는다 — httpx 예외 문자열에 토큰이 든 URL 이 포함될 수 있다
            raise NotifyError(self._scrub(f"네트워크 오류 {type(exc).__name__}: {exc}")) from None
        if response.status_code == 429:
            raise NotifyError(self._scrub(f"요청 제한(429): {response.text[:200]}"), retryable=True)
        if response.status_code != 200:
            retryable = response.status_code >= 500
            raise NotifyError(self._scrub(f"HTTP {response.status_code}: {response.text[:200]}"), retryable=retryable)
        try:
            body = response.json()
        except ValueError:
            raise NotifyError(self._scrub(f"응답 해석 실패: {response.text[:200]}")) from None
        if not isinstance(body, dict) or not body.get("ok"):
            description = body.get("description", "") if isinstance(body, dict) else ""
            raise NotifyError(self._scrub(f"Telegram 거부: {description or response.text[:200]}"), retryable=False)

    async def aclose(self) -> None:
        await self._client.aclose()


async def get_me(
    token: str,
    *,
    timeout_seconds: float = 10.0,
    api_url: str = TELEGRAM_API_URL,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """``getMe`` — 토큰이 가리키는 봇의 username·이름. 채널 관리자로 넣은 봇과 같은지 확인하는 용도."""
    if not token:
        raise NotifyError("TELEGRAM_BOT_TOKEN 이 없습니다", retryable=False)
    async with httpx.AsyncClient(base_url=api_url, timeout=timeout_seconds, transport=transport) as client:
        try:
            response = await client.get(f"/bot{token}/getMe")
        except httpx.HTTPError as exc:
            raise NotifyError(f"네트워크 오류 {type(exc).__name__}: {exc}".replace(token, "***")) from None
    if response.status_code == 401:
        raise NotifyError("토큰이 올바르지 않습니다 (401). @BotFather 가 준 토큰을 다시 확인하세요", retryable=False)
    body = response.json() if response.status_code == 200 else {}
    if not isinstance(body, dict) or not body.get("ok"):
        raise NotifyError(f"getMe 실패: HTTP {response.status_code}".replace(token, "***"), retryable=False)
    me = body["result"]
    return {"id": me.get("id"), "username": me.get("username"), "name": me.get("first_name")}


async def discover_chats(
    token: str,
    *,
    timeout_seconds: float = 10.0,
    api_url: str = TELEGRAM_API_URL,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    """봇이 최근에 본 대화(채널·그룹·개인)의 chat id 를 ``getUpdates`` 로 찾는다.

    채널이면 봇을 채널 관리자로 추가한 뒤(추가 자체가 업데이트로 남는다) 메시지를 하나 올리고 실행한다.
    개인 대화면 봇에게 /start 를 보낸다. 웹훅이 걸린 봇은 getUpdates 를 쓸 수 없다(409).
    """
    if not token:
        raise NotifyError("TELEGRAM_BOT_TOKEN 이 없습니다", retryable=False)
    scrub = lambda text: text.replace(token, "***")  # noqa: E731
    async with httpx.AsyncClient(base_url=api_url, timeout=timeout_seconds, transport=transport) as client:
        try:
            response = await client.get(f"/bot{token}/getUpdates", params={"limit": 100})
        except httpx.HTTPError as exc:
            raise NotifyError(scrub(f"네트워크 오류 {type(exc).__name__}: {exc}")) from None
    if response.status_code == 401:
        raise NotifyError("토큰이 올바르지 않습니다 (401). @BotFather 가 준 토큰을 다시 확인하세요", retryable=False)
    if response.status_code == 409:
        raise NotifyError("이 봇에는 웹훅이 설정돼 있어 getUpdates 를 쓸 수 없습니다 (409)", retryable=False)
    if response.status_code != 200:
        raise NotifyError(scrub(f"HTTP {response.status_code}: {response.text[:200]}"), retryable=False)
    body = response.json()
    if not isinstance(body, dict) or not body.get("ok"):
        raise NotifyError(scrub(f"Telegram 거부: {body.get('description', '') if isinstance(body, dict) else ''}"),
                          retryable=False)
    found: dict[int, dict[str, Any]] = {}
    for update in body.get("result", []):
        for field_name in ("channel_post", "edited_channel_post", "message", "edited_message", "my_chat_member"):
            item = update.get(field_name)
            chat = item.get("chat") if isinstance(item, dict) else None
            if not isinstance(chat, dict) or "id" not in chat:
                continue
            title = chat.get("title") or chat.get("username") or " ".join(
                part for part in (chat.get("first_name"), chat.get("last_name")) if part
            )
            entry = found.setdefault(int(chat["id"]), {"id": int(chat["id"]), "type": chat.get("type", "?"),
                                                        "title": title or "(이름 없음)", "last_text": ""})
            text = item.get("text") if isinstance(item, dict) else None
            if text:
                entry["last_text"] = str(text)[:60]
    return list(found.values())
