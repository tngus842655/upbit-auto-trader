# Claude용 감사 결과 검증 및 조치 프롬프트

이 문서는 **새 세션에서 아무 맥락 없이** 시작해도 되도록 작성되었다. 2026-09-24 에 수행한 전체 코드 보안·안정성 감사의 결과를 **다시 검증하고, 진짜 오류로 확인된 것만 우선순위 순으로 조치**하는 작업 지시서다.

- 전체 감사 보고서(근거·줄 번호·시나리오 포함): `docs/audit/2026-09-24-전체-코드-보안-안정성-감사.md`
- 재현 스크립트(네트워크·실제 키 없이 실행): `docs/audit/verify_audit_findings.py`
- 감사 당시 기준 커밋: `d9df6a7` (보고서의 줄 번호는 이 커밋 기준이다. 코드가 바뀌었으면 줄 번호가 아니라 함수 이름으로 찾아라)

---

## 0. 작업 원칙 (반드시 지킬 것)

1. **보고서를 믿지 말고 먼저 검증해라.** 각 항목을 아래 절차로 직접 재현한 뒤에만 "진짜 오류" 로 판정한다. 재현되지 않으면 오탐으로 기록하고 수정하지 않는다.
2. **한 번에 한 항목씩** 고친다. 항목마다 (a) 재현 → (b) 수정 → (c) 회귀 테스트 추가 → (d) `pytest` 전체 + `ruff` 통과 → (e) 커밋. 여러 항목을 한 커밋에 섞지 않는다.
3. 우선순위는 **CRITICAL → HIGH → MEDIUM → LOW**. CRITICAL·HIGH 가 끝나기 전에는 MEDIUM 이하를 건드리지 않는다.
4. **LIVE 안전장치를 약화시키는 변경은 금지**한다. `TRADING_MODE=LIVE` + `LIVE_TRADING_ENABLED=true` 이중 플래그, `--confirm-live REAL-MONEY`, `UpbitClient.allow_orders`, `assert_live_order_allowed` 는 그대로 두고, 관련 테스트(`tests/test_live_guard.py`, `tests/test_client_orders.py`, `tests/test_engine_control.py`)는 항상 통과해야 한다.
5. `.env` 는 절대 커밋하지 않는다. 실제 API Key 로 주문을 내는 검증은 하지 않는다. 실서버 확인이 필요한 항목은 `python -m app.main order-test`(실제 주문 없음) 까지만 쓴다.
6. 기존 테스트를 지우거나 `skip` 으로 우회하지 않는다. 테스트가 "잘못된 동작을 정답으로 고정" 하고 있으면(예: `tests/test_api.py::test_token_auth` 의 "조회는 자유") 그 테스트를 **새 동작에 맞게 고쳐서** 남긴다.
7. 수정 범위는 항목이 요구하는 최소한으로 한다. 리팩터링·기능 추가는 하지 않는다.
8. 작업 결과는 이 문서 마지막의 **결과 기록 양식**대로 남긴다.

---

## 1. 환경 준비

```bash
# Python 3.12 이상. Windows 는 .venv\Scripts\python.exe, Linux/macOS 는 .venv/bin/python
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt          # Windows: .venv\Scripts\pip.exe install -r requirements-dev.txt
.venv/bin/python -m pytest -q                          # 감사 시점: 363건 통과
.venv/bin/ruff check app tests scripts                 # 감사 시점: scripts/sweep_strategy.py 10건만
PYTHONPATH=. .venv/bin/python docs/audit/verify_audit_findings.py    # 감사 시점: 14건 중 14건 "재현됨"
```

