현재 프로젝트의 **전체 코드를 대상으로 종합적인 보안 및 안정성 감사를 수행해줘.**

이 프로젝트는 Upbit API를 이용해 실제 자산을 거래할 수 있는 자동매매 시스템이므로, 일반적인 코드 리뷰가 아니라 **실제 운영 환경에서 돈을 잃거나 계정이 탈취될 수 있다는 전제**로 매우 엄격하게 검토해줘.

코드를 수정하기 전에 먼저 전체 프로젝트를 분석하고 문제점을 모두 찾아낸 뒤, 감사 결과를 보고서 형태로 정리해줘.

---

## 1. 전체 프로젝트 구조 점검

먼저 전체 디렉터리와 파일을 확인하고 다음을 분석해줘.

- Backend 구조
- Frontend 구조
- Trading Engine
- Strategy
- Risk Manager
- Order Manager
- Position Manager
- Upbit API 연동
- WebSocket
- Database
- Authentication
- Configuration
- Logging
- Docker / 배포 설정
- 테스트 코드

각 컴포넌트 간 의존성과 데이터 흐름도 확인해줘.

특히 다음 구조가 제대로 유지되는지 확인해줘.

```text
Dashboard
    ↓
FastAPI
    ↓
Trading Engine
    ↓
Risk Manager
    ↓
Order Manager
    ↓
Upbit API
```

웹 UI가 직접 거래소 API를 호출하거나 매매 로직을 수행하는 구조가 있는지 확인해줘.

---

# 2. 보안 취약점 감사

다음 항목을 하나씩 실제 코드 기준으로 검사해줘.

### API Key / Secret

- API Key 하드코딩
- Secret Key 하드코딩
- Git에 비밀정보가 포함될 가능성
- 로그에 Secret 노출
- 에러 메시지를 통한 Secret 노출
- 환경변수 처리 문제
- `.env` 노출
- Docker 환경변수 노출

### 인증 / 권한

관리자 페이지와 API에 대해:

- 인증 우회 가능성
- 세션 관리 문제
- JWT 검증 문제
- 권한 상승
- 관리자 API 무단 접근
- Start / Stop / Live 전환 API 보호 여부
- CSRF
- CORS 설정
- Rate Limiting
- Brute-force 방어

를 검사해줘.

### 웹 보안

다음도 확인해줘.

- SQL Injection
- XSS
- CSRF
- SSRF
- Command Injection
- Path Traversal
- Insecure Deserialization
- 파일 업로드 취약점
- 민감정보 노출
- 잘못된 CORS
- 보안 헤더
- HTTPS 전제 여부

발견한 취약점은 OWASP 관점에서도 평가해줘.

---

# 3. 실제 돈이 거래되는 부분 집중 감사

가장 중요하게 검사해야 하는 부분이다.

다음과 같은 상황에서 **잘못된 주문이 발생할 가능성**이 있는지 찾아줘.

### 중복 주문

예:

```text
신호 발생
↓
주문 요청
↓
네트워크 지연
↓
응답 timeout
↓
프로그램이 주문 실패로 판단
↓
같은 주문 재전송
```

이런 상황에서 중복 주문이 발생할 수 있는지 확인해줘.

### Race Condition

동시에 여러 이벤트가 발생했을 때:

- 같은 코인을 동시에 매수
- 같은 포지션을 중복 처리
- 잔고가 업데이트되기 전에 추가 주문
- WebSocket과 REST 데이터 불일치

등이 발생할 가능성을 검사해줘.

### 주문 상태

다음 상태가 정확하게 관리되는지 확인해줘.

```text
CREATED
SUBMITTED
OPEN
PARTIALLY_FILLED
FILLED
CANCELLED
FAILED
UNKNOWN
```

특히 `UNKNOWN` 상태에서 프로그램이 잘못된 판단을 하지 않는지 확인해줘.

---

# 4. 잔고 / 포지션 계산 검증

다음 계산을 코드 기준으로 검증해줘.

- 평균 매수가
- 보유 수량
- 평가 금액
- 실현 손익
- 미실현 손익
- 수수료
- 총 자산
- 수익률
- 포지션 비율
- MDD

