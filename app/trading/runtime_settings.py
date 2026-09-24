"""실행 설정(RuntimeSettings) — 대시보드에서 바꿀 수 있는 값들의 단일 모델.

역할 분리:
- ``.env``(Settings): 비밀(API Key)·인프라(DB, 포트)·거래 모드(LIVE 이중 플래그)처럼 **재시작으로만** 바뀌는 값.
- ``RuntimeSettings``: 거래 마켓, 전략과 파라미터, 캔들 단위, 리스크 수치처럼 운영 중 바꾸는 값.
  최초 실행 때 ``.env`` 값으로 만들어 DB(``bot_settings``)에 버전 1 로 저장하고, 대시보드가 저장하면 버전이 올라간다.
  엔진은 캔들 경계마다 버전을 확인해 반영한다 — 지시 문서의 "즉시 무조건 적용하지 말고 다음 거래부터 적용".

반영 방식:
- 즉시(다음 캔들부터): ``strategy_name``, ``strategy_params``, ``risk``
- 재시작 필요: ``markets``, ``candle_interval`` (워밍업·구독 대상이 바뀌므로)
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config.settings import MARKET_CODE_RE, Settings
from app.core.exceptions import StrategyError
from app.exchange.models import CandleInterval
from app.risk.config import RiskConfig
from app.strategy import STRATEGIES, Strategy, create_strategy

HOT_FIELDS = ("strategy_name", "strategy_params", "risk")
RESTART_FIELDS = ("markets", "candle_interval")


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markets: list[str] = Field(min_length=1, max_length=50)
    strategy_name: str
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    candle_interval: str = "60m"
    risk: RiskConfig = Field(default_factory=RiskConfig)

    @field_validator("markets", mode="before")
    @classmethod
    def _normalize_markets(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            value = value.split(",")
        items = [str(v).strip().upper() for v in value if str(v).strip()]
        invalid = [m for m in items if not re.match(MARKET_CODE_RE, m)]
        if invalid:
            raise ValueError(f"잘못된 마켓 코드 {invalid}: 'KRW-BTC' 형식이어야 합니다")
        return list(dict.fromkeys(items))

    @field_validator("candle_interval")
    @classmethod
    def _normalize_interval(cls, value: str) -> str:
        return CandleInterval.parse(value).value

    @field_validator("strategy_name")
    @classmethod
    def _known_strategy(cls, value: str) -> str:
        key = value.strip().lower()
        if key not in STRATEGIES:
            raise ValueError(f"알 수 없는 전략 '{value}'. 사용 가능: {', '.join(STRATEGIES)}")
        return key

    @model_validator(mode="after")
    def _params_valid_for_strategy(self) -> RuntimeSettings:
        try:
            strategy = create_strategy(self.strategy_name, self.strategy_params)
        except StrategyError as exc:
            raise ValueError(str(exc)) from exc
        # 기본값까지 채운 정규형으로 보관 — .env 의 빈 파라미터와 대시보드가 보낸 전체 폼이 같은 뜻이면 같은 값이 되도록
        self.strategy_params = strategy.params.model_dump(mode="json")
        return self

    # ------------------------------------------------------------------
    @classmethod
    def from_settings(cls, settings: Settings) -> RuntimeSettings:
        return cls(
            markets=list(settings.markets),
            strategy_name=settings.strategy_name,
            strategy_params=dict(settings.strategy_params),
            candle_interval=settings.candle_interval,
            risk=settings.risk_config(),
        )

    def build_strategy(self) -> Strategy:
        return create_strategy(self.strategy_name, self.strategy_params)

    @property
    def interval(self) -> CandleInterval:
        return CandleInterval.parse(self.candle_interval)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def changes_vs(self, other: RuntimeSettings) -> dict[str, list[str]]:
        """``other``(새 설정)와 비교해 바뀐 필드를 즉시 반영/재시작 필요로 나눈다."""
        mine, theirs = self.to_dict(), other.to_dict()
        changed = [k for k in mine if mine[k] != theirs[k]]
        return {
            "hot": [k for k in changed if k in HOT_FIELDS],
            "restart": [k for k in changed if k in RESTART_FIELDS],
        }

    @staticmethod
    def strategy_param_schema(name: str) -> dict[str, Any]:
        """대시보드 설정 폼용: 전략 파라미터 JSON 스키마 (필드명·타입·기본값·설명·범위)."""
        cls = STRATEGIES[name.strip().lower()]
        return cls.Params.model_json_schema()