- 재현 스크립트는 `tests/` 의 헬퍼를 import 하므로 반드시 **프로젝트 루트에서 `PYTHONPATH=.`** 로 실행한다 (Windows PowerShell: `$env:PYTHONPATH="."`).
- 스크립트의 각 줄은 `[항목] 재현됨: …` 또는 `[항목] 재현 안 됨: …` 이다. **조치가 끝나면 그 항목이 "재현 안 됨" 으로 바뀌어야 한다.** 스크립트 자체를 고쳐서 통과시키는 것은 금지. 다만 수정 후 인터페이스가 바뀌어(예: `OrderStatus.UNKNOWN` 추가) 스크립트의 "기대" 판정을 바꿔야 하면, 그 이유를 결과 기록에 적고 최소한으로 고친다.
- 인터넷이 되는 환경이면 8절의 "검증 불가" 항목을 `https://docs.upbit.com/kr/` 원문으로 확인한다. 감사 환경에서는 접근이 차단되어 확인하지 못했다.

---

## 2. CRITICAL — 실거래 전 반드시 해결

### CRITICAL-1  주문 응답 타임아웃 후 identifier 조회 실패/404 → 새 identifier 로 재주문 (중복 주문)

- 파일: `app/trading/live_broker.py` — `LiveBroker.execute()` 의 재시도 루프(감사 기준 126-146행), `_find_by_identifier()`(191-199행), `make_identifier()`(45-52행)
- 주장: `create_order` 가 `UpbitNetworkError` 를 내면 `_find_by_identifier` 로 한 번 조회하고, 결과가 `None`(404 또는 조회도 네트워크 오류)이면 다음 attempt 에서 `-r2` 접미사를 붙인 **새 identifier 로 같은 주문을 다시 낸다.** 첫 주문이 서버에 접수돼 있었다면 두 번 매수한다.
- 검증: `verify_audit_findings.py` 의 `[CRITICAL-1]` 두 줄("조회도 타임아웃", "조회 404"). 거래소에 생성된 주문이 2건(`['c1', 'c1-r2']`)이면 재현.
  추가로 `tests/test_live_broker.py::test_network_error_without_order_is_rejected` 를 읽어, 이 테스트가 "1차·2차 모두 서버 생성 실패" 만 다루고 위 경우를 놓치는지 확인한다.
- 진짜 오류 판정 기준: 응답 유실 + 조회 실패 조합에서 서버 주문이 2건 생기면 진짜.
- 조치 방향 (택 1 또는 조합, 설계는 스스로 판단하되 아래 원칙 유지):
  1. 네트워크 오류 뒤에는 **절대 새 identifier 로 재주문하지 않는다.** 주문을 `UNKNOWN` 상태로 기록하고(HIGH-1 과 함께), identifier 조회를 지수 백오프로 수 회 반복한다. 끝내 확인이 안 되면 `reconcile`(거래소 잔고)로 판정하고 운영자에게 알린다.
  2. 재시작 시 `GET /v1/orders/uuids?identifiers[]=…`(공식 SDK 에 존재)로 미결 identifier 를 일괄 조회해 복구하는 경로를 추가한다.
  3. `GET /v1/order` 가 "없는 주문" 에 404 를 주는지 400 을 주는지 실서버에서 확인하고(8절), 404 를 "미생성 확정" 으로 쓰려면 최소 수 초 간격의 재조회 후에만 쓴다.
- 완료 조건: 스크립트 `[CRITICAL-1]` 두 줄 모두 "재현 안 됨". 회귀 테스트: "서버 생성 성공 + 응답 유실 + 조회 실패/404" 에서 `create_order` 호출이 1회인지, 주문 상태가 UNKNOWN(또는 확정 후 FILLED)인지.

### CRITICAL-2  오래된 호가를 mark_price 로 사용 → WebSocket 장애 중 손절·일일 손실·괴리 검사 무력화

