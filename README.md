# upbit-auto-trader

업비트(Upbit) Open API 기반 개인용 코인 자동매매 시스템. 실시간 시세 수집 → 전략 → 리스크 관리 → 주문 →
기록 → 웹 모니터링까지 단계적으로 확장하는 구조이며, 현재는 **Phase 7(실제 주문 모듈, 기본 비활성)** 까지 완료된 상태다.

> **실제 돈이 오가는 프로그램이다.** 기본 모드는 PAPER(모의)이고, 실제 주문은 `TRADING_MODE=LIVE` 와
> `LIVE_TRADING_ENABLED=true` 가 **둘 다** 설정되고, 실행 시 `--confirm-live REAL-MONEY` 까지 붙여야만 실제 주문이 나간다.
> 주문 API 를 부르는 코드는 `UpbitClient.allow_orders`(같은 이중 플래그로만 켜짐)와 `live_guard` 두 잠금을 추가로 거친다.

## 안전 원칙

- API Key 는 `.env` 에만 두고 절대 커밋하지 않는다 (`.gitignore` 적용). 출금 권한은 발급하지 않는다.
- 거래 모드 `BACKTEST` / `PAPER` / `LIVE`. 기본값 `PAPER`, `LIVE_TRADING_ENABLED` 기본값 `false`.
- 실제 주문 코드는 항상 `app/trading/live_guard.py` 의 `assert_live_order_allowed()` 를 거친다 (테스트로 보장).
- 모든 요청은 공식 Rate Limit(시세 초당 10회/IP, 자산 초당 30회/포켓)을 클라이언트 쪽에서 먼저 지킨다.
- 4xx·418 은 재시도하지 않고, 네트워크 오류·5xx·429 만 GET 에 한해 지수 백오프로 재시도한다.
- 키·토큰은 로그에 남기지 않는다. 상태 요약에는 Access Key 앞 4자만 표시한다.

## 현재 상태 (2026-09-24)

| Phase | 내용 | 상태 |
| --- | --- | --- |
| 1 | Upbit API 연결: 인증, 현재가, 잔고, 캔들, 설정, 테스트 | **완료** |
| 2 | WebSocket 실시간 현재가·체결·호가·캔들 (자동 재연결) | **완료** |
| 3 | 전략 엔진: 지표, BUY/SELL/HOLD 신호, 전략 인터페이스, Look-ahead 자동 검사, 첫 전략(이동평균 교차) | **완료** |
| 4 | 백테스트: 다음 캔들 시가 체결, 수수료·슬리피지, 손절·익절, 성과 지표(수익률·연환산·MDD·Sharpe·승률·PF), Buy & Hold 비교, 결과 저장 | **완료** |
| 5 | Paper Trading: 캔들 경계마다 닫힌 캔들 확정 → 전략 → 리스크 → 가상 체결(호가 기준), SQLite 기록, 재시작 복구, `run`/`status` | **완료** |
| 6 | 리스크 관리: 거래당 최대 투자금, 자산 대비 포지션 상한, 최대 포지션 수, 일일 최대 손실, 최대 연속 손실, 손절·익절·추적 손절(실시간 감시), 시세 괴리 방어, 재진입 대기 — 백테스트·모의매매 공용 | **완료** |
| 7 | 실제 주문 모듈: 주문 API(생성·테스트·조회·취소·주문 가능 정보), LiveBroker(시장가, identifier 멱등, 폴링·취소, 잔고 동기화), 3중 안전장치, 엔진 하트비트·명령 큐(pause/resume/stop/halt), `order-test`/`control` 명령 — 기본 비활성 | **완료** |
| 8 | 대시보드 (FastAPI) | 예정 |
| 9 | 알림 (Telegram/Discord) | 예정 |
| 10 | Docker / 서버 배포 | 예정 |

## 요구 사항

- Python 3.12 이상 (개발·검증 환경: Python 3.13.15, Windows 11)
- 업비트 API Key — 잔고 조회에만 필요. 현재가·캔들은 키 없이 동작한다.

## 설치

