"""감사 HIGH-3 회귀 — 재시작 뒤에도 당일 시작 자산·잠금·긴급 정지·재진입 시각이 유지된다."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.database import Database, Repository
from app.risk import RiskConfig, RiskManager
from app.risk.state_store import MemoryRiskStateStore, RepositoryRiskStateStore, default_risk_store
from tests.test_engine import MARKET, Harness

DAY = datetime(2026, 5, 1, 1, 0, tzinfo=UTC)  # KST 10:00


def test_restart_restores_day_start_equity_and_halt_in_process() -> None:
    """감사 재현 스크립트 [HIGH-3] 와 같은 시나리오: 같은 프로세스에서 새 RiskManager 를 만들어도 상태가 이어진다."""
    rm = RiskManager(RiskConfig(daily_loss_limit_pct=0.03))
    rm.update_equity(1_000_000, DAY)
    rm.update_equity(975_000, DAY + timedelta(hours=1))  # -2.5%, 아직 잠금 없음
    rm2 = RiskManager(RiskConfig(daily_loss_limit_pct=0.03))
    rm2.rebuild([], DAY + timedelta(hours=2), 975_000)
    assert rm2.state.day_start_equity == 1_000_000  # 재시작 시점 자산(975,000)이 아니라 당일 시작 자산
    assert rm2.update_equity(950_000, DAY + timedelta(hours=3)) is not None  # 당일 누적 -5% → 잠금
    rm.halt("수동 긴급 정지")
    rm3 = RiskManager(RiskConfig())
    rm3.rebuild([], DAY + timedelta(hours=2), 975_000)
    assert rm3.state.halted is True and rm3.state.halt_reason == "수동 긴급 정지"
    rm3.resume()
    rm4 = RiskManager(RiskConfig())
    rm4.rebuild([], DAY + timedelta(hours=3), 975_000)
    assert rm4.state.halted is False


def test_daily_loss_lock_survives_restart_and_new_day_resets_it() -> None:
    store = MemoryRiskStateStore()
    rm = RiskManager(RiskConfig(daily_loss_limit_pct=0.03), store=store)
    rm.update_equity(1_000_000, DAY)
    assert rm.update_equity(960_000, DAY + timedelta(hours=1)) is not None
    rm2 = RiskManager(RiskConfig(daily_loss_limit_pct=0.03), store=store)
    rm2.rebuild([], DAY + timedelta(hours=2), 990_000)
    assert rm2.state.lock_reason and "일일 손실" in rm2.state.lock_reason  # 거래로는 재현 안 되는 잠금도 복구
    assert rm2.state.lock_kind == "daily" and rm2.entries_blocked_reason
    rm3 = RiskManager(RiskConfig(daily_loss_limit_pct=0.03), store=store)
    rm3.rebuild([], DAY + timedelta(days=1), 990_000)  # 다음 날 → 잠금 해제, 시작 자산은 현재 자산
    assert rm3.state.lock_reason is None and rm3.state.day_start_equity == 990_000


def test_consecutive_lock_is_recomputed_from_trades_not_restored() -> None:
    """연속 손실 잠금은 저장값이 아니라 오늘 거래 재반영으로 결정된다 (오래된 잠금이 잘못 살아나지 않게)."""
    from tests.test_risk import trade

    store = MemoryRiskStateStore()
    rm = RiskManager(RiskConfig(max_consecutive_losses=2), store=store)
    rm.record_trade(trade(-1.0, DAY))
    assert rm.record_trade(trade(-1.0, DAY + timedelta(minutes=1))) is not None
    assert store.load()["lock_kind"] == "consecutive"
    rm2 = RiskManager(RiskConfig(max_consecutive_losses=2), store=store)
    rm2.rebuild([trade(-1.0, DAY)], DAY + timedelta(hours=1), 1_000_000)  # DB 에는 손실 1건만 남아 있는 상황
    assert rm2.state.consecutive_losses == 1 and rm2.state.lock_reason is None


def test_separate_stores_do_not_share_state() -> None:
    a = RiskManager(RiskConfig(), store=MemoryRiskStateStore())
    b = RiskManager(RiskConfig(), store=MemoryRiskStateStore())
    a.halt("a 만 정지")
    b.rebuild([], DAY, 1_000_000)
    assert b.state.halted is False
    assert default_risk_store().load() is None  # 기본 저장소는 건드리지 않았다


def test_repository_store_roundtrip(make_settings) -> None:
    db = Database("sqlite://")
    db.create_all()
    repo = Repository(db, mode="paper")
    rm = RiskManager(RiskConfig(daily_loss_limit_pct=0.03, cooldown_seconds=600), store=RepositoryRiskStateStore(repo))
    rm.update_equity(1_000_000, DAY)
    rm.halt("점검")
    rm.state.last_exit_at[MARKET] = DAY
    rm._persist()
    saved = repo.load_risk_state()
    assert saved["day_start_equity"] == 1_000_000 and saved["halted"] is True and saved["last_exit_at"][MARKET]
    rm2 = RiskManager(RiskConfig(daily_loss_limit_pct=0.03, cooldown_seconds=600), store=RepositoryRiskStateStore(repo))
    rm2.rebuild([], DAY + timedelta(hours=1), 980_000)
    assert rm2.state.day_start_equity == 1_000_000 and rm2.state.halted and rm2.state.last_exit_at[MARKET] == DAY


async def test_engine_rebuild_uses_db_store(make_settings) -> None:
    h = Harness(make_settings)
    h.engine.risk = RiskManager(RiskConfig(daily_loss_limit_pct=0.03), store=RepositoryRiskStateStore(h.repo))
    h.engine.risk.update_equity(1_000_000, h.now)
    h.engine.risk.halt("수동")
    h2 = Harness(make_settings, db=h.db)  # 같은 DB 로 새 엔진(재시작)
    h2.engine.risk = RiskManager(RiskConfig(daily_loss_limit_pct=0.03), store=RepositoryRiskStateStore(h2.repo))
    h2.now = h.now + timedelta(minutes=30)
    h2.engine.rebuild_risk_state(h2.now, 975_000)
    assert h2.engine.risk.state.day_start_equity == 1_000_000 and h2.engine.risk.state.halted
    assert any(e.event == "risk_lock_restored" for e in h2.repo.recent_logs(5))
    h2.engine.risk.resume()
    assert h2.engine.risk.update_equity(950_000, h2.now) is not None  # 당일 -5% → 잠금 발동


def test_store_failure_does_not_break_risk_manager() -> None:
    class Broken:
        def save(self, data):
            raise RuntimeError("disk full")

        def load(self):
            raise RuntimeError("disk full")

    rm = RiskManager(RiskConfig(daily_loss_limit_pct=0.03), store=Broken())
    rm.update_equity(1_000_000, DAY)
    rm.halt("x")
    rm.rebuild([], DAY, 1_000_000)  # load 실패 → 빈 상태로 시작하되 예외 없음
    assert rm.state.day_start_equity == 1_000_000 and rm.state.halted is False
    with pytest.raises(RuntimeError):
        Broken().load()


def test_cash_flow_state_survives_restart_same_day() -> None:
    """감사 MEDIUM-4 — 당일 입출금·커서는 재시작 뒤에도 남고, 날짜가 바뀌면 커서는 미초기화(None)로 돌아간다."""
    t = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)
    store = MemoryRiskStateStore()
    rm = RiskManager(RiskConfig(daily_loss_limit_pct=0.03), store=store)
    rm.update_equity(1_000_000, t)
    rm.apply_cash_flow(-100_000, 7, t)
    again = RiskManager(RiskConfig(daily_loss_limit_pct=0.03), store=store)
    again.rebuild([], t + timedelta(hours=1), 895_000)
    assert again.state.day_cash_flow == -100_000 and again.state.cash_flow_cursor == 7
    assert again.daily_base_equity == 900_000
    assert again.update_equity(870_000, t + timedelta(hours=2)) is not None
    fresh = RiskManager(RiskConfig(daily_loss_limit_pct=0.03), store=store)
    fresh.rebuild([], t + timedelta(days=1), 870_000)
    assert fresh.state.day_cash_flow == 0.0 and fresh.state.cash_flow_cursor is None