- 파일: `app/trading/market_state.py` — `PriceState.mark_price`(34-39행), `PriceState.is_fresh`(30-32행); `app/trading/engine.py` — `ensure_prices()`(430-431행, `set_last_price` 만 호출), `check_exits()`(393행), `current_equity()`(368-372행); `app/risk/manager.py` — `evaluate()` 괴리 검사(188-196행)
- 주장: `mark_price` 는 `best_bid/ask` 가 있으면 `book_time` 과 무관하게 호가 중간값을 돌려준다. REST 보정은 `last_price/last_time` 만 갱신하고 `is_fresh` 는 두 시각 중 최신값을 보므로 "체결가는 방금, 호가는 몇 시간 전" 이 신선한 것으로 통과한다. 그 값이 손절·일일 손실·괴리 검사·스냅샷에 모두 쓰인다.
- 검증: 스크립트 `[CRITICAL-2]` 두 줄. `is_fresh=True` 인데 `mark_price` 가 3시간 전 호가(1억)이고, 실제 -20% 에서 손절 판정이 `None` 이면 재현.
- 진짜 오류 판정 기준: 위 재현 + `engine.ensure_prices` 가 호가를 갱신·무효화하지 않는 것을 코드로 확인.
- 조치 방향: `mark_price` 가 호가를 쓰기 전에 `book_time` 이 허용 나이(`price_max_age_seconds`) 이내인지 확인하고 아니면 `last_price` 로 폴백. `is_fresh` 는 "실제로 쓰는 데이터의 시각" 기준으로. REST 보정 시 호가를 함께 갱신하거나 `best_bid/ask` 를 무효화. 마켓별로 시세가 N초 이상 오래되면 알림 + 신규 진입 중단.
- 완료 조건: 스크립트 `[CRITICAL-2]` 두 줄 "재현 안 됨". 회귀 테스트: 호가 오래됨 + 체결가 최신 → `mark_price == last_price`, `check_exits` 가 손절 발동; PaperBroker 의 기존 동작(`tests/test_orders.py`)은 유지.

---

## 3. HIGH

### HIGH-1  주문 상태에 UNKNOWN/PARTIAL 이 없어 "체결 확인 실패" 가 REJECTED 로 축약됨

- 파일: `app/trading/orders.py` `OrderStatus`(23-26행); `app/trading/live_broker.py` `execute()` 150-155행, `_apply_fill()` 220-225행; `app/trading/engine.py` `_record_order()` 322-363행, `check_exits()` 374-415행
- 주장: 주문 생성 뒤 `_wait_for_fill` 이 실패하면 REJECTED 로 기록하고 계좌를 바꾸지 않는다. 체결된 매수는 손절 감시 밖(최대 300초), 체결된 매도는 내부 포지션이 남아 **1초마다** 매도 재시도 + 주문·로그·알림 폭주.
- 검증: 스크립트 `[HIGH-1]`. 거래소 주문 1건 생성인데 내부 상태 REJECTED, 현금 차감 0 이면 재현.
- 조치 방향: `SUBMITTED / OPEN / PARTIALLY_FILLED / CANCELLED / UNKNOWN` 추가. UNKNOWN 은 (a) 같은 마켓의 신규 주문·청산 재시도를 막고 (b) 백그라운드로 uuid 조회를 반복해 확정 (c) 확정 전엔 reconcile 로 잠정 반영. 청산 재시도에는 마켓별 백오프·횟수 상한. DB `orders.status` 는 `String(12)` 이므로 `PARTIALLY_FILLED`(16자)를 쓰려면 컬럼 길이도 확인.
- 완료 조건: 스크립트 `[HIGH-1]` "재현 안 됨". 회귀 테스트: 폴링 실패 → UNKNOWN 기록, 이후 조회 성공 시 FILLED 로 확정되고 계좌 반영.

### HIGH-2  시세 루프가 예외 한 번에 영구 종료되고 재시작하지 않음

- 파일: `app/trading/engine.py` `_price_loop()` 570-587행, `run()` 618행·678-680행
- 주장: 루프 내부(`check_exits → broker.execute → repo.save_order`)에서 `TraderError` 가 아닌 예외(SQLite `OperationalError`, `KeyError` 등)나 WS 치명 오류가 나면 로그만 남기고 태스크가 끝난다. 이후 손절 감시는 캔들 경계에서만.
- 검증: 스크립트 `[HIGH-2]`. 루프 종료 + `price_stream_failed` 로그면 재현.
- 조치 방향: 루프를 감시 태스크로 감싸 예외 시 백오프 후 재생성, 하트비트 `ws_status` 와 알림에 반영. `check_exits` 안의 예외는 마켓 단위로 격리. 시세 없이 N초 이상이면 "감시 중단" 알림.
- 완료 조건: 스크립트 `[HIGH-2]` "재현 안 됨"(루프가 살아 있거나 재생성). 회귀 테스트: 첫 예외 후에도 다음 시세 메시지에서 `check_exits` 가 호출되는지.