실제 거래소 잔고와 내부 DB의 상태가 달라졌을 때 어떻게 복구되는지도 확인해줘.

특히 프로그램을 재시작했을 때:

```text
DB 상태
vs
Upbit 실제 잔고
vs
실제 주문 상태
```

를 동기화하는 로직이 제대로 되어 있는지 검사해줘.

---

# 5. Risk Management 감사

다음 제한이 실제 주문 전에 강제되는지 확인해줘.

- 최대 주문 금액
- 최대 포지션 비율
- 일일 최대 손실
- 손절
- 익절
- 최대 거래 횟수
- 최대 연속 손실
- 최대 포지션 수

중요한 것은 단순히 UI에 설정값이 존재하는지가 아니라,

**실제 주문 코드에서 이 제한을 우회할 수 없는지** 확인하는 것이다.

예를 들어 Frontend에서 제한값을 조작하거나 API를 직접 호출해도 Risk Manager를 우회할 수 없어야 한다.

---

# 6. LIVE Trading 안전성 검사

다음 조건을 반드시 확인해줘.

```text
TRADING_MODE=LIVE
AND
LIVE_TRADING_ENABLED=true
```

두 조건이 모두 충족되지 않으면 실제 주문 API가 호출되지 않아야 한다.

그리고 다음 상황에서도 실제 주문이 발생하지 않는지 검사해줘.

- 설정값 누락
- 환경변수 오류
- API Key 오류
- 서버 재시작
- DB 오류
- WebSocket 연결 끊김
- 전략 오류
- 데이터 이상
- 프로그램 예외
- 잘못된 마켓 코드
- 비정상적인 가격
- 음수/0 주문 수량
- 비정상적인 주문 금액

---

# 7. Upbit API 사용 검증

현재 코드의 API 사용 방식이 최신 Upbit 공식 API 문서와 일치하는지 확인해줘.

특히:

- 인증 방식
- JWT
- Query Hash
- 주문 API
- 잔고 API
- WebSocket
- Rate Limit
- 오류 처리
- 주문 상태 조회

를 실제 코드와 공식 문서를 비교해서 검증해줘.

잘못된 API 사용이나 오래된 구현이 있다면 표시해줘.

---

# 8. WebSocket / 실시간 데이터 검사

다음 상황을 테스트 관점에서 검토해줘.

```text
WebSocket disconnect
WebSocket reconnect
네트워크 지연
데이터 중복
데이터 누락
순서 뒤바뀜
오래된 데이터
잘못된 가격
```

특히 WebSocket 데이터가 오래 끊겼는데도 봇이 정상적으로 거래를 계속하는 문제가 없는지 확인해줘.

Heartbeat / stale data detection이 필요한지도 검토해줘.

---

# 9. Strategy 로직 감사

각 전략을 실제 코드 기준으로 검토해줘.

특히:

- Look-ahead Bias
- 미래 데이터 참조
- 잘못된 캔들 사용
- 신호 중복 발생
- 신호가 너무 자주 발생하는 문제
- 데이터 초기화 문제
- NaN 처리
- 경계값 처리
- 시간대(Timezone) 문제
- 지표 계산 오류

를 검사해줘.

전략이 과거 데이터에서는 작동하지만 실시간 환경에서는 다르게 동작할 가능성도 찾아줘.

---

# 10. Backtest 검증

백테스트 엔진에서 다음 문제가 없는지 확인해줘.

- Look-ahead Bias
- Survivorship Bias
- 미래 가격 참조
- 수수료 누락
- 슬리피지 누락
- 체결 가능성 무시
- 캔들 내부 가격을 비현실적으로 사용
- 초기 자본 계산 오류
- 포지션 계산 오류
- MDD 계산 오류
- 수익률 계산 오류

백테스트 결과가 실제 거래보다 과도하게 좋아질 수 있는 원인이 있는지 찾아줘.

---

# 11. Database 감사

DB 관련:

- Race Condition
- Transaction 처리
- Commit / Rollback
- 데이터 중복
- 주문 중복
- Foreign Key
- Index
- Migration
- Connection Pool
- DB 장애 처리

를 확인해줘.

특히 주문과 거래 기록이 저장되는 과정에서 프로그램이 중단될 경우 데이터가 깨질 가능성을 검사해줘.

