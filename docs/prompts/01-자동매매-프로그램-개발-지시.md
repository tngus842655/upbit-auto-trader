# 업비트 코인 자동매매 프로그램 개발 프로젝트

## 1. 프로젝트 목표

국내 암호화폐 거래소 업비트(Upbit)의 Open API를 이용해서 개인용 코인 자동매매 프로그램을 개발한다.

목표는 단순한 매수/매도 예제 프로그램이 아니라,

- 실시간 시세 수집
- 매매 전략 실행
- 백테스트
- 모의매매
- 실제 주문
- 리스크 관리
- 거래 기록
- 로그 및 오류 관리
- 웹 기반 모니터링

까지 확장 가능한 구조의 자동매매 시스템을 만드는 것이다.

중요:

**처음부터 실제 돈이 거래되는 구조로 구현하지 않는다.**

개발 초기에는 반드시 테스트/모의매매 환경을 우선하고, 실제 주문 기능은 별도의 안전장치와 명시적인 설정을 거쳐 활성화되도록 설계한다.

---

# 2. 기본 기술 스택

다음 기술을 우선적으로 사용한다.

- Python 3.12+
- Upbit Open API
- Upbit WebSocket
- FastAPI
- PostgreSQL 또는 개발 초기에는 SQLite
- SQLAlchemy
- pandas
- numpy
- pydantic
- pytest
- Docker

프론트엔드는 필요하다면 React 또는 Next.js를 사용한다.

단, 프로젝트를 불필요하게 복잡하게 만들지 않는다.

처음에는 다음 구조를 우선 완성한다.

```text
Python
 ├── Upbit API 연동
 ├── WebSocket 실시간 데이터
 ├── 전략 엔진
 ├── 리스크 관리
 ├── 주문 관리
 ├── 백테스트
 ├── 모의매매
 ├── 거래 기록
 └── REST API
```

---

# 3. 반드시 공식 Upbit API 문서를 기준으로 개발

Upbit API의 엔드포인트, 인증 방식, 요청 파라미터, Rate Limit, 주문 방식 등은 추측해서 구현하지 않는다.

반드시 최신 공식 문서를 확인하고 구현한다.

공식 문서:

https://docs.upbit.com/kr/

API가 변경되었을 가능성이 있으면 최신 문서를 우선한다.

---

# 4. 프로젝트 구조

확장성을 고려해서 처음부터 기능을 분리한다.

예를 들어 다음과 같은 구조를 고려한다.