### HIGH-3  재시작 시 일일 손실 한도 기준선이 리셋되고 halt·잠금이 사라짐

- 파일: `app/risk/manager.py` `rebuild()` 130-139행, `start_day_if_needed()` 82-95행, `halt()` 141-144행; `app/trading/engine.py` `rebuild_risk_state()` 186-196행, `run()` 617행
- 주장: `rebuild` 는 `day_start_equity` 를 재시작 시점 자산으로 둔다. `halted`, `lock_reason`, `last_exit_at` 은 메모리에만 있다.
- 검증: 스크립트 `[HIGH-3]` 두 줄. 재시작 후 `day_start=975,000`, 당일 -5% 에서 `lock=None`, halt 후 재시작 `halted=False` 면 재현.
- 조치 방향: 당일(KST) 시작 자산을 DB 에서 복구(`balances` 의 당일 첫 스냅샷, 또는 새 `risk_state` 테이블). `halted / halt_reason / lock_reason / last_exit_at` 영속화. 시작 시 잠금 상태면 알림.
- 완료 조건: 스크립트 `[HIGH-3]` 두 줄 "재현 안 됨". 회귀 테스트: 재시작 후 당일 누적 -3% 에서 잠금 발동, halt 유지.

### HIGH-4  정체 후 누락 캔들의 오래된 신호를 현재가로 일괄 실행, 캔들 공백 미검출

- 파일: `app/trading/engine.py` `process_closed_candles()` 217-244행(`refresh_candles=5`); `app/trading/market_state.py` `merge_candles()` 50-70행; `app/strategy/data.py` `validate_candles()` 133-137행
- 주장: 새로 닫힌 캔들마다 전략을 돌려 즉시 주문한다. 몇 시간 뒤 깨어나면 BUY→SELL 이 현재가로 연속 체결된다. 5개 넘게 놓치면 사이 캔들이 영구 누락된다.
- 검증: 스크립트 `[HIGH-4]`. 3시간 정체 후 BUY·SELL 두 주문이 모두 FILLED 면 재현.
- 조치 방향: 마지막 닫힌 캔들의 신호만 실행(이전 캔들은 지표 갱신용). 신호 캔들 시각이 현재보다 1 인터벌 이상 오래되면 실행하지 않고 로그. `merge_candles` 뒤 연속성 검사, 공백이 있으면 `warmup()` 재실행.
- 완료 조건: 스크립트 `[HIGH-4]` "재현 안 됨"(주문 최대 1건). 회귀 테스트: 다중 캔들 동시 확정 시 마지막 신호만 주문; 공백 감지 시 warmup 호출.

### HIGH-5  대시보드 인증: 조회 API 무인증, IP 기반 인증의 CSRF·프록시 우회

- 파일: `app/api/server.py` `require_auth` 62-75행, `LOCAL_HOSTS` 40행, GET 엔드포인트 81-153행, `api_bot_command` 276-285행, `api_bot_start` 235-263행, `ws_live` 328-332행; `app/main.py` `cmd_serve` 581-595행
- 주장: (a) 모든 GET 이 토큰 유무와 무관하게 무인증. (b) 토큰 미설정 시 `request.client.host` 만으로 허용 → 외부 사이트의 폼 POST(빈 본문)로 `stop/halt/pause/resume/reload/kill/start(paper)` 호출 가능. (c) 리버스 프록시가 `X-Forwarded-For` 를 안 붙이면 원격 요청 전부 로컬 판정.
- 검증: 스크립트 `[HIGH-5]` 두 줄. 외부 Origin 폼 POST 가 200 이고 live 큐에 `['stop', 'halt']` 가 들어가면 재현; 토큰 설정 상태에서 조회가 200 이면 재현.
- 조치 방향: 모든 `/api/*` 와 `/ws` 에 인증 적용(토큰 필수화가 가장 단순. 로컬 전용 모드를 남기려면 세션 쿠키 + CSRF 토큰). 변경·제어 API 는 JSON 본문만 허용하고 빈 본문 거부, `Origin`/`Sec-Fetch-Site` 검사. 토큰 비교는 `hmac.compare_digest`, 쿼리스트링 토큰 제거(WebSocket 은 첫 메시지로 전달하거나 서브프로토콜 사용), 실패 지연. `forwarded_allow_ips` 문서화. `tests/test_api.py::test_token_auth` 의 "조회는 자유" 기대를 새 동작으로 수정.
- 완료 조건: 스크립트 `[HIGH-5]` 두 줄 "재현 안 됨". 회귀 테스트: 토큰 설정 시 GET 401, 외부 Origin/빈 본문 POST 403.

