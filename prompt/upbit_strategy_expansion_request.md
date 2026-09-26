# 업비트 자동매매 전략 확장 구현 요청

## 1. 프로젝트 현황

현재 Python 기반 업비트 자동매매 프로그램을 개발하고 있습니다.

현재 구현된 전략은 다음 2개입니다.

1. `ma_cross`
   - 단기/장기 SMA 교차
   - 거래량 조건
   - RSI 필터

2. `rsi`
   - RSI 과매도 구간 진입 시 매수
   - RSI 과매수 구간 진입 시 매도

현재 전략 선택 UI는 다음과 같은 형태입니다.

```text
전략
┌─────────────────────────────────────────┐
│ 신호를 만드는 규칙입니다.                │
│                                         │
│ ma_cross — 단기/장기 SMA 교차 + 거래량·RSI 필터 │
│ rsi      — RSI 과매도 탈출 매수 / 과매수 이탈 매도 │
└─────────────────────────────────────────┘
```

현재 구조를 최대한 유지하면서 추가 전략을 구현해주세요.

---

# 2. 추가할 전략

다음 전략들을 각각 독립적인 전략으로 구현해주세요.

## A. Bollinger Band 전략

전략 ID:

```text
bollinger
```

기본 개념:

- Bollinger Band 하단 접근/이탈 후 밴드 내부로 재진입하면 매수 신호
- 상단 밴드 접근/이탈 후 하락하면 매도 신호
- RSI를 선택적인 추가 필터로 사용할 수 있도록 구성

기본 파라미터:

```text
window = 20
std_dev = 2.0
rsi_window = 14
oversold = 40
overbought = 60
```

매수 예시:

```text
이전 캔들에서 가격 <= 하단 밴드
AND
현재 캔들에서 가격 > 하단 밴드
AND
RSI < oversold
```

매도 예시:

```text
이전 캔들에서 가격 >= 상단 밴드
AND
현재 캔들에서 가격 < 상단 밴드
AND
RSI > overbought
```

단, 기존 프로젝트의 전략 구조에 맞춰 가장 자연스러운 방식으로 구현해주세요.

---

# 3. MACD 전략

전략 ID:

```text
macd
```

기본 파라미터:

```text
fast_period = 12
slow_period = 26
signal_period = 9
```

매수:

```text
MACD가 Signal을 상향 돌파
```

매도:

```text
MACD가 Signal을 하향 돌파
```

선택적인 추가 필터:

```text
MACD > 0
```

가능하다면 UI에서 MACD 0선 필터를 켜고 끌 수 있도록 구현해주세요.

---

# 4. EMA Cross 전략

전략 ID:

```text
ema_cross
```

기본 파라미터:

```text
short_period = 9
medium_period = 21
long_period = 50
```

기본 매수 조건:

```text
EMA 9 > EMA 21
AND
EMA 21 > EMA 50
```

추가 RSI 필터:

```text
RSI > 50
```

매도:

```text
EMA 9 < EMA 21
```

SMA Cross와 별도의 전략으로 취급해주세요.

---

# 5. 거래량 돌파 전략

전략 ID:

```text
volume_breakout
```

목적:

가격 돌파와 거래량 증가가 동시에 발생하는 상황을 포착합니다.

기본 파라미터:

```text
breakout_window = 20
volume_window = 20
volume_multiplier = 1.5
```

매수 조건:

```text
현재 가격 > 최근 N개 캔들의 최고가
AND
현재 거래량 > 평균 거래량 × volume_multiplier
```

매도 조건은 프로젝트의 기존 리스크 관리 구조를 최대한 활용해주세요.

가능하면 다음 옵션을 지원해주세요.

```text
stop_loss
take_profit
trailing_stop
```

---

# 6. ADX Trend 전략

전략 ID:

```text
adx_trend
```

ADX는 방향 자체보다는 추세의 강도를 판단하는 필터로 사용합니다.

기본 파라미터:

```text
adx_window = 14
adx_threshold = 25
```

예시 매수:

```text
기존 추세 조건 만족
AND
ADX > 25
```

가능하면 방향성 지표인 `+DI`, `-DI`도 활용해주세요.

예:

```text
ADX > threshold
AND
+DI > -DI
```

매도:

```text
-DI > +DI
```

또는 기존 매도 조건과 결합할 수 있도록 구조화해주세요.

---

# 7. Stochastic 전략

전략 ID:

```text
stochastic
```

기본 파라미터:

```text
k_period = 14
d_period = 3
oversold = 20
overbought = 80
```

매수:

```text
%K가 %D를 상향 돌파
AND
과매도 영역
```

매도:

```text
%K가 %D를 하향 돌파
AND
과매수 영역
```

---

# 8. Ichimoku 전략

전략 ID:

```text
ichimoku
```

기본 파라미터:

```text
conversion_period = 9
base_period = 26
span_b_period = 52
displacement = 26
```

기본 매수 조건:

```text
가격 > 구름
AND
전환선 > 기준선
```

매도:

```text
가격 < 구름
OR
전환선 < 기준선
```

기존 프로젝트의 캔들 데이터 구조에 맞게 구현해주세요.

