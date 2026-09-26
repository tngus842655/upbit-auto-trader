"""전략 인터페이스와 신호 모델.

원칙:
- 전략은 **닫힌 캔들 DataFrame** (``app.strategy.data`` 규약) 만 입력으로 받는 순수 함수다.
  같은 코드가 백테스트·모의매매·실거래에서 그대로 쓰인다.
- ``evaluate(df)`` 는 전 구간에 대해 지표와 ``action`` 열을 벡터 연산으로 만든다. 지표가 인과적이면
  프리픽스 계산 ``evaluate(df[:i+1])`` 의 마지막 행과 ``evaluate(df)[i]`` 가 같아야 하며,
  ``check_no_lookahead()`` 가 이를 검증한다 (Look-ahead Bias 자동 검사).
- ``generate_signal(df)`` 는 마지막 닫힌 캔들에 대한 ``Signal`` 하나를 돌려준다.
- 포지션 보유 여부는 전략이 아니라 주문·리스크 계층이 판단한다. 전략은 "지금 조건이 매수/매도 조건인가" 만 답한다.
- 전략은 계열(``StrategyFamily``: 추세·평균회귀·돌파·필터)을 밝힌다. 지금은 표시·비교용이며, 나중에 시장 상태에 따라
  계열을 고르는 Strategy Router 가 이 값으로 후보를 고른다.
- 어떤 전략도 수익을 보장하지 않는다. 수익성은 백테스트(Phase 4)·모의매매(Phase 5)로만 검증한다.
"""

from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.exceptions import MarketDataError, StrategyError
from app.strategy.data import CANDLE_COLUMNS


class Action(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class StrategyFamily(StrEnum):
    """전략 계열. 대시보드 묶음 표시·성과 비교에 쓰고, 향후 Strategy Router 가 계열 단위로 전략을 고른다."""

    TREND = "TREND"  # 추세 추종: 이미 난 추세에 올라탄다
    MEAN_REVERSION = "MEAN_REVERSION"  # 평균 회귀: 과매도 반등·과매수 하락을 노린다
    BREAKOUT = "BREAKOUT"  # 돌파: 박스권 상단을 거래량과 함께 뚫을 때 들어간다
    FILTER = "FILTER"  # 필터: 추세 강도(ADX)·거래량 흐름(OBV)으로 방향을 확인한다

    @property
    def label(self) -> str:
        return _FAMILY_LABELS[self]


_FAMILY_LABELS = {
    StrategyFamily.TREND: "추세 추종",
    StrategyFamily.MEAN_REVERSION: "평균 회귀",
    StrategyFamily.BREAKOUT: "돌파",
    StrategyFamily.FILTER: "필터 (추세 강도·거래량)",
}


ACTION_COLUMN = "action"
REASON_COLUMN = "reason"
WARMUP_REASON = "warmup"


@dataclass(frozen=True)
class Signal:
    """전략이 한 캔들에 대해 내린 결론. 로그·DB(strategy_signals)에 그대로 기록한다."""

    action: Action
    market: str
    time: datetime  # 신호를 만든 캔들의 시작 시각(UTC)
    price: float  # 그 캔들의 종가 (참고 가격)
    strategy: str
    reason: str = ""
    indicators: dict[str, float] = field(default_factory=dict)
    strength: float | None = None  # 0~1, 전략이 제공하면 포지션 크기 조절에 활용

    @property
    def is_actionable(self) -> bool:
        return self.action is not Action.HOLD

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "market": self.market,
            "time": self.time.isoformat(),
            "price": self.price,
            "strategy": self.strategy,
            "reason": self.reason,
            "strength": self.strength,
            "indicators": {k: (None if v is None or (isinstance(v, float) and math.isnan(v)) else v)
                           for k, v in self.indicators.items()},
        }