### 방법 A: [uv](https://docs.astral.sh/uv/) 사용 (권장 — 시스템에 Python 이 없어도 된다)

```bash
uv python install 3.13
uv venv --python 3.13 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements-dev.txt
```

### 방법 B: 표준 Python

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
```

> Windows 참고: `python` 명령이 Microsoft Store 스텁(실행해도 `Python` 만 출력)이면 위 명령이 조용히 실패한다.
> 이 경우 방법 A 를 쓰거나, 아래 예시처럼 `.venv\Scripts\python.exe` 를 직접 호출한다.
>
> 이 개발 PC 에서는 uv 와 Python 3.13 이 `C:\Workspace\tools\uv\uv.exe`, `C:\Workspace\tools\python\` 에 있고
> `.venv` 는 그 Python 을 가리킨다. venv 를 다시 만들 때는
> `C:\Workspace\tools\uv\uv.exe venv --python C:\Workspace\tools\python\cpython-3.13.15-windows-x86_64-none\python.exe .venv`.

## 설정

```bash
copy .env.example .env      # Windows
```

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `UPBIT_ACCESS_KEY` / `UPBIT_SECRET_KEY` | 없음 | 업비트 > 마이페이지 > Open API 관리. 권한은 [자산조회]·[주문조회]만, 실행 PC 의 공인 IP 등록 필요 |
| `TRADING_MODE` | `PAPER` | `BACKTEST` / `PAPER` / `LIVE` (대소문자 무관) |
| `LIVE_TRADING_ENABLED` | `false` | `LIVE` 모드에서도 이 값이 `true` 가 아니면 실제 주문 차단 |
| `MARKETS` | `KRW-BTC,KRW-ETH` | 거래 대상 마켓, 쉼표 구분 |
| `STRATEGY_NAME` | `ma_cross` | 전략 이름 (`ma_cross`, `rsi`) |
| `STRATEGY_PARAMS` | 없음 | 전략 파라미터. `short_window=20,long_window=60` 또는 JSON `{"short_window": 20}` |
| `CANDLE_INTERVAL` | `60m` | 전략이 보는 캔들 단위 (`1m`~`240m`, `1d` 등) |
| `DATABASE_URL` | `sqlite:///./data/trader.db` | 모의매매 기록 DB (신호·주문·체결·포지션·잔고·로그·캔들) |
| `PAPER_INITIAL_CASH` / `PAPER_FEE_RATE` / `PAPER_SLIPPAGE_RATE` | `1000000` / `0.0005` / `0.0005` | 모의 계좌 초기 현금, 수수료율, 슬리피지 |
| `POSITION_FRACTION` | `1.0` | 매수 시 현금 사용 비율 |
| `WARMUP_CANDLES` / `SNAPSHOT_INTERVAL_SECONDS` | `300` / `300` | 시작 시 받아 둘 캔들 수, 자산 스냅샷 주기(초) |
| `RISK_MAX_ORDER_AMOUNT` | 없음 | 거래당 최대 투자금 KRW (비우면 무제한) |
| `RISK_MAX_POSITION_RATIO` | `1.0` | 전체 자산 대비 코인 평가액 상한 |
| `RISK_MAX_OPEN_POSITIONS` | `1` | 동시 보유 최대 마켓 수 |
| `RISK_DAILY_LOSS_LIMIT_PCT` | `0.03` | 당일(KST) 시작 자산 대비 손실 한도. 넘으면 그날 신규 진입 중지 (`off` 로 끔) |
| `RISK_MAX_CONSECUTIVE_LOSSES` | `3` | 연속 손실 한도. 넘으면 그날 신규 진입 중지 (`off` 로 끔) |
| `RISK_STOP_LOSS_PCT` / `RISK_TAKE_PROFIT_PCT` | `0.05` / 없음 | 평균 매수가 대비 손절·익절 비율 |
| `RISK_TRAILING_STOP_PCT` | 없음 | 보유 중 최고가 대비 하락률로 청산 |
| `RISK_PRICE_DEVIATION_LIMIT` | `0.10` | 신호 캔들 종가와 현재 시세의 괴리가 이보다 크면 진입 안 함 |
| `RISK_COOLDOWN_SECONDS` | `0` | 청산 후 같은 마켓 재진입 대기(초) |
| `LOG_LEVEL` / `LOG_DIR` | `INFO` / `logs` | 로그 레벨, 로그 폴더 (`logs/trader.log`, 10MB×5 회전) |
| `UPBIT_API_URL` | `https://api.upbit.com` | REST 엔드포인트 |
| `HTTP_TIMEOUT_SECONDS` / `HTTP_MAX_RETRIES` | `10` / `3` | HTTP 타임아웃(초), GET 재시도 횟수 |