### HIGH-6  비-TraderError 예외(DB 잠금 등)가 엔진 전체를 종료시키고 감시자·자동 재시작이 없음

- 파일: `app/trading/engine.py` `run()` 624-675행(`except TraderError` 만), `poll_commands()` 522-565행, `write_heartbeat()` 505-520행; `app/main.py` `main()` 750-773행; `app/database/database.py` `Database.__init__` 29-43행(`timeout` 미지정)
- 주장: 메인 루프의 DB 호출·전략 계산에서 나는 `OperationalError`·pandas 예외 등은 잡히지 않아 프로세스가 끝나고, 열린 포지션이 감시 없이 남는다. 대시보드와 SQLite 를 공유하므로 "database is locked" 가 현실적 트리거.
- 검증: 코드 근거 항목이다(스크립트에 없음). `run()` 의 `try/except` 를 읽어 `TraderError` 외 예외가 어디서도 잡히지 않는지 확인하고, 필요하면 `Harness` 로 `repo.write_heartbeat` 가 `OperationalError` 를 내게 만들어 `run()` 이 종료되는지 직접 재현한다.
- 조치 방향: 루프 단계별 예외 격리(DB 오류는 재시도·백오프 후 계속, 전략 예외는 마켓 단위 건너뜀), `connect_args["timeout"]` 명시(예: 30초), 치명 오류 시 "청산 감시만 유지하는 안전 모드", 종료 시 즉시 알림. 프로세스 감시자(systemd/supervisor/Docker restart)는 Phase 10 에서.
- 완료 조건: DB 오류 1회로 `run()` 이 끝나지 않는 회귀 테스트.

### HIGH-7  엔진 단일 인스턴스 보장 없음

- 파일: `app/main.py` `cmd_run()` 317-410행; `app/api/server.py` `api_bot_start()` 235-263행; `app/api/process.py` `engine_is_alive()` 25-35행
- 주장: CLI `run` 과 대시보드 Start 동시 실행, 또는 하트비트가 기록되기 전 Start 연타로 같은 모드 엔진이 두 개 뜬다. 락 파일·PID 검사 없음.
- 검증: 코드 근거 항목. `cmd_run` 에 다른 실행 중 엔진을 확인하는 코드가 없는지, `api_bot_start` 가 `start_engine` 호출 전에 STARTING 을 기록하지 않는지 확인.
- 조치 방향: 모드별 락 파일(`fcntl`/`msvcrt`) 또는 `engine_status` 에 PID+시작시각을 원자적으로 기록하고 살아 있는 PID 면 시작 거부. 대시보드는 `start_engine` 직전에 STARTING 하트비트를 기록.
- 완료 조건: 두 번째 `run` 이 즉시 종료되는 회귀 테스트(락 획득 실패).

---

## 4. MEDIUM (CRITICAL·HIGH 완료 후)

