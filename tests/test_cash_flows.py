"""감사 MEDIUM-4 회귀 — 대시보드가 기록한 입출금(포켓 이전)을 엔진이 리스크 기준 자산에 반영한다."""

from __future__ import annotations

from tests.test_engine import Harness


async def test_engine_applies_dashboard_cash_flows(make_settings) -> None:
    h = Harness(make_settings)
    h.repo.save_cash_flow(50_000, "시작 전 입금")
    await h.engine.warmup()
    h.engine.rebuild_risk_state(h.now, 1_000_000)
    # 시작 전 기록은 이미 현재 자산에 들어 있으니 반영하지 않고 커서만 맞춘다
    assert await h.engine.apply_cash_flows(h.now) == 0 and h.engine.risk.state.cash_flow_cursor == 1
    assert h.engine.risk.state.day_cash_flow == 0.0
    h.repo.save_cash_flow(-100_000, "봇→메인")
    assert await h.engine.apply_cash_flows(h.now) == 1
    assert h.engine.risk.state.day_cash_flow == -100_000 and h.engine.risk.daily_base_equity == 900_000
    assert await h.engine.apply_cash_flows(h.now) == 0  # 중복 반영 없음
    assert any(e.event == "cash_flow_applied" for e in h.repo.recent_logs(5))
    # 리스크 상태 저장소에 남아 재시작 후에도 이어진다
    assert h.engine.risk.store.load()["cash_flow_cursor"] == 2