## 실행

프로젝트 루트(`upbit-auto-trader/`)에서 실행한다. 아래 명령은 어떤 것도 주문을 만들지 않는다.

```bash
.venv\Scripts\python.exe -m app.main check                      # 설정·현재가·캔들·(키가 있으면) 잔고 점검
.venv\Scripts\python.exe -m app.main ticker KRW-BTC KRW-XRP     # 현재가
.venv\Scripts\python.exe -m app.main candles KRW-BTC --interval 15m --count 10
.venv\Scripts\python.exe -m app.main balance                    # 잔고 (API Key 필요)
.venv\Scripts\python.exe -m app.main stream KRW-BTC KRW-ETH --types trade,orderbook,ticker --seconds 10   # WebSocket 실시간
.venv\Scripts\python.exe -m app.main stream KRW-BTC --types candle.1m --seconds 30
.venv\Scripts\python.exe -m app.main signal KRW-BTC --strategy ma_cross --interval 60m --count 300   # 전략 신호 계산
.venv\Scripts\python.exe -m app.main signal KRW-ETH --strategy rsi --interval 15m --params window=10,oversold=25
.venv\Scripts\python.exe scripts\fetch_candles.py KRW-BTC --interval 60m --start 2026-01-01   # 과거 캔들 CSV → data/
.venv\Scripts\python.exe -m app.main backtest KRW-BTC --interval 60m --start 2026-06-01 --strategy ma_cross           # 백테스트
.venv\Scripts\python.exe -m app.main backtest KRW-BTC --interval 60m --start 2026-01-01 --end 2026-09-01 --stop-loss 0.05 --take-profit 0.10 --params short_window=10,long_window=30
.venv\Scripts\python.exe -m app.main backtest KRW-BTC --interval 60m --start 2026-06-01 --env-risk                      # .env 의 RISK_* 규칙 그대로
.venv\Scripts\python.exe -m app.main backtest KRW-BTC --interval 60m --start 2026-06-01 --daily-loss-limit 0.02 --max-consecutive-losses 2 --trailing-stop 0.03 --max-order-amount 300000
.venv\Scripts\python.exe -m app.main backtest KRW-BTC --interval 60m --start 2026-01-01 --csv data/KRW-BTC_60m.csv --capital 500000
.venv\Scripts\python.exe -m app.main run --interval 60m                        # 모의매매 (PAPER 전용, Ctrl+C 로 종료)
.venv\Scripts\python.exe -m app.main run KRW-BTC --interval 1m --duration 600   # 10분만 실행
.venv\Scripts\python.exe -m app.main status --limit 5                          # 엔진 상태(하트비트)·계좌·포지션·신호·주문·로그
.venv\Scripts\python.exe -m app.main control pause                             # 실행 중 엔진 제어: pause / resume / stop / halt / resume-risk
.venv\Scripts\python.exe -m app.main order-test KRW-BTC --amount 5000          # 주문 테스트 API 로 키·권한 검증 (실제 주문 없음)
.venv\Scripts\python.exe -m app.main run --confirm-live REAL-MONEY             # LIVE (TRADING_MODE=LIVE + LIVE_TRADING_ENABLED=true 필요)
```