| ID | 요약 | 파일 (감사 기준 위치) | 검증 | 조치 방향 |
| --- | --- | --- | --- | --- |
| MEDIUM-1 | 시장가 매수 응답에 `trades` 가 없으면 `info.price`(=주문 총액)를 단가로 사용 | `live_broker.py` `_apply_fill()` 226-227행 | 스크립트 `[MEDIUM-1]` (avg_price=100,000, 차감 150 이면 재현) | `ord_type=price` 면 `(locked 또는 price−remaining)/executed` 로 계산하거나 `trades` 채워질 때까지 재조회, 안 되면 UNKNOWN |
| MEDIUM-2 | `avg_buy_price=0`·먼지 잔고가 포지션으로 편입 → 손절 무력, 마켓 점유, 매도 거부 반복 | `live_broker.py` `portfolio_from_accounts()` 55-76행; `manager.py` `check_exits()` 223-226행 | 스크립트 `[MEDIUM-2]` 두 줄 | 평가액 < 최소 주문 금액이면 편입 제외, avg=0 이면 현재가를 기준가로 쓰거나 운영자 확인 전 자동 청산 제외 + 알림 |
| MEDIUM-3 | `reconcile` 이 `locked` 무시 → 진행 중 매도와 경합 시 포지션 삭제·왕복 기록 누락 | `live_broker.py` 62·69·274-276행; `engine.py` 664-667행 vs `_price_loop` 580행 | 코드 근거. `Account.balance` 만 쓰는지 확인 | `total(balance+locked)` 사용, 진행 중 주문 있는 마켓은 reconcile 제외 또는 락으로 직렬화 |
| MEDIUM-4 | 포켓 이전(입출금)이 누적 수익률·일일 손실 계산을 왜곡 | `services.py` `performance()` 183행; `manager.py` `update_equity()` 104-112행; `main.py` 308-311행 | 코드 근거 | 입출금 이벤트 기록 후 기준 자산 조정 |
| MEDIUM-5 | 강제 종료가 PID 재사용을 검증하지 않음 | `process.py` `kill_engine()` 62-72행 | 코드 근거 | cmdline/시작시각 확인, STALE 아닐 때만 |
| MEDIUM-6 | LiveBroker 가 시세 신선도·이상치를 보지 않음, 단일 틱으로 손절 | `live_broker.py` `execute()`; `engine.py` 392-395행; `manager.py` 188-196행 | 코드 근거 | 신선도 검사, 연속 N틱/M초 지속 조건 |
| MEDIUM-7 | 체결 1건 기록이 여러 트랜잭션 → 중간 종료 시 PAPER 상태 불일치 | `engine.py` `_record_order()` 328-356행; `repository.py` 60-114행 | 코드 근거 | 단일 트랜잭션 메서드 |
| MEDIUM-8 | 백테스트 추적 손절의 캔들 내 순서 가정이 낙관적, 익절 터치=체결 | `backtest/engine.py` 189-199행; `manager.py` 231-236행 | 코드 근거 (`tests/test_risk.py::test_trailing_stop_tracks_peak` 가 현재 동작을 고정) | 직전 캔들까지의 peak 로 판정 등 보수화 |
| MEDIUM-9 | SQLite 잠금 대기 미설정, 두 프로세스 동시 쓰기 | `database.py` 29-43행 | 코드 근거 | `timeout` 명시, `busy_timeout` |
| MEDIUM-10 | 토큰 인증 강도(쿼리스트링, 비상수시간 비교, 무제한 시도, 보안 헤더·HTTPS 없음) | `server.py` 62-69·329-332행; `app.js` 37-38·358행 | 코드 근거 | HIGH-5 와 함께 처리 |
| MEDIUM-11 | key 없는 알림은 쿨다운 없음 → 장애 시 폭주 | `notify/manager.py` `emit()` 77-83행; `engine.py` 362-363행 | 코드 근거 | 마켓·종류 key + 쿨다운 |
| MEDIUM-12 | 대시보드 백테스트가 엔진과 같은 IP 의 시세 Rate limit 을 조율 없이 소비 | `api/backtests.py` 79-83·288-322행; `backtest/loader.py` 74-75행 | 코드 근거 | 백테스트 로더 별도 낮은 한도, 동시 1건 |
| MEDIUM-13 | 엔진 경로에 캔들 무결성·이상치 검사 없음 | `market_state.py` `merge_candles()` 50-70행 | 코드 근거 | merge 후 `validate_candles`, 이상치면 보류 + 알림 |
| MEDIUM-14 | Rate limit 외부 의존(`Remaining-Req` 가정, `Retry-After` 미지원, `DELETE /v1/order` 그룹) | `rate_limiter.py` 73-79·201-206행; `upbit_client.py` 504-507행 | **검증 불가** — 8절 | `Retry-After` 지원, 헤더 부재 정상 처리, 실서버 확인 후 매핑 |

