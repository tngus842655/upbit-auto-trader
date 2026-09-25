"""엔진 프로세스 관리 — 대시보드의 Start/Stop 이 엔진을 **별도 프로세스** 로 띄우고 내린다.

엔진은 웹 서버와 독립적으로 살아 있다(웹 서버가 죽어도 계속 돈다). 정지는 DB 명령 큐(stop)로 우아하게 요청하고,
응답이 없을 때만 PID 로 강제 종료한다. 실행 로그는 ``logs/engine-{mode}.log`` 에 남는다.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from app.config.settings import PROJECT_ROOT, Settings
from app.database.models import from_db_time
from app.database.repository import Repository

log = logging.getLogger(__name__)

HEARTBEAT_STALE_SECONDS = 45.0
ENGINE_LOG_MAX_BYTES = 10 * 1024 * 1024  # 엔진 stdout 로그 회전 기준 (감사 LOW-2)
ENGINE_LOG_BACKUPS = 3


def rotate_engine_log(path: Path, *, max_bytes: int = ENGINE_LOG_MAX_BYTES, backups: int = ENGINE_LOG_BACKUPS) -> bool:
    """``path`` 가 ``max_bytes`` 이상이면 ``.1`` ``.2`` … 로 밀어 회전한다(가장 오래된 것은 삭제). 회전했으면 True.

    엔진 stdout 은 자식 프로세스가 직접 쓰므로 실행 중 회전은 못 하고, 대시보드가 엔진을 띄울 때마다 검사한다
    (감사 LOW-2).
    """
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return False
        oldest = path.with_name(f"{path.name}.{backups}")
        if oldest.exists():
            oldest.unlink()
        for i in range(backups - 1, 0, -1):
            src = path.with_name(f"{path.name}.{i}")
            if src.exists():
                src.rename(path.with_name(f"{path.name}.{i + 1}"))
        path.rename(path.with_name(f"{path.name}.1"))
        return True
    except OSError as exc:  # 회전 실패는 시작을 막지 않는다
        log.warning("엔진 로그 회전 실패 %s: %s", path, exc)
        return False


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
    log_path = log_dir / f"engine-{mode}.log"
    rotate_engine_log(log_path)
    out = open(log_path, "ab")  # noqa: SIM115 - 자식 프로세스가 쓰는 핸들
    cmd = [python or sys.executable, "-m", "app.main", "run"]
    if mode == "live":
        cmd += ["--confirm-live", confirm_live]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["TRADING_MODE"] = "LIVE" if mode == "live" else "PAPER"
    kwargs: dict = {"cwd": str(PROJECT_ROOT), "stdout": out, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                    "env": env}
    if os.name == "nt":
        # CREATE_NO_WINDOW: 숨은 콘솔을 만들어 물려준다. DETACHED_PROCESS 를 쓰면 .venv 의 python.exe(런처)가 콘솔 없이
        # 뜨고, 그 자식인 실제 인터프리터가 새 콘솔을 만들어 빈 터미널 창이 나타난다(그 창을 닫으면 엔진이 죽는다).
        # CREATE_NEW_PROCESS_GROUP 은 kill_engine 의 CTRL_BREAK 전달용.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    finally:
        out.close()  # 자식이 핸들을 물려받았으니 부모 쪽은 닫는다 — 시작 횟수만큼 핸들이 새던 문제 (감사 LOW-10)
    log.info("엔진 프로세스 시작: pid=%s mode=%s", proc.pid, mode)
    return proc.pid


ENGINE_CMD_MARKERS = ("app.main", "run")


def process_cmdline(pid: int) -> str | None:
    """PID 의 명령줄을 OS 도구로 읽는다 (psutil 없이). 프로세스가 없으면 None, 확인에 실패하면 "" (감사 MEDIUM-5)."""
    if pid <= 0:
        return None
    try:
        if os.name == "nt":
            script = (
                f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}'; "
                "if ($p) { $p.CommandLine } else { exit 3 }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=20, check=False,
            )
            if result.returncode == 3:
                return None
            return (result.stdout or "").strip() if result.returncode == 0 else ""
        proc_path = Path(f"/proc/{int(pid)}/cmdline")
        if proc_path.exists():
            return proc_path.read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        result = subprocess.run(["ps", "-o", "args=", "-p", str(int(pid))], capture_output=True, text=True,
                                timeout=20, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("프로세스 %s 명령줄 확인 실패: %s", pid, exc)
        return ""


def is_engine_process(cmdline: str | None) -> bool:
    """명령줄이 우리 엔진(``python -m app.main run``)인지. 프로세스가 없거나(None) 확인 실패("")면 False."""
    if not cmdline:
        return False
    return all(marker in cmdline for marker in ENGINE_CMD_MARKERS)


def _process_alive(pid: int) -> bool:
    return process_cmdline(pid) is not None


def _wait_exit(pid: int, timeout: float, *, poll: float = 0.5, alive=None, clock=time.monotonic,
               sleep=time.sleep) -> bool:
    """프로세스가 ``timeout`` 초 안에 끝나면 True."""
    check = alive or _process_alive
    deadline = clock() + timeout
    while clock() < deadline:
        if not check(pid):
            return True
        sleep(poll)
    return not check(pid)


def _force_kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True)
    else:
        os.kill(pid, signal.SIGKILL)


def kill_engine(pid: int, *, grace_seconds: float = 8.0) -> bool:
    """엔진을 내린다: 먼저 정상 종료 신호(POSIX SIGTERM / Windows CTRL_BREAK)를 보내 finally(마지막 스냅샷·정지 알림·
    STOPPED 하트비트)가 돌게 하고, ``grace_seconds`` 안에 끝나지 않으면 강제 종료한다 (감사 LOW-10).
    호출 전에 process_cmdline/is_engine_process 로 대상을 확인할 것(MEDIUM-5).
    """
    try:
        os.kill(pid, signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
    except OSError as exc:
        log.warning("정상 종료 신호 실패 pid=%s (%s) → 강제 종료", pid, exc)
    else:
        if _wait_exit(pid, grace_seconds):
            log.info("엔진 pid=%s 정상 종료 확인", pid)
            return True
        log.warning("엔진 pid=%s 가 %.0f초 안에 끝나지 않음 → 강제 종료", pid, grace_seconds)
    try:
        _force_kill(pid)
        return True
    except OSError as exc:
        log.warning("강제 종료 실패 pid=%s: %s", pid, exc)
        return False
