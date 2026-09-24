"""감사 HIGH-7 회귀 — 같은 모드의 엔진은 한 PC 에서 하나만 뜬다."""

from __future__ import annotations

import os

import pytest

from app.main import acquire_engine_lock
from app.trading.instance_lock import InstanceLock


def test_second_lock_on_same_file_fails_until_released(tmp_path) -> None:
    path = tmp_path / "engine-paper.lock"
    first = InstanceLock(path)
    assert first.acquire() is True and first.held
    assert str(os.getpid()) in first.holder()
    second = InstanceLock(path)
    assert second.acquire() is False and not second.held  # 다른 핸들로는 잡을 수 없다
    with pytest.raises(RuntimeError, match="이미 실행 중"):
        with InstanceLock(path):
            pass
    first.release()
    assert not first.held
    assert second.acquire() is True  # 풀리면 잡힌다
    second.release()
    assert first.acquire() is True  # 같은 객체로 다시 잡기
    first.release()


def test_modes_use_separate_locks(tmp_path) -> None:
    paper = acquire_engine_lock("paper", base_dir=tmp_path)
    live = acquire_engine_lock("live", base_dir=tmp_path)
    assert paper is not None and live is not None
    assert acquire_engine_lock("paper", base_dir=tmp_path) is None  # 두 번째 paper 는 거부
    assert (tmp_path / "engine-paper.lock").exists() and (tmp_path / "engine-live.lock").exists()
    paper.release()
    live.release()
    assert acquire_engine_lock("paper", base_dir=tmp_path) is not None
