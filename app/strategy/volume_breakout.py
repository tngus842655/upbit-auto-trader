"""거래량 동반 돌파 전략 (VolumeBreakoutStrategy) — 돌파 계열.

- 매수: 종가 > 직전 ``breakout_window`` 개 캔들의 최고가 **이고**
  거래량 > 직전 ``volume_window`` 개 평균 × ``volume_multiplier``
  (두 조건이 새로 함께 성립한 캔들. 최고가·평균 거래량 모두 현재 캔들을 뺀 과거 값으로 잰다)
- 매도(선택): 종가 < 직전 ``exit_window`` 개 캔들의 최저가 (채널 이탈, 0 이면 끔)

손절·익절·추적 손절은 전략이 아니라 기존 리스크 관리(``RiskConfig`` 의 stop_loss_pct / take_profit_pct /
trailing_stop_pct)가 모든 전략에 똑같이 적용한다 — 전략은 진입가를 모르기 때문이다. ``exit_window=0`` 이면
리스크 규칙으로만 청산되므로 백테스트에서도 리스크 규칙을 켜야 포지션이 정리된다.
"""

from __future__ import annotations

from typing import ClassVar

import pandas as pd

from app.strategy.base import (
    ACTION_COLUMN,
    REASON_COLUMN,
    Strategy,
    StrategyFamily,
    StrategyParams,
    action_series,
    param,
    reasons,
    warmup_mask,
)
from app.strategy.indicators import rising_edge, rolling_high, rolling_low, sma


class VolumeBreakoutParams(StrategyParams):
    breakout_window: int = param(
        20, ge=2, le=500, label="돌파 기간", description="직전 N개 캔들 최고가를 넘으면 돌파",
        help_text="종가가 직전 N개 캔들(현재 캔들 제외)의 최고가를 넘으면 가격 돌파로 봅니다. 보통 20.",
    )
    volume_window: int = param(
        20, ge=2, le=500, label="거래량 평균 기간", description="직전 N개 캔들 평균 (현재 제외)",
        help_text="돌파 캔들의 거래량과 비교할 평균 거래량의 기간입니다. 현재 캔들은 평균에 넣지 않습니다.",
    )
    volume_multiplier: float = param(
        1.5, ge=0.1, le=20.0, label="거래량 배수", description="평균 거래량 대비 최소 배수",
        help_text="돌파 캔들의 거래량이 '평균 × 이 배수'보다 커야 매수합니다. 1.5 = 평균보다 50% 많을 때.\n"
                  "거래량 없는 돌파(속임수 돌파)를 거릅니다.",
    )
    exit_window: int = param(
        10, ge=0, le=500, label="채널 이탈 매도 기간", description="직전 N개 캔들 최저가 아래로 마감하면 매도 (0 = 끔)",
        help_text="종가가 직전 N개 캔들(현재 제외)의 최저가 아래로 내려가면 매도합니다 (터틀 방식 청산).\n"
                  "0 이면 전략 매도 신호가 없고 리스크 관리의 손절·익절·추적 손절로만 청산합니다.",
    )


class VolumeBreakoutStrategy(Strategy):
    name: ClassVar[str] = "volume_breakout"
    description: ClassVar[str] = "가격 + 거래량 돌파"
    family: ClassVar[StrategyFamily] = StrategyFamily.BREAKOUT
    rules: ClassVar[str] = (
        "매수: 종가 > 직전 N개 캔들 최고가 이고 거래량 > 직전 평균 × 배수 (현재 캔들은 기준에서 제외)\n"
        "매도: 종가 < 직전 채널 이탈 기간 최저가 (0 이면 끔)\n"
        "손절·익절·추적 손절: 리스크 관리 설정을 사용 (채널 이탈을 끄면 백테스트에서 리스크 규칙을 켜세요)"
    )
    Params: ClassVar[type[StrategyParams]] = VolumeBreakoutParams
    params: VolumeBreakoutParams

    @property
    def warmup_periods(self) -> int:
        # 기준값(직전 N개)이 N+1 번째 캔들부터 나오고, 새로 성립했는지 보려면 직전 캔들이 하나 더 필요
        p = self.params
        return max(p.breakout_window, p.volume_window, p.exit_window) + 2

    @property
    def indicator_columns(self) -> list[str]:
        cols = ["breakout_high", "volume_ratio"]
        if self.params.exit_window:
            cols.append("exit_low")
        return cols

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        close = df["close"]
        out = df[["close"]].copy()
        out["breakout_high"] = rolling_high(df["high"], p.breakout_window)
        volume_avg = sma(df["volume"].shift(1), p.volume_window)
        out["volume_ratio"] = df["volume"] / volume_avg.where(volume_avg > 0)  # 평균 0 이면 NaN (무한대 방지)
        breakout = (close > out["breakout_high"]).fillna(False)
        surge = (df["volume"] > volume_avg * p.volume_multiplier).fillna(False)

        warm = warmup_mask(df, self.warmup_periods)
        buy = rising_edge(breakout & surge) & ~warm
        sell = pd.Series(False, index=df.index)
        if p.exit_window:
            out["exit_low"] = rolling_low(df["low"], p.exit_window)
            sell = rising_edge((close < out["exit_low"]).fillna(False)) & ~warm
        out[ACTION_COLUMN] = action_series(buy, sell)
        out[REASON_COLUMN] = reasons(
            "",
            (warm, "warmup"),
            (buy, f"{p.breakout_window}캔들 고가 돌파 + 거래량 {p.volume_multiplier:g}배 이상"),
            (sell, f"{p.exit_window}캔들 저가 이탈"),
            (rising_edge(breakout) & ~surge, f"고가 돌파이지만 거래량 {p.volume_multiplier:g}배 미만"),
        )
        return out