`run` 은 `TRADING_MODE=PAPER` 에서만 동작한다. 시작 시 DB 에 계좌가 있으면 현금·포지션·처리 이력을 복구해 이어서 돌고,
캔들 경계(예: 매시 정각 + 3초)마다 REST 로 닫힌 캔들을 확정해 전략을 돌린다. 가상 체결은 WebSocket 호가의
최우선 매도/매수가에 슬리피지·수수료를 더해 즉시 이뤄지며, 모든 신호·주문·체결·포지션·자산 스냅샷·이벤트가 SQLite 에 남는다.

`backtest` 는 API 로 받은 캔들을 `data/cache/` 에 저장해 다음 실행에서 재사용하고, 결과를 `data/backtests/<시각>_<마켓>_<단위>_<전략>/` 에 `summary.json`, `trades.csv`, `equity.csv`, `signals.csv` 로 남긴다(`--no-save` 로 생략). 출력 예:

```
=== 백테스트: ma_cross {...} / KRW-BTC 60m ===
  기간 2026-06-01 00:00 ~ 2026-09-24 11:00 KST (115.5일, 캔들 2769개)
  초기자본 1,000,000 KRW, 수수료 0.050%, 슬리피지 0.050%, 현금 사용 100%, 손절 없음, 익절 없음
  지표                     전략         Buy & Hold
  최종 자산             910,473          1,064,664
  총 수익률              -8.95%             +6.47%
  최대 낙폭(MDD)        -13.68%            -18.88%
  Sharpe                  -2.30               0.72
  거래 11회 (승 4 / 패 7), 승률 36.36%, Profit Factor 0.36, 기대값 -8,139 KRW/거래, 최대 연속 손실 3회
```

`--params` 와 `STRATEGY_PARAMS` 는 `key=value,key=value` 형식(권장)과 JSON 객체를 모두 받는다. JSON 을 PowerShell 5.1 에서
쓰려면 `--params '{\"window\": 10}'` 처럼 작은따옴표 안에 `\"` 로 써야 한다(`'{"window": 10}'` 는 따옴표가 사라져 깨진다).

`signal` 출력 예 (닫힌 캔들만 사용, Look-ahead 검사 후 출력):

```
=== 전략 신호: ma_cross {'short_window': 20, 'long_window': 60, ...} / KRW-BTC 60m ===
  캔들 301개 (2026-09-11 22:00 ~ 2026-09-24 10:00 KST, 닫힌 캔들만)
  마지막 캔들 종가 115,661,000  →  신호 [HOLD]
    sma_short      115,864,100.0000
    sma_long       115,990,066.6667
    volume_ratio   0.6577
    rsi            47.8636
=== 구간 내 BUY/SELL 신호 4개 (최근 4개) ===
  2026-09-21 16:00  BUY     111,210,000  골든크로스 SMA20>SMA60
  2026-09-24 08:00  SELL    115,908,000  데드크로스 SMA20<SMA60
Look-ahead 검사 통과. 이 신호는 참고용이며 수익을 보장하지 않습니다.
```

`stream` 출력 예 (S = 스냅샷, R = 실시간):

```
[체결 S] KRW-BTC   매수    115,518,000 x 0.00017624   11:26:05
[호가 S] KRW-BTC   매도1    115,518,000 (0.1211)  매수1    115,517,000 (0.7349)  스프레드 1,000
[현재가 R] KRW-BTC      115,518,000  -0.34%
[캔들 R] KRW-BTC   candle.1m   11:26:00 O 115,518,000 H 115,518,000 L 115,518,000 C 115,518,000 V 0.0480
```

캔들 단위: `1s, 1m, 3m, 5m, 10m, 15m, 30m, 60m(1h), 240m(4h), 1d, 1w, 1M, 1y`.

`check` 출력 예:

```
=== 현재가 ===
  KRW-BTC         115,660,000  전일대비 -0.21%  24h 거래대금 1,507.4억  체결시각(KST) 2026-09-24 11:00:31
=== 캔들 (KRW-BTC 60m, 최신순) ===
  2026-09-24 11:00:00     115,661,000    115,675,000    115,660,000    115,660,000         0.0499
=== 잔고 === API Key 가 없어 건너뜀 (.env 에 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 설정)
```

## 테스트

