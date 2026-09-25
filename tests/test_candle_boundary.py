"""엔진 메인 루프의 캔들 경계 처리 — 실제 시간처럼 2초씩 깨어나는 시계에서도 경계마다 캔들 처리가 실행돼야 한다.

조치 전에는 반복마다 '다음 경계'를 다시 계산해서, 유예 3초가 명령 폴링 간격 2초보다 길면 경계를 지난 반복에서
목표가 한 경계 뒤로 밀려 캔들 처리가 한 번도 실행되지 않았다 (2026-09-25 모의매매 점검 중 발견: 1분봉 10분 동안 0회).
"""

from __future__ import annotations

from datetime import timedelta

from tests.test_engine import T0, Harness


async def test_candle_processing_fires_at_each_boundary_with_polling_clock(make_settings) -> None:
    h = Harness(make_settings)  # 60m 캔들, 유예 3초, 명령 폴링 2초
    assert h.settings.candle_grace_seconds == 3.0 and h.engine.command_poll_interval == 2.0
    h.now = T0 + timedelta(hours=10, minutes=59, seconds=55)  # 11:00 경계 5초 전

    async def advance(seconds: float) -> None:  # 기다리는 대신 가짜 시계를 그만큼 진행
        h.now += timedelta(seconds=max(seconds, 0.001))

    h.engine._wait_for_stop = advance  # type: ignore[method-assign]
    await h.engine.run(duration_seconds=3610)  # 11:00 과 12:00 경계를 지난다
    checks = h.engine.stats.candle_checks
    assert checks == 2, f"경계마다 한 번씩 캔들 처리가 돌아야 한다 (실행 {checks}회)"  # 조치 전: 0회
    assert h.repo.read_engine_status().last_candle_at is not None