```text
upbit-auto-trader/
│
├── app/
│   ├── main.py
│   │
│   ├── config/
│   │   └── settings.py
│   │
│   ├── exchange/
│   │   ├── upbit_client.py
│   │   ├── websocket.py
│   │   └── models.py
│   │
│   ├── strategy/
│   │   ├── base.py
│   │   ├── indicators.py
│   │   └── example_strategy.py
│   │
│   ├── trading/
│   │   ├── engine.py
│   │   ├── order_manager.py
│   │   └── position_manager.py
│   │
│   ├── risk/
│   │   └── risk_manager.py
│   │
│   ├── backtest/
│   │   ├── engine.py
│   │   └── metrics.py
│   │
│   ├── database/
│   │   ├── models.py
│   │   └── database.py
│   │
│   └── api/
│       └── routes.py
│
├── tests/
│
├── scripts/
│
├── data/
│
├── .env.example
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

실제 구현 과정에서 더 좋은 구조가 있다면 변경해도 되지만, 변경 이유를 설명한다.

---

# 5. API Key 보안

API Key와 Secret Key를 코드에 절대 하드코딩하지 않는다.

반드시 환경변수를 사용한다.

예:

```env
UPBIT_ACCESS_KEY=
UPBIT_SECRET_KEY=
TRADING_MODE=paper
```

Git에 `.env`가 올라가지 않도록 `.gitignore`도 작성한다.

또한 실제 거래 모드에서는 출금 권한을 사용하지 않는다.

---

# 6. 거래 모드

프로그램은 최소한 다음 세 가지 모드를 지원한다.

```text
BACKTEST
PAPER
LIVE
```

### BACKTEST

과거 데이터를 이용해서 전략을 검증한다.

실제 API 주문을 절대 실행하지 않는다.

### PAPER

실시간 데이터를 이용하지만 실제 주문을 넣지 않는다.

가상의 잔고를 사용해서 실제 거래처럼 시뮬레이션한다.

### LIVE

실제 Upbit 주문을 실행한다.

LIVE 모드는 기본적으로 비활성화한다.

예:

```env
TRADING_MODE=PAPER
LIVE_TRADING_ENABLED=false
```

실제 거래를 활성화하려면 명시적인 설정을 두 번 확인하도록 한다.

예:

```env
TRADING_MODE=LIVE
LIVE_TRADING_ENABLED=true
```

두 조건이 모두 충족되지 않으면 실제 주문 API를 호출하지 않는다.

---

# 7. 거래 대상

초기 버전에서는 모든 코인을 자동으로 거래하지 않는다.

우선 설정 파일에서 거래 대상 마켓을 지정할 수 있도록 한다.

예:

```text
KRW-BTC
KRW-ETH
```

추후 여러 종목을 동시에 관리할 수 있도록 설계한다.

---

# 8. 데이터 수집

다음 데이터를 수집할 수 있어야 한다.

- 현재가
- 체결 데이터
- 호가
- OHLCV 캔들
- 거래량
- 주문 상태
- 잔고
- 보유 자산

가능한 경우 실시간 데이터는 WebSocket을 사용한다.

REST API와 WebSocket의 역할을 명확하게 분리한다.

---

# 9. 전략 시스템

전략을 프로그램의 다른 부분과 분리한다.

예를 들어:

```python
class Strategy:
    def generate_signal(self, market_data):
        pass
```

전략은 최소한 다음 신호를 반환할 수 있어야 한다.

```text
BUY
SELL
HOLD
```

향후 전략을 쉽게 추가할 수 있어야 한다.

예:

```text
MovingAverageStrategy
RSIStrategy
MACDStrategy
BreakoutStrategy
CustomStrategy
```

---

# 10. 첫 번째 전략

첫 번째 전략은 복잡한 AI 전략으로 시작하지 않는다.

기본적인 기술적 지표 기반 전략 하나를 구현해서 전체 시스템이 정상적으로 작동하는지 검증한다.

예를 들어:

- 단기 이동평균
- 장기 이동평균
- 거래량
- RSI

등을 활용할 수 있다.

단, 특정 전략이 수익을 낸다고 가정하지 않는다.

전략의 수익성은 반드시 백테스트와 모의매매 결과로 검증한다.

---

# 11. 백테스트 시스템

백테스트 기능을 반드시 만든다.

입력:

```text
종목
기간
시간봉
초기자본
수수료
전략 파라미터
```

출력:

```text
총 수익률
연환산 수익률
승률
총 거래 횟수
평균 수익
평균 손실
Profit Factor
최대 낙폭(MDD)
Sharpe Ratio
수수료
최종 자산
```

가능하면 Buy & Hold와 비교할 수 있도록 한다.

중요:

미래 데이터를 현재 의사결정에 사용하는 Look-ahead Bias가 발생하지 않도록 한다.

거래 수수료와 가능하면 슬리피지도 반영한다.

---

# 12. 리스크 관리

자동매매 프로그램에서 가장 중요한 부분 중 하나다.

다음 기능을 설계한다.

- 거래당 최대 투자금
- 전체 자산 대비 최대 포지션 비율
- 일일 최대 손실
- 최대 연속 손실
- 최대 포지션 수
- 손절
- 익절
- 주문 실패 시 재시도
- API 오류 처리
- WebSocket 연결 끊김 처리
- 비정상 가격 데이터 감지
- 중복 주문 방지

예를 들어 하루 손실이 설정값을 초과하면 해당 날짜에는 신규 거래를 중지한다.

---

# 13. 주문 시스템

주문 실행 부분은 전략과 분리한다.

예:

```text
Strategy
   ↓