---

## 5. LOW (여유가 있을 때)

| ID | 요약 | 파일 |
| --- | --- | --- |
| LOW-1 | 하트비트 `api_ok` 가 첫 캔들 점검 이후 항상 True | `engine.py` `write_heartbeat()` 512행 |
| LOW-2 | 대시보드가 띄운 엔진 stdout 로그가 회전 없이 누적 | `process.py` 44행 |
| LOW-3 | 설정 요약이 Access Key 앞 4자·`database_url` 출력 | `settings.py` `summary()` 334-356행; `main.py` 70-76행 |
| LOW-4 | `LIVE_TRADING_ENABLED` 가 `1/yes/on` 도 참 | `settings.py` 127행 |
| LOW-5 | 1w/1M/1y 캔들 경계 계산 불일치 | `engine.py` `next_boundary()` 65-69행 |
| LOW-6 | 실행 설정 저장 `max(version)+1 → INSERT` 비원자적 | `repository.py` 312-316행 |
| LOW-7 | 매도 수량 `.8f` 반올림이 잔고를 1e-8 초과 가능 | `upbit_client.py` 305행 |
| LOW-8 | LIVE 재시작 후 `entry_fee=0`, `opened_at=지금`, `initial_cash` 고정 | `live_broker.py` 73-75행; `main.py` 308-311행 |
| LOW-9 | FK 없음, `bot_logs`/`balances` 무한 성장, `count_rows` 전체 스캔 | `models.py`; `repository.py` 338-340행 |
| LOW-10 | `kill(SIGTERM)` 시 `finally` 미실행, 로그 파일 핸들 누수 | `process.py` 44·62-72행; `main.py` |
| LOW-11 | ruff 오류 10건 | `scripts/sweep_strategy.py` |
| LOW-12 | `chance.max_total` 미강제 | `live_broker.py` `_build_params()` |
| LOW-13 | 대시보드 프로세스가 주문 가능 클라이언트 보유 | `services.py` `bot_client()` 254-255행 |
| LOW-14 | `/ws` 의 `mode` 미검증, 접속당 2초 DB 폴링 | `server.py` 328-347행 |

---

## 6. 감사에서 문제 없음으로 확인된 것 (다시 검증할 필요 없음, 회귀만 막을 것)

- LIVE 이중 플래그·확인 문구·클라이언트 잠금은 서버에서 강제되며 웹에서 우회할 수 없다.
- 전략(`ma_cross`, `rsi`)과 백테스트 엔진에 Look-ahead 는 없다(`check_no_lookahead` 통과).
- JWT 생성 규칙(`query_hash`, 배열 파라미터, 본문 해시)은 공식 SDK 와 일치한다.
- Secret Key 는 로그·오류 메시지·git 이력 어디에도 없다. XSS·SQL Injection·Command Injection·Path Traversal 은 발견되지 않았다.
- 수수료·평균가·실현손익·미실현손익·MDD 계산식은 정확하다(입력 가격이 오래된 문제는 CRITICAL-2).

---

## 7. 추가해야 할 테스트 (조치 항목별 회귀 테스트와 별도로)