```bash
.venv\Scripts\python.exe -m pytest          # 네트워크 없이 동작 (httpx MockTransport)
.venv\Scripts\ruff.exe check app tests scripts
```

테스트 범위: JWT 생성 규칙(HS512, query_hash), 설정·LIVE 이중 안전장치, 마켓 코드 파싱, Rate Limit 그룹 매핑과
슬라이딩 윈도우, `Remaining-Req` 처리, 요청 URL 형식(배열 파라미터 `[]` 보존), 응답 모델 파싱, 오류 매핑
(400/401/403/404/418/429/5xx), 재시도·백오프, 캔들 페이지네이션, 클라이언트에 주문 메서드가 없음,
WebSocket 요청 형식·재연결·치명적 오류 중단, 지표 손계산 대조와 인과성, 전략 신호 위치·필터·워밍업, 모든 전략의 Look-ahead 검사,
캔들 DataFrame 검증·CSV 왕복·미완성 캔들 제거.

## 프로젝트 구조

```
upbit-auto-trader/
├── app/
│   ├── main.py                 # CLI 진입점 (check / ticker / candles / balance / stream / signal)
│   ├── config/settings.py      # pydantic-settings, TradingMode, LIVE 이중 안전장치
│   ├── core/
│   │   ├── exceptions.py       # TraderError → UpbitError → API/네트워크/응답 예외 계층
│   │   └── logging.py          # 콘솔 + 회전 파일 로그
│   ├── exchange/
│   │   ├── auth.py             # JWT(HS512) + query_hash 생성
│   │   ├── rate_limiter.py     # 그룹별 슬라이딩 윈도우, Remaining-Req 반영
│   │   ├── models.py           # Market / Ticker / Candle / Account / CandleInterval
│   │   ├── upbit_client.py     # httpx 비동기 REST 클라이언트 (시세·캔들·잔고), 재시도, 페이지네이션
│   │   ├── ws_models.py        # WebSocket 메시지 모델 (WsTicker / WsTrade / WsOrderbook / WsCandle)
│   │   └── websocket.py        # WebSocket 클라이언트: 구독 요청, 자동 재연결(지수 백오프), PING 유지
│   ├── strategy/
│   │   ├── base.py             # Action(BUY/SELL/HOLD), Signal, Strategy 인터페이스, check_no_lookahead()
│   │   ├── indicators.py       # SMA/EMA/RSI/MACD/볼린저/ATR/돌파/교차 — 전부 인과적(causal)
│   │   ├── data.py             # 캔들 DataFrame 규약, CSV 입출력, 무결성 검증, 미완성 캔들 제거, 이상치 감지
│   │   ├── ma_cross.py         # 첫 전략: 이동평균 교차 + 거래량·RSI 필터
│   │   └── rsi.py              # 두 번째 예시 전략: RSI 평균회귀
│   ├── trading/
│   │   ├── portfolio.py        # 모의 계좌: 현금·포지션·수수료·왕복 거래 기록 (백테스트·모의매매 공용)
│   │   ├── orders.py           # Order/OrderRequest 모델, PaperBroker(호가 기준 가상 체결, client_id 중복 방지)
│   │   ├── market_state.py     # 마켓별 닫힌 캔들 롤링 저장 + 현재가·호가 상태
│   │   ├── engine.py           # TradingEngine: 캔들 경계 확정 → 전략 → 리스크 → 브로커 → DB, 재시작 복구, 하트비트·명령 큐
│   │   ├── live_broker.py      # LiveBroker: 실제 시장가 주문 (주문 가능 정보 확인, identifier, 폴링·취소, 거래소 잔고 동기화)
│   │   └── live_guard.py       # 실제 주문 관문 (Phase 7 에서 사용)
│   ├── risk/
│   │   ├── config.py           # RiskConfig: 모든 리스크 수치 (나중에 DB·웹 입력으로 교체 가능)
│   │   ├── manager.py          # RiskManager: 진입 심사 evaluate / 청산 감시 check_exits / 결과 반영 record_trade
│   │   └── base.py             # RiskPolicy 인터페이스 + BasicRiskManager(최소판)
│   ├── database/
│   │   ├── models.py           # strategy_signals / orders / trades / round_trips / positions / accounts / balances / bot_logs / market_data
│   │   │                       #   + engine_status(하트비트) / bot_commands(명령 큐) — 대시보드(Phase 8) 이음새
│   │   ├── database.py         # SQLAlchemy 엔진·세션 (SQLite WAL)
│   │   └── repository.py       # 저장·조회·재시작 복구
│   ├── backtest/
│   │   ├── engine.py           # BacktestConfig / BacktestEngine: 다음 캔들 시가 체결, 슬리피지, 손절·익절, B&H 벤치마크
│   │   ├── metrics.py          # 총수익률·CAGR·MDD·변동성·Sharpe·Sortino·승률·PF·기대값·연속 손실·노출
│   │   ├── report.py           # 콘솔 보고서, summary.json / trades.csv / equity.csv / signals.csv 저장
│   │   └── loader.py           # CSV 또는 API(+data/cache) 에서 닫힌 캔들 로드
│   └── api/                    # Phase 8 자리
├── scripts/fetch_candles.py    # 과거 캔들 CSV 수집
├── tests/                      # pytest (네트워크 불필요)
├── docs/upbit-api-notes.md     # 공식 문서에서 확정한 API 사양 요약 (URL·갱신일 포함)
├── data/  logs/                # 런타임 산출물 (git 제외)
├── .env.example  .gitignore  requirements.txt  requirements-dev.txt  pyproject.toml
```

