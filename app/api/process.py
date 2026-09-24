"""엔진 프로세스 관리 — 대시보드의 Start/Stop 이 엔진을 **별도 프로세스** 로 띄우고 내린다.

엔진은 웹 서버와 독립적으로 살아 있다(웹 서버가 죽어도 계속 돈다). 정지는 DB 명령 큐(stop)로 우아하게 요청하고,
응답이 없을 때만 PID 로 강제 종료한다. 실행 로그는 ``logs/engine-{mode}.log`` 에 남는다.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.config.settings import PROJECT_ROOT, Settings
from app.database.models import from_db_time
from app.database.repository import Repository

log = logging.getLogger(__name__)

HEARTBEAT_STALE_SECONDS = 45.0


def engine_is_alive(repo: Repository, now: datetime | None = None) -> tuple[bool, str]:
    """하트비트로 엔진 생존을 판단한다. (alive, status)."""
    status = repo.read_engine_status()
    if status is None:
        return False, "NONE"
    now = now or datetime.now(UTC)
    updated = from_db_time(status.updated_at)
    fresh = updated is not None and (now - updated).total_seconds() <= HEARTBEAT_STALE_SECONDS
    if status.status == "STOPPED" or not fresh:
        return False, status.status if status.status == "STOPPED" else "STALE"
    return True, status.status


def start_engine(settings: Settings, mode: str, *, confirm_live: str = "", python: str | None = None) -> int:
    """``python -m app.main run`` 을 분리된 프로세스로 띄우고 PID 를 돌려준다."""
    log_dir = Path(settings.log_dir)
    if not log_dir.is_absolute():
        log_dir = PROJECT_ROOT / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    out = open(log_dir / f"engine-{mode}.log", "ab")  # noqa: SIM115 - 자식 프로세스가 쓰는 핸들
    cmd = [python or sys.executable, "-m", "app.main", "run"]
    if mode == "live":
        cmd += ["--confirm-live", confirm_live]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["TRADING_MODE"] = "LIVE" if mode == "live" else "PAPER"
    kwargs: dict = {"cwd": str(PROJECT_ROOT), "stdout": out, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                    "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    log.info("엔진 프로세스 시작: pid=%s mode=%s", proc.pid, mode)
    return proc.pid


def kill_engine(pid: int) -> bool:
    """마지막 수단: PID 로 강제 종료. 성공하면 True."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
        else:
            os.kill(pid, 15)
        return True
    except OSError as exc:
        log.warning("강제 종료 실패 pid=%s: %s", pid, exc)
        return False