---

# 12. 장애 복구 테스트

다음 상황을 가정해서 프로그램이 어떻게 동작하는지 분석해줘.

```text
1. 프로그램 강제 종료
2. 서버 재부팅
3. 인터넷 끊김
4. Upbit API 장애
5. WebSocket 장애
6. DB 장애
7. 주문 요청 timeout
8. 주문 응답 timeout
9. 부분 체결
10. 서버 재시작 중 주문 체결
```

각 상황에서:

- 무엇이 발생하는지
- 어떤 데이터가 손실되는지
- 잘못된 주문이 발생할 수 있는지
- 복구 가능한지

를 설명해줘.

---

# 13. Dashboard / Admin API 감사

관리자 화면에서 제공하는:

- Start
- Stop
- Pause
- 설정 변경
- 전략 변경
- Trading Mode 변경
- LIVE 활성화

기능을 외부 사용자가 직접 API를 호출했을 때도 안전한지 확인해줘.

Frontend에서 버튼을 숨기는 것은 보안이 아니므로 **Backend에서 실제 권한 검증이 이루어지는지** 확인해줘.

---

# 14. 성능 및 안정성

다음도 검사해줘.

- Memory Leak
- CPU 과다 사용
- 불필요한 API 요청
- Rate Limit 초과 가능성
- WebSocket reconnect loop
- 무한 retry
- Thread / Async 문제
- Blocking 코드
- DB Connection Leak

장시간 실행했을 때 문제가 생길 가능성을 검토해줘.

---

# 15. 테스트 품질

현재 테스트 코드를 전부 확인하고,

- 테스트가 실제 핵심 로직을 검증하는지
- 중요한 부분이 테스트에서 빠져 있는지
- Mock이 잘못되어 실제 환경과 다른 결과를 내는지
- LIVE 주문을 실수로 호출할 가능성이 없는지

확인해줘.

필요한 경우 추가해야 할 테스트 목록도 작성해줘.

---

# 16. 감사 결과 작성 방식

문제를 발견하면 반드시 다음 형식으로 정리해줘.

```text
[CRITICAL]
파일:
위치:
문제:
왜 위험한가:
실제 발생 가능한 시나리오:
해결 방법:

[HIGH]
파일:
위치:
문제:
왜 위험한가:
해결 방법:

[MEDIUM]
...

[LOW]
...
```

심각도는 다음 기준으로 분류한다.

```text
CRITICAL
실제 자산 손실 / 계정 탈취 / 대규모 주문 사고 가능

HIGH
중요 기능 오류 또는 상당한 보안 위험

MEDIUM
운영 중 문제를 발생시킬 가능성이 있는 문제

LOW
개선하면 좋은 품질/유지보수 문제
```

**문제가 없다고 추측해서 "안전하다"고 결론내리지 말고, 실제 코드에서 근거를 찾아서 판단해줘.**

---

# 17. 특히 찾아야 하는 위험한 패턴

다음 패턴은 발견하면 반드시 별도로 표시해줘.

```text
중복 주문
무한 재시도
API timeout 후 재주문
잔고 불일치
포지션 불일치
LIVE 모드 우회
Risk Manager 우회
인증 우회
Secret Key 노출
WebSocket stale data
Race Condition
Look-ahead Bias
DB 상태 불일치
서버 재시작 후 잘못된 주문
```

---

# 18. 감사 이후 작업

**첫 단계에서는 코드를 수정하지 말고 전체 감사 결과만 먼저 제출해줘.**

마지막에 다음 형식으로 요약해줘.

```text
=== 전체 감사 결과 ===

CRITICAL: X개
HIGH: X개
MEDIUM: X개
LOW: X개

가장 위험한 문제:
1.
2.
3.

실거래 전에 반드시 해결해야 하는 문제:
1.
2.
3.

추가 테스트가 필요한 부분:
1.
2.
3.
```

그 다음 내가 확인하면 문제를 우선순위별로 하나씩 수정하도록 진행한다.

**실제 코드에 근거하지 않은 추측성 문제는 취약점으로 보고하지 말고, 근거와 함께 명확하게 구분해줘.**