프롬프트의 예시 구조와 달라진 점:

- `app/core/` 추가 — 예외·로깅처럼 모든 모듈이 공유하는 기반을 한곳에 둔다.
- `app/trading/live_guard.py` 를 Phase 1 에 선반영 — 실제 주문 관문을 먼저 고정하고 테스트로 잠근다.
- `Dockerfile` / `docker-compose.yml` 은 Phase 10 에서 작성 — 이 개발 PC 에 Docker 가 없어 검증할 수 없는 파일은 넣지 않는다.

## 설계 메모 (Phase 1)

요청 한 건의 흐름:

1. 파라미터 정규화(`None` 제거, bool → `true/false`, 순서 유지)
2. 해시용 문자열 `unquote(urlencode(params, doseq=True))` 와 전송용 문자열(값 URL 인코딩, 배열 이름의 `[]` 보존)을 각각 생성
3. 인증이 필요하면 JWT 생성 (`query_hash` = SHA512(해시용 문자열)); 키가 없으면 요청 전에 `ConfigError`
4. 경로로 Rate Limit 그룹을 정하고 `RateLimiter.acquire(group)` 로 대기
5. httpx 전송 → `Remaining-Req` 헤더 반영 → 2xx 는 pydantic 모델로 변환, 그 외는 상태 코드별 예외로 매핑
6. 재시도 대상(네트워크 오류·5xx·429, GET 만)이면 0.5s·1s·2s… 백오프 후 반복, 아니면 즉시 예외

WebSocket(Phase 2) 흐름:

1. `Subscription` 목록으로 요청 배열 생성: `[{"ticket"}, {"type","codes",...}, {"format":"DEFAULT"}]`
2. 연결 후 요청 전송 → 스냅샷(SNAPSHOT) 1회 + 실시간(REALTIME) 스트림 수신, `{"status":"UP"}` 은 건너뜀
3. 라이브러리 PING(30초)으로 120초 유휴 종료 방지, 120초 무수신·끊김·네트워크 오류 시 1→2→4…30초 백오프 재연결 후 재구독
4. 서버 요청 오류 중 `WRONG_FORMAT`/`INVALID_AUTH` 등은 재연결해도 같으므로 즉시 예외, `TOO_MANY_REQUEST` 는 백오프 재연결
5. REST 는 스냅샷·과거·계좌 조회, WebSocket 은 실시간 스트림 — 역할을 섞지 않는다

전략 엔진(Phase 3) 원칙:

