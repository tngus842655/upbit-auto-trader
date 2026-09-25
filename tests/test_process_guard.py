"""감사 MEDIUM-5 회귀 — 강제 종료 대상 확인: 명령줄 조회와 엔진 프로세스 판별."""

from __future__ import annotations

import os
import sys

from app.api.process import is_engine_process, process_cmdline


def test_process_cmdline_of_current_process_and_missing_pid() -> None:
    mine = process_cmdline(os.getpid())
    assert mine  # 현재 프로세스(pytest)는 조회된다
    exe = os.path.basename(sys.executable).lower().replace(".exe", "")
    assert exe in mine.lower() or "pytest" in mine.lower() or "python" in mine.lower()
    assert process_cmdline(999_999_999) is None  # 없는 PID
    assert process_cmdline(0) is None and process_cmdline(-5) is None


def test_is_engine_process_markers() -> None:
    assert is_engine_process("C:/x/python.exe -m app.main run --confirm-live REAL-MONEY") is True
    assert is_engine_process("/usr/bin/python3 -m app.main run") is True
    assert is_engine_process("python -m app.main serve") is False  # 대시보드 서버는 엔진이 아니다
    assert is_engine_process("notepad.exe") is False
    assert is_engine_process("") is False and is_engine_process(None) is False


def test_rotate_engine_log_rolls_backups(tmp_path) -> None:
    """감사 LOW-2 — 기준 크기를 넘은 엔진 로그는 .1/.2/.3 으로 밀리고 가장 오래된 것은 지워진다."""
    from app.api.process import rotate_engine_log

    path = tmp_path / "engine-paper.log"
    path.write_bytes(b"x" * 100)
    assert rotate_engine_log(path, max_bytes=1000, backups=3) is False  # 아직 작다
    assert path.exists()
    for round_no in range(1, 5):
        path.write_bytes(f"round{round_no}".encode() * 50)
        assert rotate_engine_log(path, max_bytes=10, backups=3) is True
        assert not path.exists()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["engine-paper.log.1", "engine-paper.log.2", "engine-paper.log.3"]
    assert (tmp_path / "engine-paper.log.1").read_bytes().startswith(b"round4")
    assert (tmp_path / "engine-paper.log.3").read_bytes().startswith(b"round2")  # round1 은 밀려나 삭제
    assert rotate_engine_log(tmp_path / "missing.log") is False


def test_kill_engine_graceful_then_force(monkeypatch) -> None:
    """감사 LOW-10 — 먼저 정상 종료 신호를 보내 기다리고, 끝나지 않을 때만 강제 종료한다."""
    from app.api import process as proc

    sent: list[tuple[int, int]] = []
    forced: list[int] = []
    monkeypatch.setattr(proc.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(proc, "_force_kill", lambda pid: forced.append(pid))
    monkeypatch.setattr(proc, "_wait_exit", lambda pid, timeout: True)
    assert proc.kill_engine(4242, grace_seconds=1.0) is True
    assert len(sent) == 1 and sent[0][0] == 4242 and forced == []  # 정상 종료만으로 끝 (조치 전: 바로 taskkill /F)
    monkeypatch.setattr(proc, "_wait_exit", lambda pid, timeout: False)
    assert proc.kill_engine(4243, grace_seconds=1.0) is True and forced == [4243]  # 기다려도 안 끝나면 강제

    def boom(pid, sig):
        raise OSError("no such process group")

    monkeypatch.setattr(proc.os, "kill", boom)
    assert proc.kill_engine(4244) is True and forced == [4243, 4244]  # 신호를 못 보내면 바로 강제


def test_wait_exit_polls_until_gone() -> None:
    from app.api import process as proc

    alive = iter([True, True, False])
    clock = {"t": 0.0}
    assert proc._wait_exit(1, 5.0, alive=lambda pid: next(alive, False), clock=lambda: clock["t"],
                           sleep=lambda s: clock.__setitem__("t", clock["t"] + s)) is True
    assert proc._wait_exit(1, 1.0, alive=lambda pid: True, clock=lambda: clock["t"],
                           sleep=lambda s: clock.__setitem__("t", clock["t"] + s)) is False


async def test_install_stop_handlers_requests_engine_stop(make_settings) -> None:
    """감사 LOW-10 — 종료 시그널을 받으면 엔진 stop 이벤트가 켜져 finally(정지 알림·STOPPED 하트비트)까지 돈다."""
    import asyncio
    import signal

    from app.main import install_stop_handlers
    from tests.test_engine import Harness

    h = Harness(make_settings)
    installed = install_stop_handlers(h.engine)
    loop = asyncio.get_running_loop()
    try:
        if os.name == "nt":
            assert "SIGBREAK" in installed
            handler = signal.getsignal(signal.SIGBREAK)
            handler(signal.SIGBREAK, None)  # CTRL_BREAK 수신을 흉내 낸다
            await asyncio.sleep(0)
            assert h.engine._stop.is_set()
        else:
            assert "SIGTERM" in installed and "SIGINT" in installed
    finally:
        for name in installed:
            if os.name == "nt":
                signal.signal(getattr(signal, name), signal.SIG_DFL)
            else:
                loop.remove_signal_handler(getattr(signal, name))


def test_start_engine_hides_console_window(monkeypatch, tmp_path, make_settings) -> None:
    """Windows: 엔진은 숨은 콘솔(CREATE_NO_WINDOW)로 띄운다 — DETACHED_PROCESS 면 빈 터미널 창이 뜬다."""
    import subprocess

    from app.api import process as proc

    captured = {}

    class FakePopen:
        pid = 4242

        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)

    monkeypatch.setattr(proc.subprocess, "Popen", FakePopen)
    settings = make_settings(log_dir=str(tmp_path / "logs"))
    assert proc.start_engine(settings, "paper") == 4242
    assert captured["cmd"][-3:] == ["-m", "app.main", "run"] and captured["env"]["TRADING_MODE"] == "PAPER"
    assert captured["stdin"] is subprocess.DEVNULL and captured["stdout"].closed  # 부모 쪽 로그 핸들은 닫힘 (LOW-10)
    if os.name == "nt":
        flags = captured["creationflags"]
        assert flags & subprocess.CREATE_NO_WINDOW and flags & subprocess.CREATE_NEW_PROCESS_GROUP
        assert not flags & subprocess.DETACHED_PROCESS
    else:
        assert captured["start_new_session"] is True
