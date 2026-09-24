"""엔진 단일 인스턴스 잠금 (감사 HIGH-7) — 같은 모드의 엔진이 한 PC 에서 두 개 뜨지 않게 한다.

모드별 잠금 파일(``data/engine-{mode}.lock``)에 OS 파일 잠금을 건다. 프로세스가 죽으면 OS 가 잠금을 풀므로
오래된 PID 파일 때문에 시작이 막히는 일은 없다. 파일 내용(PID·시작 시각)은 안내용이다.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)


class InstanceLock:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fh: IO[str] | None = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> bool:
        """잠금을 시도한다. 다른 프로세스가 잡고 있으면 False (기다리지 않는다)."""
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - 잠금 동안 열어 둔다
        try:
            _lock(fh)
        except OSError:
            fh.close()
            return False
        try:
            fh.seek(0)
            fh.truncate()
            # 첫 바이트는 잠금 전용(Windows 는 잠긴 바이트를 다른 핸들이 읽지 못한다) — 안내 정보는 그 뒤에 쓴다
            fh.write(f"L{os.getpid()} {datetime.now(UTC).isoformat()}\n")
            fh.flush()
        except OSError as exc:  # 잠금은 잡았으니 내용 기록 실패는 경고만
            log.warning("잠금 파일 기록 실패: %s", exc)
        self._fh = fh
        return True

    def holder(self) -> str:
        """잠금을 잡은 프로세스 안내 (PID 시작시각). 읽을 수 없으면 빈 문자열."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                fh.seek(1)  # 잠긴 첫 바이트는 건너뛴다
                return fh.read().strip()
        except OSError:
            return ""

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            _unlock(fh)
        except OSError as exc:
            log.warning("잠금 해제 실패: %s", exc)
        finally:
            fh.close()

    def __enter__(self) -> InstanceLock:
        if not self.acquire():
            raise RuntimeError(f"이미 실행 중 ({self.holder()})")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _lock(fh: IO[str]) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh: IO[str]) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