---

# 9. OBV 전략

전략 ID:

```text
obv
```

OBV(On Balance Volume)를 이용해 가격과 거래량의 흐름을 확인합니다.

기본 파라미터:

```text
obv_window = 20
```

예시:

```text
OBV 상승 추세
AND
가격 상승/돌파
```

→ 매수

OBV가 하락 추세로 전환되면 매도 신호.

가능하면 OBV 자체의 이동평균을 이용해서 신호를 만들어주세요.

---

# 10. CCI 전략

전략 ID:

```text
cci
```

기본 파라미터:

```text
window = 20
oversold = -100
overbought = 100
```

매수:

```text
CCI가 -100 아래에서 위로 돌파
```

매도:

```text
CCI가 +100 위에서 아래로 돌파
```

---

# 11. Williams %R 전략

전략 ID:

```text
williams_r
```

기본 파라미터:

```text
window = 14
oversold = -80
overbought = -20
```

매수:

```text
Williams %R이 -80 아래에서 위로 돌파
```

매도:

```text
Williams %R이 -20 위에서 아래로 돌파
```

---

# 12. 전략 공통 인터페이스

추가 전략을 단순히 개별 코드로 흩어놓지 말고, 기존 프로젝트 구조에 맞는 공통 인터페이스를 유지해주세요.

가능하다면 다음과 같은 구조를 사용해주세요.

```python
class BaseStrategy:
    def generate_signal(self, df):
        ...
```

각 전략:

```python
class MACDStrategy(BaseStrategy):
    ...

class BollingerStrategy(BaseStrategy):
    ...

class EMACrossStrategy(BaseStrategy):
    ...

class VolumeBreakoutStrategy(BaseStrategy):
    ...

class ADXTrendStrategy(BaseStrategy):
    ...

class StochasticStrategy(BaseStrategy):
    ...

class IchimokuStrategy(BaseStrategy):
    ...

class OBVStrategy(BaseStrategy):
    ...

class CCIStrategy(BaseStrategy):
    ...

class WilliamsRStrategy(BaseStrategy):
    ...
```

현재 `ma_cross`, `rsi`가 이미 구현되어 있으므로 기존 코드를 최대한 재사용하고 기존 전략이 깨지지 않도록 해주세요.

프로젝트의 기존 아키텍처가 위 구조와 다르다면 무조건 위 구조로 변경하지 말고, **현재 프로젝트의 구조를 먼저 분석한 후 가장 자연스러운 방식으로 통합**해주세요.

---

# 13. 전략 선택 UI

현재 전략 선택 Dropdown에 다음 항목을 추가해주세요.

```text
ma_cross — 단기/장기 SMA 교차 + 거래량·RSI 필터
rsi — RSI 과매도 탈출 매수 / 과매수 이탈 매도

bollinger — Bollinger Band 반전
macd — MACD Signal 교차
ema_cross — EMA 추세 교차
volume_breakout — 가격 + 거래량 돌파
adx_trend — ADX 추세 강도
stochastic — Stochastic 과매도/과매수
ichimoku — Ichimoku 구름 돌파
obv — OBV 거래량 추세
cci — CCI 과매도/과매수
williams_r — Williams %R 과매도/과매수
```

전략을 선택하면 해당 전략에서 사용하는 파라미터만 UI에 표시해주세요.

예를 들어 MACD 선택 시:

```text
전략
[ MACD Signal 교차 ▼ ]

fast period
[ 12 ]

slow period
[ 26 ]

signal period
[ 9 ]

0선 필터
[ ✓ ]
```

Bollinger 선택 시:

```text
전략
[ Bollinger Band 반전 ▼ ]

window
[ 20 ]

표준편차
[ 2.0 ]

RSI 필터
[ ✓ ]

RSI window
[ 14 ]

oversold
[ 40 ]

overbought
[ 60 ]
```

---

# 14. Look-ahead Bias 방지

백테스트와 실거래 모두에서 미래 데이터를 참조하지 않도록 해주세요.

특히 다음 지표 계산을 확인해주세요.

```text
shift()
rolling()
EMA
RSI
MACD
Bollinger Band
Ichimoku
```

현재 캔들의 신호를 계산할 때 미래 캔들의 값이 절대 사용되지 않아야 합니다.

---

# 15. 데이터 부족 처리

지표 계산에 필요한 최소 캔들이 확보되지 않은 경우에는 매매 신호를 생성하지 말아주세요.

예:

```python
if len(df) < required_period:
    return HOLD
```

기존 프로젝트에서 `HOLD`, `NONE`, `NO_SIGNAL` 등 다른 상태값을 사용하고 있다면 기존 규칙을 그대로 따라주세요.

---

# 16. 기존 매매 엔진과의 호환

전략에서 직접 주문을 실행하지 말고 다음 구조를 유지해주세요.

```text
전략
 ↓
BUY / SELL / HOLD
 ↓
기존 리스크 관리
 ↓
기존 주문 엔진
 ↓
Upbit API
```

전략 추가 때문에 기존 주문 API, 인증 로직, 잔고 처리 로직 등을 불필요하게 변경하지 마세요.

