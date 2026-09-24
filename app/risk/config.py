"""리스크 설정 (RiskConfig).

모든 리스크 수치를 한 객체에 모은다. 지금은 ``.env``(Settings) 에서 만들지만, 같은 모델을 DB·웹 입력에서
만들어 끼워 넣을 수 있도록 Settings 와 분리해 둔다. 값은 pydantic 으로 검증한다.

- ``position_fraction``: 매수 시 현금 사용 비율 (1.0 = 전액)
- ``max_order_amount``: 거래당 최대 투자금 (KRW). None 이면 제한 없음
- ``max_position_ratio``: 전체 자산 대비 코인 평가액 상한 (신규 매수 후 기준). 1.0 이면 제한 없음
- ``max_open_positions``: 동시에 보유할 최대 마켓 수
- ``daily_loss_limit_pct``: 당일(KST) 시작 자산 대비 손실 한도. 넘으면 그날은 신규 진입 중지. None 이면 끔
- ``max_consecutive_losses``: 연속 손실 횟수 한도. 넘으면 그날은 신규 진입 중지. None 이면 끔
- ``stop_loss_pct`` / ``take_profit_pct``: 평균 매수가 대비 손절·익절 비율. None 이면 끔
- ``trailing_stop_pct``: 보유 중 최고가 대비 하락률로 청산. None 이면 끔
- ``price_deviation_limit``: 신호 캔들 종가와 체결 참조가의 괴리 상한 (비정상 시세 방어). None 이면 끔
- ``cooldown_seconds``: 같은 마켓 청산 후 재진입 대기 시간
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RiskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    position_fraction: float = Field(default=1.0, gt=0, le=1.0)
    max_order_amount: float | None = Field(default=None, gt=0)
    max_position_ratio: float = Field(default=1.0, gt=0, le=1.0)
    max_open_positions: int = Field(default=1, ge=1, le=50)
    daily_loss_limit_pct: float | None = Field(default=0.03, gt=0, lt=1)
    max_consecutive_losses: int | None = Field(default=3, ge=1, le=100)
    stop_loss_pct: float | None = Field(default=0.05, gt=0, lt=1)
    take_profit_pct: float | None = Field(default=None, gt=0)
    trailing_stop_pct: float | None = Field(default=None, gt=0, lt=1)
    price_deviation_limit: float | None = Field(default=0.10, gt=0, lt=1)
    min_order_amount: float = Field(default=5000.0, ge=0)
    cooldown_seconds: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def _check_relations(self) -> RiskConfig:
        if self.max_order_amount is not None and self.max_order_amount < self.min_order_amount:
            raise ValueError("max_order_amount 는 min_order_amount 이상이어야 합니다")
        return self

    @classmethod
    def unrestricted(cls, **overrides: Any) -> RiskConfig:
        """백테스트 기본값: 손절·익절·한도 없이 순수 전략 성과만 본다."""
        base: dict[str, Any] = {
            "daily_loss_limit_pct": None, "max_consecutive_losses": None, "stop_loss_pct": None,
            "take_profit_pct": None, "trailing_stop_pct": None, "price_deviation_limit": None,
        }
        base.update(overrides)
        return cls(**base)

    def describe(self) -> dict[str, Any]:
        def pct(v: float | None) -> str:
            return "끔" if v is None else f"{v * 100:g}%"

        return {
            "현금 사용 비율": f"{self.position_fraction * 100:g}%",
            "거래당 최대 투자금": "제한 없음" if self.max_order_amount is None else f"{self.max_order_amount:,.0f} KRW",
            "자산 대비 포지션 상한": f"{self.max_position_ratio * 100:g}%",
            "최대 포지션 수": str(self.max_open_positions),
            "일일 손실 한도": pct(self.daily_loss_limit_pct),
            "최대 연속 손실": "끔" if self.max_consecutive_losses is None else f"{self.max_consecutive_losses}회",
            "손절": pct(self.stop_loss_pct),
            "익절": pct(self.take_profit_pct),
            "추적 손절": pct(self.trailing_stop_pct),
            "시세 괴리 한도": pct(self.price_deviation_limit),
            "재진입 대기": f"{self.cooldown_seconds:g}초",
        }