class StrategyParams(BaseModel):
    """전략 파라미터 기본형. 하위 클래스가 필드를 정의한다. 알 수 없는 키는 오류로 막는다(오타 방지)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def param(
    default: Any,
    *,
    label: str,
    description: str | None = None,
    help_text: str | None = None,
    depends_on: str | None = None,
    **constraints: Any,
) -> Any:
    """화면 정보가 붙은 파라미터 필드. 대시보드 설정 폼은 JSON 스키마로 자동 생성되므로 여기 적은 값이 그대로 보인다.

    - ``label``: 폼에 보일 이름 (없으면 필드 이름)
    - ``description``: 입력칸 옆 짧은 설명, ``help_text``: ? 아이콘 툴팁
    - ``depends_on``: 이 불리언 필드가 꺼져 있으면 폼에서 입력칸을 비활성화한다 (값은 무시됨)
    """
    extra: dict[str, Any] = {"label": label}
    if help_text:
        extra["help"] = help_text
    if depends_on:
        extra["depends_on"] = depends_on
    return Field(default=default, description=description, json_schema_extra=extra, **constraints)


class Strategy(ABC):
    """전략 기본 클래스.

    하위 클래스는 ``name``, ``Params``, ``warmup_periods``, ``evaluate()`` 를 구현한다.
    ``evaluate()`` 가 돌려주는 DataFrame 은 입력과 같은 index 를 가지며 최소한
    ``action``(BUY/SELL/HOLD 문자열), ``reason`` 열과 지표 열들을 포함한다.
    ``family`` 는 전략 계열, ``rules`` 는 화면에 보여 줄 매수·매도 규칙 요약이다 (동작에는 영향 없음).
    """

    name: ClassVar[str] = "base"
    description: ClassVar[str] = ""
    family: ClassVar[StrategyFamily | None] = None
    rules: ClassVar[str] = ""
    Params: ClassVar[type[StrategyParams]] = StrategyParams

    def __init__(self, params: Mapping[str, Any] | StrategyParams | None = None) -> None:
        try:
            if isinstance(params, StrategyParams):
                self.params = params
            else:
                self.params = self.Params.model_validate(dict(params or {}))
        except ValidationError as exc:
            raise StrategyError(f"{self.name} 파라미터 오류: {exc}") from exc

    # ------------------------------------------------------------------
    @property
    @abstractmethod
    def warmup_periods(self) -> int:
        """유효한 신호를 내기 위해 필요한 최소 캔들 수."""

    @abstractmethod
    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        """전 구간 지표·행동 계산 (인과적이어야 한다)."""

    # ------------------------------------------------------------------
    @property
    def indicator_columns(self) -> list[str]:
        """``evaluate`` 결과에서 신호 ``indicators`` 로 기록할 열. 기본은 action/reason 을 뺀 전부."""
        return []

    def generate_signal(self, df: pd.DataFrame, *, market: str | None = None) -> Signal:
        """마지막 닫힌 캔들에 대한 신호. 캔들이 부족하면 HOLD(warmup)."""
        self._check_frame(df)
        market = market or df.attrs.get("market") or ""
        if len(df) < self.warmup_periods:
            last = df.index[-1].to_pydatetime()
            return Signal(
                Action.HOLD, market, last, float(df["close"].iloc[-1]), self.name,
                reason=f"{WARMUP_REASON}: {len(df)}/{self.warmup_periods} 캔들",
            )
        result = self.evaluate(df)
        return self._signal_from_row(result, len(result) - 1, market)

    def scan(self, df: pd.DataFrame, *, market: str | None = None, include_hold: bool = False) -> list[Signal]:
        """전 구간의 신호 목록 (백테스트·점검용). 기본은 BUY/SELL 만 돌려준다."""
        self._check_frame(df)
        market = market or df.attrs.get("market") or ""
        result = self.evaluate(df)
        signals = []
        for i in range(len(result)):
            if i < self.warmup_periods - 1 and not include_hold:
                continue
            signal = self._signal_from_row(result, i, market)
            if include_hold or signal.is_actionable:
                signals.append(signal)
        return signals

    # ------------------------------------------------------------------
    def _check_frame(self, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            raise MarketDataError("전략 입력 캔들이 비어 있습니다")
        missing = [c for c in CANDLE_COLUMNS if c not in df.columns]
        if missing:
            raise MarketDataError(f"전략 입력에 열이 없습니다: {missing}")

    def _signal_from_row(self, result: pd.DataFrame, i: int, market: str) -> Signal:
        row = result.iloc[i]
        action = Action(str(row[ACTION_COLUMN]))
        cols = self.indicator_columns or [
            c for c in result.columns if c not in (ACTION_COLUMN, REASON_COLUMN, *CANDLE_COLUMNS)
        ]
        indicators = {c: _to_float(row[c]) for c in cols if c in result.columns}
        return Signal(
            action=action,
            market=market,
            time=result.index[i].to_pydatetime(),
            price=float(row["close"]),
            strategy=self.name,
            reason=str(row.get(REASON_COLUMN, "")),
            indicators=indicators,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.params.model_dump()})"


def _to_float(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return math.nan
    return f


def reasons(default: str, *cases: tuple[pd.Series, str]) -> pd.Series:
    """조건별 사유 문자열 열을 만든다. 앞의 조건이 우선한다."""
    if not cases:
        raise ValueError("cases 가 비어 있습니다")
    conds = [c.fillna(False).to_numpy(dtype=bool) for c, _ in cases]
    texts = [t for _, t in cases]
    out = np.select(conds, texts, default=default)
    return pd.Series(out, index=cases[0][0].index, dtype="object")


def warmup_mask(df: pd.DataFrame, warmup_periods: int) -> pd.Series:
    """워밍업 구간(앞쪽 ``warmup_periods - 1`` 개 캔들) 표시. 이 구간에서는 신호를 내지 않는다."""
    return pd.Series(np.arange(len(df)) < warmup_periods - 1, index=df.index)


def action_series(buy: pd.Series, sell: pd.Series) -> pd.Series:
    """BUY/SELL 불리언 열 → ``action`` 문자열 열. 둘 다 참이면 SELL (기존 전략과 같은 우선순위)."""
    actions = pd.Series(Action.HOLD.value, index=buy.index, dtype="object")
    actions[buy.fillna(False).astype(bool)] = Action.BUY.value
    actions[sell.fillna(False).astype(bool)] = Action.SELL.value
    return actions


def check_no_lookahead(
    strategy: Strategy,
    df: pd.DataFrame,
    *,
    points: Iterable[int] | None = None,
    samples: int = 8,
    seed: int = 0,
) -> None:
    """프리픽스 계산과 전체 계산이 일치하는지 검사한다. 어긋나면 ``StrategyError``.

    지표가 미래 값을 참조하면(예: ``shift(-1)``, ``center=True``) 반드시 어긋난다.
    """
    full = strategy.evaluate(df)
    n = len(df)
    start = max(strategy.warmup_periods, 2)
    if n <= start:
        raise StrategyError(f"Look-ahead 검사에 캔들이 부족합니다: {n} <= {start}")
    if points is None:
        rng = random.Random(seed)
        candidates = list(range(start, n))
        points = sorted(set(rng.sample(candidates, min(samples, len(candidates)))) | {n - 1})
    numeric_cols = [c for c in full.columns if c not in (ACTION_COLUMN, REASON_COLUMN) and full[c].dtype.kind in "fiu"]
    for i in points:
        partial = strategy.evaluate(df.iloc[: i + 1])
        if str(partial[ACTION_COLUMN].iloc[-1]) != str(full[ACTION_COLUMN].iloc[i]):
            raise StrategyError(
                f"Look-ahead 감지: index {i} action 프리픽스={partial[ACTION_COLUMN].iloc[-1]} "
                f"전체={full[ACTION_COLUMN].iloc[i]}"
            )
        for col in numeric_cols:
            a, b = partial[col].iloc[-1], full[col].iloc[i]
            both_nan = pd.isna(a) and pd.isna(b)
            if both_nan or (not pd.isna(a) and not pd.isna(b) and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)):
                continue
            raise StrategyError(f"Look-ahead 감지: index {i} 열 {col} 프리픽스={a} 전체={b}")
