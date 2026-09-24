"""리스크 상태 저장소 — 재시작해도 당일 시작 자산·잠금·긴급 정지가 유지되게 한다 (감사 HIGH-3).

- ``RiskStateStore`` 프로토콜: ``save(data)`` / ``load() -> data | None``.
- ``MemoryRiskStateStore``: 프로세스 안에서만 유지. ``default_risk_store()`` 는 프로세스 전역 하나를 돌려주므로
  같은 프로세스에서 RiskManager 를 다시 만들어도(설정 다시 읽기 등) 상태가 이어진다.
- ``RepositoryRiskStateStore``: ``Repository.save_risk_state / load_risk_state`` 로 DB(``risk_state`` 테이블)에 남긴다.
  엔진(``run``)이 쓰며, 프로세스를 껐다 켜도 복구된다.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)


@runtime_checkable
class RiskStateStore(Protocol):
    def save(self, data: dict[str, Any]) -> None: ...

    def load(self) -> dict[str, Any] | None: ...


class MemoryRiskStateStore:
    def __init__(self) -> None:
        self._data: dict[str, Any] | None = None

    def save(self, data: dict[str, Any]) -> None:
        self._data = dict(data)

    def load(self) -> dict[str, Any] | None:
        return dict(self._data) if self._data is not None else None

    def clear(self) -> None:
        self._data = None


class RepositoryRiskStateStore:
    """``save_risk_state`` / ``load_risk_state`` 를 가진 저장소(Repository)에 위임한다. 실패해도 매매를 막지 않는다."""

    def __init__(self, repo: Any) -> None:
        self.repo = repo

    def save(self, data: dict[str, Any]) -> None:
        try:
            self.repo.save_risk_state(data)
        except Exception as exc:  # noqa: BLE001 - 상태 저장 실패가 리스크 판단을 멈추지 않게
            log.warning("리스크 상태 저장 실패: %s", exc)

    def load(self) -> dict[str, Any] | None:
        try:
            return self.repo.load_risk_state()
        except Exception as exc:  # noqa: BLE001
            log.warning("리스크 상태 복구 실패: %s", exc)
            return None


_DEFAULT_STORE = MemoryRiskStateStore()


def default_risk_store() -> MemoryRiskStateStore:
    """프로세스 전역 메모리 저장소 (RiskManager 의 기본값)."""
    return _DEFAULT_STORE