---

# 17. 수수료 / 슬리피지

백테스트가 있다면 전략별 성과를 비교할 때 반드시 기존 프로그램에서 사용하는 거래 수수료와 슬리피지를 적용해주세요.

전략별 성과 비교에서 수수료가 누락되지 않도록 확인해주세요.

---

# 18. 전략 계열 분류

향후 확장성을 위해 전략을 다음 4가지 계열로 구분할 수 있도록 구조를 고려해주세요.

```text
TREND
 ├─ ma_cross
 ├─ ema_cross
 ├─ macd
 └─ ichimoku

MEAN_REVERSION
 ├─ rsi
 ├─ bollinger
 ├─ stochastic
 ├─ cci
 └─ williams_r

BREAKOUT
 └─ volume_breakout

FILTER
 ├─ adx
 └─ obv
```

이번 작업에서는 전략을 자동으로 선택하는 복잡한 Strategy Router까지 만들 필요는 없습니다.

향후 Strategy Router를 추가하기 쉽도록 구조만 깔끔하게 설계해주세요.

---

# 19. 백테스트 연동

각 전략에 대해 기존 백테스트 시스템에서 다음 결과를 확인할 수 있도록 연동해주세요.

```text
총 거래 횟수
승률
총 수익률
평균 수익률
최대 낙폭(MDD)
최대 연속 손실
수수료
실현 손익
```

가능하다면 같은 기간에 여러 전략을 테스트해서 비교할 수 있는 구조도 고려해주세요.

단, 기존 백테스트 기능이 있다면 해당 기능을 깨뜨리지 않는 것이 최우선입니다.

---

# 20. 구현 순서

다음 순서로 작업해주세요.

## Phase 1

```text
Bollinger
MACD
EMA Cross
Volume Breakout
```

## Phase 2

```text
ADX
Stochastic
CCI
Williams %R
```

## Phase 3

```text
Ichimoku
OBV
```

## Phase 4

```text
UI 개선
전략별 파라미터 설정
백테스트 연동
전략별 성과 비교
```

---

# 21. 테스트

각 전략에 대해 최소한 다음 테스트를 추가해주세요.

### 지표 계산 테스트

- 정상적인 OHLCV 데이터에서 지표가 정상 계산되는지
- 데이터가 부족할 때 오류가 발생하지 않는지
- NaN 처리 여부가 적절한지

### 신호 테스트

- BUY 조건에서 BUY가 발생하는지
- SELL 조건에서 SELL이 발생하는지
- 조건이 만족되지 않을 때 HOLD가 발생하는지

### Look-ahead 테스트

미래 캔들을 추가했을 때 이미 확정된 과거 신호가 변경되지 않는지 확인해주세요.

### 기존 전략 회귀 테스트

기존:

```text
ma_cross
rsi
```

전략의 기존 동작이 변경되지 않았는지 확인해주세요.

---

# 22. 작업 방식

먼저 현재 프로젝트의 파일 구조와 기존 `ma_cross`, `rsi` 전략 구현을 분석해주세요.

그 후 다음 순서로 진행해주세요.

1. 현재 전략 아키텍처 분석
2. 기존 전략의 구현 방식 파악
3. 추가 전략을 어디에 구현할지 결정
4. 기존 코드와 충돌 가능성 확인
5. Phase 1 구현
6. 테스트 실행
7. 기존 전략 정상 작동 여부 확인
8. Phase 2 구현
9. 테스트 실행
10. Phase 3 구현
11. UI 및 백테스트 연동
12. 전체 테스트 실행

기존 코드의 동작을 임의로 변경하지 말고, 필요한 부분만 최소한으로 수정해주세요.

특히 실거래 주문 코드와 API 인증/주문 로직은 전략 추가 작업 때문에 변경하지 않는 것을 원칙으로 합니다.

---

# 23. 최종 목표

최종적으로 전략 선택 Dropdown에서 기존 2개 + 신규 전략들이 정상적으로 표시되어야 합니다.

```text
전략
├─ ma_cross
├─ rsi
├─ bollinger
├─ macd
├─ ema_cross
├─ volume_breakout
├─ adx_trend
├─ stochastic
├─ ichimoku
├─ obv
├─ cci
└─ williams_r
```

전략을 선택하면 해당 전략에 필요한 파라미터 UI만 동적으로 표시되어야 합니다.

또한 동일한 전략 로직을 다음 두 환경에서 사용할 수 있도록 해주세요.

```text
백테스트
   ↓
전략 로직
   ↓
BUY / SELL / HOLD

실거래
   ↓
동일한 전략 로직
   ↓
BUY / SELL / HOLD
   ↓
리스크 관리
   ↓
주문 엔진
   ↓
Upbit API
```

**핵심 원칙은 기존 자동매매 시스템을 최대한 보존하면서 전략 모듈을 확장 가능하게 만드는 것입니다.**

구현 과정에서 현재 프로젝트 구조와 요구사항이 충돌하는 부분이 있다면 임의로 기존 기능을 변경하지 말고, 먼저 가장 안전한 통합 방식을 선택해주세요.
