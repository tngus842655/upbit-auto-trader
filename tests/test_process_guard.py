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
