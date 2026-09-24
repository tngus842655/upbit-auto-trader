"""실행 설정(RuntimeSettings) 테스트."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.risk import RiskConfig
from app.trading.runtime_settings import HOT_FIELDS, RESTART_FIELDS, RuntimeSettings


def test_from_settings_defaults(make_settings) -> None:
    rt = RuntimeSettings.from_settings(make_settings())
    assert rt.markets == ["KRW-BTC", "KRW-ETH"] and rt.strategy_name == "ma_cross" and rt.candle_interval == "60m"
    assert rt.risk.stop_loss_pct == 0.05
    assert rt.build_strategy().name == "ma_cross"
    assert rt.interval.value == "60m"
    d = rt.to_dict()
    assert set(d) == {"markets", "strategy_name", "strategy_params", "candle_interval", "risk"}
    assert RuntimeSettings(**d) == rt  # JSON 왕복


def test_normalization() -> None:
    rt = RuntimeSettings(markets="krw-xrp, KRW-SOL,krw-xrp", strategy_name="RSI", candle_interval="1h",
                         strategy_params={"window": 10})
    assert rt.markets == ["KRW-XRP", "KRW-SOL"] and rt.strategy_name == "rsi" and rt.candle_interval == "60m"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"markets": ["BTC"]}, "마켓 코드"),
        ({"strategy_name": "magic"}, "알 수 없는 전략"),
        ({"candle_interval": "2m"}, "캔들 단위"),
        ({"strategy_params": {"short_window": 50, "long_window": 10}}, "short_window"),
        ({"strategy_params": {"typo": 1}}, "typo"),
        ({"risk": {"stop_loss_pct": 1.5}}, "stop_loss_pct"),
        ({"unknown": 1}, "unknown"),
    ],
)
def test_validation_errors(kwargs, message: str) -> None:
    base = {"markets": ["KRW-BTC"], "strategy_name": "ma_cross", "candle_interval": "60m"}
    with pytest.raises(ValidationError) as info:
        RuntimeSettings(**{**base, **kwargs})
    assert message in str(info.value)


def test_changes_classification() -> None:
    base = RuntimeSettings(markets=["KRW-BTC"], strategy_name="ma_cross", candle_interval="60m")
    same = RuntimeSettings(**base.to_dict())
    assert base.changes_vs(same) == {"hot": [], "restart": []}
    hot = base.model_copy(update={"strategy_params": {"short_window": 5, "long_window": 20},
                                  "risk": RiskConfig(stop_loss_pct=0.03)})
    assert base.changes_vs(hot) == {"hot": ["strategy_params", "risk"], "restart": []}
    restart = RuntimeSettings(markets=["KRW-ETH"], strategy_name="rsi", candle_interval="15m")
    changes = base.changes_vs(restart)
    # 전략이 바뀌면 파라미터도 새 전략의 기본값으로 바뀐 것이므로 함께 보고된다
    assert changes["hot"] == ["strategy_name", "strategy_params"]
    assert changes["restart"] == ["markets", "candle_interval"]
    assert set(HOT_FIELDS) | set(RESTART_FIELDS) == set(base.to_dict())


def test_params_are_normalized_with_defaults() -> None:
    """.env 의 빈 파라미터와 대시보드 폼의 전체 파라미터가 같은 뜻이면 '변경 없음'이어야 한다."""
    from_env = RuntimeSettings(markets=["KRW-BTC"], strategy_name="ma_cross", strategy_params={})
    assert from_env.strategy_params["short_window"] == 20 and "volume_factor" in from_env.strategy_params
    from_form = RuntimeSettings(markets=["KRW-BTC"], strategy_name="ma_cross",
                                strategy_params=dict(from_env.strategy_params))
    assert from_env.changes_vs(from_form) == {"hot": [], "restart": []}


def test_param_schema_for_form() -> None:
    schema = RuntimeSettings.strategy_param_schema("ma_cross")
    props = schema["properties"]
    assert props["short_window"]["default"] == 20 and props["short_window"]["type"] == "integer"
    assert "description" in props["rsi_max_for_buy"]
    risk_schema = RiskConfig.model_json_schema()
    assert "anyOf" in risk_schema["properties"]["stop_loss_pct"]  # float | None → 폼에서 '끔' 체크박스