1. `LiveBroker`: 서버 생성 성공 + 응답 유실 + 조회 실패/404 → 주문 1건 (CRITICAL-1)
2. `PriceState`/`engine`: 호가 오래됨 + 체결가 최신 → 최신 체결가 사용 (CRITICAL-2)
3. `RiskManager.rebuild`: 재시작 후 `day_start_equity` 복구, halt/lock 영속 (HIGH-3)
4. `engine._price_loop`: 내부 예외 후 재생성, 하트비트 반영 (HIGH-2)
5. `engine.run`: `OperationalError` 로 종료되지 않음 (HIGH-6)
6. `process_closed_candles`: 다중 캔들 동시 확정 시 마지막 신호만, 공백 감지 (HIGH-4)
7. `_apply_fill`: `trades` 없는 `ord_type=price` 응답의 평균가·현금; 부분 체결 후 취소 상태값 (MEDIUM-1, HIGH-1)
8. `reconcile`: `locked` 잔고·진행 중 주문 경합, 먼지·avg=0 편입 (MEDIUM-2, MEDIUM-3)
9. 대시보드: 토큰 설정 시 GET 401, 외부 Origin 폼 POST 403, XFF 유무별 로컬 판정, Start 이중 호출 거부 (HIGH-5, HIGH-7)
10. 포켓 이전이 일일 손실·누적 수익률에 영향 없음 (MEDIUM-4)
11. Rate limit: `Retry-After` 준수, `Remaining-Req` 부재 시 동작 (MEDIUM-14)
12. 백테스트 추적 손절의 캔들 내 순서 보수 처리 (MEDIUM-8)

---

## 8. 감사 환경에서 검증하지 못한 것 — 인터넷이 되면 먼저 확인

감사 환경은 `docs.upbit.com`·`api.upbit.com` 접근이 차단되어 아래를 확인하지 못했다. 공식 Python SDK 1.0.0 소스와만 비교했다. 취약점으로 보고하지 않았으니 **문서 원문으로 확인한 뒤** 필요한 것만 반영한다.

| 확인 항목 | 왜 중요한가 | 확인 방법 |
| --- | --- | --- |
| `GET /v1/order` 가 없는 주문에 404 를 주는지 400 을 주는지 | CRITICAL-1 의 "미생성 확정" 판단 기준 | 문서 `reference/get-order`, 또는 실서버에 임의 identifier 조회 (주문 없음) |
| identifier 재사용 불가 규칙의 정확한 범위(거부된 주문도 포함?) | CRITICAL-1 조치 방식 2 (같은 identifier 재전송) 가능 여부 | 문서 `reference/new-order` |
| JWT HS512 수락 여부 | SDK 는 HS256 을 쓴다. 프로젝트는 HS512 | `python -m app.main order-test` 로 인증 통과 확인 |
| `Remaining-Req` 헤더가 아직 제공되는지, `Retry-After` 를 주는지 | MEDIUM-14 | 문서 `reference/rate-limits`, 실제 응답 헤더 |
| Rate limit 그룹별 한도와 `DELETE /v1/order` 의 그룹 | `rate_limiter.py` 매핑 | 문서 `reference/rate-limits` |
| `GET /v1/orders/uuids` 의 identifiers 일괄 조회 | CRITICAL-1/HIGH-1 복구 경로 | 문서 `reference/list-orders-by-uuids` (SDK `order_list_by_uuids_params.py` 참고) |

---

## 9. 결과 기록 양식

작업이 끝나면 `docs/audit/2026-09-24-조치-결과.md` 를 만들고 항목마다 아래 형식으로 남긴다. 검증만 하고 수정하지 않은 항목도 기록한다.

```text
[ID] 제목
판정: 진짜 오류 / 오탐 / 검증 불가
재현 방법과 결과: (스크립트 출력 또는 직접 재현한 절차)
조치: (수정한 파일·함수, 커밋 해시) 또는 "조치 안 함 — 이유"
회귀 테스트: (추가한 테스트 이름)
검증: pytest 결과 / ruff 결과 / verify_audit_findings.py 해당 줄 "재현 안 됨" 확인
남은 위험: (있으면)
```

마지막에 요약한다.

```text
=== 조치 결과 ===
진짜 오류로 확인·수정: N건 (ID 목록)
오탐: N건 (ID 목록, 이유)
검증 불가·보류: N건 (ID 목록, 필요한 정보)
verify_audit_findings.py: 재현됨 N건 → M건
pytest: N건 통과 / ruff: 오류 N건
실거래 전 남은 필수 항목:
1.
2.
```
