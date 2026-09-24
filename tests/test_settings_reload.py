"""엔진의 실행 설정 핫리로드 테스트 — 대시보드가 저장한 새 버전이 다음 캔들 경계에 반영되는지."""

from __future__ import annotations

from app.risk import RiskConfig
from app.strategy import create_strategy
from app.trading.runtime_settings import RuntimeSettings
from tests.test_engine import MARKET, Harness


def base_runtime() -> RuntimeSettings:
    return RuntimeSettings(markets=[MARKET], strategy_name="ma_cross", candle_interval="60m",
                           strategy_params={"short_window": 20, "long_window": 60},
                           risk=RiskConfig.unrestricted(position_fraction=0.5, stop_loss_pct=0.05))


async def test_hot_reload_applies_strategy_and_risk(make_settings) -> None:
    h = Harness(make_settings)
    await h.engine.warmup()
    rt = base_runtime()
    h.engine.runtime = rt
    h.engine.strategy = rt.build_strategy()
    h.engine.settings_version = h.repo.save_runtime_settings(rt.to_dict(), "v1")
    assert await h.engine.maybe_reload_settings() is None  # 새 버전 없음

    new = rt.model_copy(update={"strategy_params": {"short_window": 5, "long_window": 20},
                                "risk": RiskConfig.unrestricted(position_fraction=0.5, stop_loss_pct=0.02)})
    v2 = h.repo.save_runtime_settings(new.to_dict(), "손절 축소")
    changes = await h.engine.maybe_reload_settings()
    assert changes == {"hot": ["strategy_params", "risk"], "restart": []}
    assert h.engine.strategy.params.short_window == 5
    assert h.engine.risk.config.stop_loss_pct == 0.02
    assert h.engine.settings_version == v2 and h.engine.restart_required is False
    status = h.repo.read_engine_status()
    assert status.settings_version == v2 and status.restart_required is False
    assert any(e.event == "settings_applied" for e in h.repo.recent_logs(5))


async def test_restart_required_and_invalid_version_skipped(make_settings) -> None:
    h = Harness(make_settings)
    await h.engine.warmup()
    rt = base_runtime()
    h.engine.runtime = rt
    h.engine.strategy = rt.build_strategy()
    h.engine.settings_version = h.repo.save_runtime_settings(rt.to_dict(), "v1")

    bad = h.repo.save_runtime_settings({**rt.to_dict(), "strategy_name": "magic"}, "잘못된 값")
    assert await h.engine.maybe_reload_settings() is None
    assert h.engine.settings_version == bad  # 잘못된 버전은 건너뛰고 표시만
    assert any(e.event == "settings_invalid" for e in h.repo.recent_logs(5))

    moved = rt.model_copy(update={"markets": ["KRW-ETH"], "candle_interval": "15m"})
    v3 = h.repo.save_runtime_settings(moved.to_dict(), "마켓 변경")
    changes = await h.engine.maybe_reload_settings()
    assert changes["restart"] == ["markets", "candle_interval"] and h.engine.restart_required is True
    assert h.engine.settings_version == v3
    assert h.repo.read_engine_status().restart_required is True
    assert "재시작 필요" in h.repo.read_engine_status().message


async def test_reload_command_and_strategy_switch_triggers_warmup(make_settings) -> None:
    h = Harness(make_settings)
    await h.engine.warmup()
    rt = base_runtime()
    h.engine.runtime = rt
    h.engine.strategy = rt.build_strategy()
    h.engine.settings_version = h.repo.save_runtime_settings(rt.to_dict(), "v1")
    calls_before = len(h.client.candle_calls)

    switched = rt.model_copy(update={"strategy_name": "rsi", "strategy_params": {"window": 14}})
    h.repo.save_runtime_settings(switched.to_dict(), "전략 교체")
    h.repo.enqueue_command("reload")
    assert h.engine.poll_commands() == ["reload"] and h.engine._reload_requested is True
    changes = await h.engine.maybe_reload_settings()
    assert changes["hot"] == ["strategy_name", "strategy_params"]
    assert h.engine.strategy.name == "rsi" and isinstance(h.engine.strategy, type(create_strategy("rsi")))
    # 워밍업 캔들(8개)이 rsi 워밍업(16)보다 적으므로 다시 받는다
    assert len(h.client.candle_calls) > calls_before