Signal
   ↓
Risk Manager
   ↓
Order Manager
   ↓
Upbit API
```

주문 전에 반드시 다음을 확인한다.

```text
1. 거래 모드가 LIVE인가?
2. LIVE_TRADING_ENABLED가 true인가?
3. 잔고가 충분한가?
4. 주문 금액이 최소 주문 조건을 만족하는가?
5. 현재 포지션 상태가 적절한가?
6. 동일한 주문이 이미 존재하지 않는가?
7. 일일 손실 제한을 초과하지 않았는가?
8. API Rate Limit 문제가 없는가?
```

---

# 14. 장애 대응

자동매매는 프로그램이 멈추거나 인터넷이 끊기는 상황을 고려해야 한다.

다음 상황을 처리한다.

- API timeout
- HTTP 오류
- WebSocket disconnect
- 잘못된 API 응답
- 주문 체결 지연
- 주문 실패
- 프로그램 재시작
- DB 연결 실패

프로그램 재시작 후에도 기존 주문과 잔고 상태를 다시 조회해서 내부 상태를 복구할 수 있도록 한다.

---

# 15. 로그

모든 중요한 이벤트를 기록한다.

예:

```text
[INFO] BTC 현재가 수신
[INFO] BUY signal generated
[INFO] Risk check passed
[INFO] Order submitted
[INFO] Order filled
[WARNING] WebSocket disconnected
[ERROR] API request failed
```

거래 로그에는 최소한 다음 정보를 기록한다.

```text
timestamp
market
side
price
volume
order_id
strategy
fee
status
```

---

# 16. 데이터베이스

다음 정보를 저장할 수 있도록 한다.

```text
market_data
orders
trades
positions
balances
strategy_signals
bot_logs
```

나중에 웹 대시보드에서 조회할 수 있도록 구조화한다.

---

# 17. 웹 API

FastAPI를 이용해서 다음 정보를 조회할 수 있도록 한다.

```text
GET /api/status
GET /api/balance
GET /api/positions
GET /api/orders
GET /api/trades
GET /api/performance
GET /api/strategy
```

추후 다음 기능도 추가할 수 있도록 설계한다.

```text
POST /api/bot/start
POST /api/bot/stop
POST /api/bot/pause
```

단, LIVE 거래 시작 API는 보안상 별도의 인증과 확인 절차를 거치도록 한다.

---

# 18. 대시보드

가능하다면 웹 대시보드를 만든다.

다음 정보를 한 화면에서 볼 수 있도록 한다.

```text
현재 봇 상태
현재 자산
현금 잔고
보유 코인
총 수익률
오늘 수익률
MDD
최근 거래
현재 전략 신호
최근 오류
```

---

# 19. 알림

추후 Telegram 또는 Discord 등의 알림 시스템을 쉽게 추가할 수 있는 구조로 만든다.

다음 이벤트를 알림으로 보낼 수 있도록 한다.

```text
매수
매도
주문 체결
손절
일일 손실 제한 도달
API 오류
봇 중지
봇 재시작
```

---

# 20. 테스트

실제 API에 주문을 보내기 전에 최대한 많은 부분을 테스트한다.

최소한 다음 테스트를 작성한다.

```text
API 인증 테스트
시장 데이터 테스트
전략 테스트
백테스트 테스트
리스크 관리 테스트
주문 생성 테스트
중복 주문 방지 테스트
수수료 계산 테스트
포지션 계산 테스트
```

특히 LIVE 모드에서 실수로 주문이 실행되지 않는지 테스트한다.

---

# 21. 개발 순서

한 번에 모든 기능을 만들지 않는다.

다음 순서로 단계적으로 개발한다.

### Phase 1

Upbit API 연결

```text
현재가 조회
잔고 조회
캔들 조회
```

### Phase 2

WebSocket 연결

```text
실시간 체결
실시간 호가
```

### Phase 3

전략 엔진

```text
Indicator
Signal
Strategy
```

### Phase 4

백테스트

```text
과거 데이터
가상 거래
성과 분석
```

### Phase 5

Paper Trading

```text
실시간 데이터
가상 주문
가상 잔고
```

### Phase 6

Risk Management

```text
손절
익절
최대 투자금
일일 손실 제한
```

### Phase 7

실제 주문 모듈

단, 기본적으로 비활성화한다.

### Phase 8

Dashboard

### Phase 9

알림

### Phase 10

Docker / 서버 배포

---

# 22. 중요한 개발 원칙

다음 원칙을 반드시 지킨다.

1. 실제 수익이 발생한다고 가정하지 않는다.
2. 백테스트 결과를 실제 미래 수익으로 해석하지 않는다.
3. 과최적화가 발생하지 않도록 주의한다.
4. Look-ahead Bias를 방지한다.
5. 거래 수수료를 반드시 고려한다.
6. 실제 주문 기능은 기본 비활성화한다.
7. API Key를 코드에 넣지 않는다.
8. 출금 API 권한은 사용하지 않는다.
9. 모든 주문에는 리스크 검사를 거친다.
10. 장애 발생 시 안전한 방향으로 동작하도록 한다.
11. 주문 중복을 방지한다.
12. 모든 거래와 의사결정 과정을 로그로 남긴다.
13. 코드보다 안정성과 검증 가능성을 우선한다.

---

# 23. 작업 방식

너는 이 프로젝트의 시니어 Python 백엔드/퀀트 개발자 역할을 한다.

작업할 때 다음 순서를 따른다.

1. 현재 프로젝트 구조를 먼저 분석한다.
2. 필요한 파일을 확인한다.
3. 구현 전에 설계를 간단하게 설명한다.
4. 작은 단위로 구현한다.
5. 구현 후 테스트한다.
6. 오류가 있으면 직접 수정한다.
7. 다음 단계로 넘어가기 전에 현재 단계가 정상적으로 작동하는지 확인한다.

한 번에 모든 기능을 구현하지 않는다.

각 Phase를 완료할 때마다:

```text
[완료된 기능]
[변경된 파일]
[실행 방법]
[테스트 결과]
[남은 작업]
[주의사항]
```

형태로 정리한다.

---

# 24. 가장 중요한 요구사항

**실제 돈을 잃을 수 있는 자동매매 프로그램이라는 점을 항상 고려한다.**

따라서 처음부터 화려한 매매 전략을 만드는 것보다,

```text
정확한 데이터
↓
정확한 주문 상태
↓
정확한 포지션 관리
↓
정확한 리스크 관리
↓
철저한 백테스트
↓
Paper Trading
↓
소액 Live Trading
```

순서로 시스템을 완성한다.

특히 실제 주문 기능을 구현할 때는 내가 명시적으로 LIVE 모드를 활성화하기 전까지 실제 주문이 절대 실행되지 않도록 한다.

---

# 25. 첫 번째 작업

지금은 전체 프로그램을 한 번에 만들지 말고 **Phase 1부터 시작한다.**

먼저 최신 Upbit 공식 API 문서를 확인하고,

1. 프로젝트 구조 생성
2. Python 환경 구성
3. Upbit API 인증 모듈 구현
4. 현재가 조회 구현
5. 잔고 조회 구현
6. 캔들 조회 구현
7. 환경변수 설정
8. `.gitignore`
9. 기본 테스트
10. README

까지 구현한다.

**이 단계에서는 절대로 실제 매수/매도 주문을 실행하지 않는다.**

구현을 시작하기 전에 현재 환경과 프로젝트 파일을 먼저 확인하고, 필요한 경우 나에게 질문한 후 진행한다.