1. 전략 입력은 **닫힌 캔들만** 담은 DataFrame(`time` UTC index, `open/high/low/close/volume/value`). 진행 중인 캔들은 `drop_unclosed()` 로 제거한다.
2. 전략은 `evaluate(df)` 로 전 구간의 지표와 `action` 열을 벡터 연산으로 만들고, `generate_signal(df)` 는 마지막 행을 `Signal` 로 돌려준다. 백테스트·모의매매·실거래가 같은 코드를 쓴다.
3. 지표는 `rolling`/`ewm` 같은 인과적 연산만 쓴다. `check_no_lookahead()` 가 "앞부분만 잘라 계산한 값 == 전체를 계산한 같은 위치 값" 을 무작위 표본으로 검사해 미래 참조를 잡아낸다(테스트에서 모든 등록 전략에 실행).
4. 전략은 포지션을 모른다. "지금 매수/매도 조건인가" 만 답하고, 보유 여부·중복 주문·손절은 리스크·주문 계층(Phase 6~7)이 판단한다.
5. 파라미터는 pydantic 으로 검증하며(오타 키 거부, 단기 < 장기 등) 과최적화를 피하기 위해 기본값을 단순하게 둔다. 어떤 전략도 수익을 보장하지 않는다.

새 전략 추가: `Strategy` 상속 → `name`, `Params`, `warmup_periods`, `evaluate()` 구현 → `app/strategy/__init__.py` 의 `STRATEGIES` 에 등록. 등록만 하면 공통 테스트(Look-ahead·워밍업)가 자동 적용된다.

백테스트(Phase 4) 체결 규칙:

1. 캔들 i 가 닫힌 뒤 나온 신호는 **캔들 i+1 시가** 에 체결한다(종가 체결 금지). 매수 = 시가×(1+슬리피지), 매도 = 시가×(1−슬리피지), 편도 수수료(기본 0.05%)를 매번 뺀다.
2. 손절·익절은 보유 중 캔들의 저가·고가로 판정하고, 같은 캔들에서 둘 다 닿으면 손절을 먼저 본다. 손절가 아래로 갭 하락하면 시가 체결. 익절은 지정가로 보고 슬리피지를 적용하지 않는다.
3. 롱 온리, 피라미딩 없음: 보유 중 BUY 와 미보유 SELL 은 무시하고 횟수만 센다. 마지막 캔들에서 보유 중이면 종가로 청산해 성과를 확정한다.
4. Buy & Hold 는 첫 캔들 시가 전액 매수(같은 수수료·슬리피지) → 마지막 종가 청산으로 같은 기준에서 비교한다.
5. 연환산 계수는 `365.25일 / 캔들 길이`(연중무휴), 무위험 수익률 0. 실행 전 전략 Look-ahead 검사를 강제한다.

모의매매(Phase 5) 동작:

1. 시작: DB 에서 계좌·포지션·처리한 주문의 client_id 를 복구하고, 과거 캔들 `WARMUP_CANDLES` 개를 받아 둔다.
2. 시세: WebSocket `ticker` + `orderbook`(1호가) 을 구독해 최신 체결가·최우선 호가를 유지한다. 시세가 30초 이상 오래되면 REST 현재가로 보정한다.
3. 캔들 확정: 캔들 경계 + `CANDLE_GRACE_SECONDS` 마다 REST 로 최근 캔들을 다시 받아 **닫힌 캔들만** 합친다. WebSocket 캔들 스트림은 체결이 있을 때만 오고 재연결 중 빠질 수 있어 확정 기준으로 쓰지 않는다.
4. 신호 → 리스크(보유 여부·예산) → 가상 체결(최우선 매도/매수가 × 슬리피지, 수수료) → `orders`/`trades`/`positions`/`accounts`/`balances` 기록.
5. 중복 방지 2단계: 같은 (마켓·단위·전략·캔들 시각) 신호는 DB 유니크 제약으로 한 번만 기록·처리되고, 브로커는 같은 client_id 주문을 거부한다.
6. 종료(Ctrl+C·`--duration`)와 재시작 사이에 상태가 DB 에 있으므로 프로그램을 껐다 켜도 계좌가 이어진다.

리스크 관리(Phase 6) 규칙 — `RiskManager` 하나를 백테스트와 모의매매가 같이 쓴다:

1. 진입 심사 순서: 긴급 정지 → 당일 잠금(일일 손실·연속 손실) → 이미 보유 → 최대 포지션 수 → 재진입 대기 → 시세 괴리 → 예산(현금×비율, 거래당 상한, 자산 대비 상한) → 최소 주문 금액. 거부 사유는 로그·DB 에 남는다.
2. 청산 감시: 손절 → 익절 → 추적 손절 순. 백테스트는 캔들 저가·고가로, 모의매매는 1초마다 현재 호가 중간값으로 판정해 즉시 매도 주문을 낸다.
3. 일일 손실은 **당일(KST) 시작 자산 대비 평가액**(미실현 포함)으로 재며, 한도를 넘으면 그날은 신규 진입만 막고 청산은 계속 허용한다. 날짜가 바뀌면 카운터와 잠금이 풀린다.
4. 재시작 시 오늘 청산된 거래를 DB 에서 다시 읽어 연속 손실·당일 손익을 복구한다.
5. 백테스트 기본은 규칙 없음(순수 전략 성과), `--env-risk` 또는 개별 플래그로 규칙을 켠다.

실제 주문(Phase 7) 안전장치와 동작:

1. 세 개의 잠금: `.env` 이중 플래그(`TRADING_MODE=LIVE` + `LIVE_TRADING_ENABLED=true`) → 실행 시 `--confirm-live REAL-MONEY` → `UpbitClient.allow_orders`(같은 이중 플래그로만 켜짐) + `live_guard`. 하나라도 빠지면 주문 API 호출 전에 막힌다.
2. 주문 전 `GET /v1/orders/chance` 로 페어 상태·최소 주문 금액·거래소 잔고를 확인하고, 예산은 거래소 잔고 안으로 자른다.
3. 시장가만 사용: 매수 `ord_type=price`(총액 KRW), 매도 `ord_type=market`(수량). 업비트 `identifier` 에 신호 기반 client_id 를 넣어 같은 신호가 두 번 나가지 않게 하고, 응답을 못 받으면 identifier 로 조회해 중복 주문을 막는다. 실패한 identifier 는 재사용 불가라 재시도마다 접미사를 바꾼다.
4. 체결은 `GET /v1/order` 를 폴링해 확인(기본 30초). 미체결이면 취소 접수 후 부분 체결만 반영한다. 체결 금액·수량·수수료는 거래소 응답 그대로 계좌에 반영한다.
5. 시작 시와 스냅샷 주기마다 `GET /v1/accounts` 로 내부 계좌를 거래소 기준으로 동기화한다. 실거래 기록은 같은 DB 에 `mode=live` 로 구분해 남는다.
6. 출금 API 는 어떤 경우에도 구현하지 않는다(테스트로 보장).

대시보드 이음새(Phase 8 대비): 엔진은 `engine_status` 테이블에 10초마다 하트비트(상태 RUNNING/PAUSED/STOPPED, WebSocket 상태, 마지막 시세·캔들·거래 시각, 평가액, 리스크 상태)를 쓰고, `bot_commands` 큐를 2초마다 읽어 pause(신규 매수 중단)/resume/stop/halt(긴급 정지)/resume_risk 를 처리한다. 웹 서버는 이 두 테이블만 읽고 쓰면 되므로 엔진과 완전히 분리된 프로세스로 둘 수 있다. 설정 변경은 Phase 8 에서 `bot_settings` 테이블에 저장해 다음 캔들부터 반영하는 방식으로 붙인다.

자세한 API 사양(엔드포인트, 파라미터, 에러 코드, Rate Limit 표)은 [docs/upbit-api-notes.md](docs/upbit-api-notes.md).

## 주의

- 어떤 전략도 수익을 보장하지 않는다. 백테스트 결과는 미래 수익이 아니다.
- LIVE 전환은 백테스트 → Paper Trading 검증 → 소액 순서로만 진행한다.
- `.env` 를 잃어버리거나 노출했다면 즉시 업비트에서 해당 API Key 를 폐기한다.
