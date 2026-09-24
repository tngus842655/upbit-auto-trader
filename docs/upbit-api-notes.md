# 업비트 Open API 사양 메모 (Phase 1 기준)

확인일: 2026-09-24. 출처는 모두 공식 문서 <https://docs.upbit.com/kr/> 이며, 페이지 URL 뒤에 `.md` 를 붙이면
원문 마크다운을 받을 수 있다 (예: `https://docs.upbit.com/kr/reference/auth.md`). 전체 목록은
`https://docs.upbit.com/kr/llms.txt`. **API 가 바뀌면 이 문서보다 공식 문서를 우선한다.**

## 공통 — REST API 사용 및 에러 안내 (`reference/rest-api-guide`, 2026-08-31 갱신)

- Base URL `https://api.upbit.com`, TLS 1.2 이상(1.3 권장), 응답·POST 본문 모두 `application/json`.
- 에러 응답 형식 `{"error": {"name": ..., "message": ...}}`.
  시세(Quotation) API 는 `name` 이 정수(HTTP 코드), 거래(Exchange) API 는 문자열 에러 코드.
- GET/DELETE 쿼리 파라미터는 URL 인코딩해서 보낸다. 단 Exchange 배열 파라미터 이름의 `[]` 는 인코딩하지 않는다.
- gzip 응답은 시세 API 만 지원 (`Accept-Encoding: gzip`).
- 주요 에러 코드
  - 400: `create_ask_error`/`create_bid_error`, `insufficient_funds_ask`/`_bid`, `under_min_total_ask`/`_bid`,
    `validation_error`, `invaild_parameter`(문서 표기 그대로), `duplicated_identifier`
  - 401: `invalid_query_payload`, `jwt_verification`, `expired_access_key`, `nonce_used`, `no_authorization_ip`,
    `no_authorization_token`
  - 403: `out_of_scope`(권한 부족 — 도메인마다 401/403 이 섞일 수 있으니 코드 문자열로 판단), `open_api_withdraw_locked`
  - 404: `pocket_not_found`, `currency_not_found` / 418: 과도한 요청으로 차단 / 429: 요청 제한 초과 / 500: 서버 오류

## 인증 (`reference/auth`, 2026-09-22 갱신)

- JWT, 서명 알고리즘 **HS512 권장** (`{"alg":"HS512","typ":"JWT"}`). 예제 일부는 아직 HS256 을 쓰지만 둘 다 허용.
- payload: `access_key`, `nonce`(매 요청 새 UUID), 쿼리/본문이 있으면 `query_hash` + `query_hash_alg="SHA512"`.
- `query_hash` = SHA512( **URL 인코딩 전** 쿼리 문자열 ), 파라미터 순서 유지.
  - 배열 파라미터: `states[]=wait&states[]=watch`
  - 쉼표 구분 파라미터: `markets=KRW-BTC,KRW-ETH`
  - POST: JSON 본문 key-value 를 `k=v&k=v` 로 이어 붙여 해시
  - 공식 Python 예제: `unquote(urlencode(params, doseq=True))` → `hashlib.sha512(...).hexdigest()`
- Secret Key 는 Base64 가 아니다(디코딩 금지). 업비트 키는 40자라 PyJWT 가 HS512 최소 길이(64바이트) 경고를 내므로
  `app/exchange/auth.py` 에서 그 경고만 억제한다.
- 전송: `Authorization: Bearer {JWT}` (REST·WebSocket 동일).
- API Key 발급 시 호출 IP 를 허용 목록에 등록(키당 최대 10개). 권한 그룹: 자산조회 / 주문하기 / 주문조회 /
  출금하기 / 출금조회 / 입금하기 / 입금조회. **이 프로젝트는 자산조회·주문조회(·Phase 7 부터 주문하기)만 사용하며
  출금 권한은 절대 부여하지 않는다.**

## 요청 수 제한 (`reference/rate-limits`, 2026-09-08 갱신)

| 그룹 | 한도 | 측정 단위 | 대상 |
| --- | --- | --- | --- |
| `market` | 초당 10회 | IP | 페어 목록 |
| `candle` | 초당 10회 | IP | 초/분/일/주/월/연 캔들 |
| `trade` | 초당 10회 | IP | 체결 이력 |
| `ticker` | 초당 10회 | IP | 현재가(페어·마켓 단위) |
| `orderbook` | 초당 10회 | IP | 호가·호가 정책 |
| `default` | 초당 30회 | 포켓 | 잔고, 주문 조회·취소, 입출금, API Key 목록 등 |
| `order` | 초당 12회 | 포켓 | 주문 생성, 취소 후 재주문 |
| `order-test` | 초당 8회 | 포켓 | 주문 생성 테스트 |
| `order-cancel-all` | 2초당 1회 | 포켓 | 주문 일괄 취소 |
| `websocket-connect` | 초당 5회 | IP(비인증)/포켓(인증) | WebSocket 연결 |
| `websocket-message` | 초당 5회·분당 100회 | 커넥션 | WebSocket 데이터 요청 |

- 같은 그룹의 API 는 한도를 함께 차감. 같은 포켓의 여러 API Key 도 한도를 공유(처리량을 늘리려면 포켓 분리).
- 응답 헤더 `Remaining-Req: group=default; min=1800; sec=29` — `sec` 가 현재 잔여 요청 수, `min` 은 deprecated.
- 429 → 다음 초 경계까지 대기 후 재시도. 418 → 429 누적으로 일시 차단(반복 시 차단 시간 증가) → 안내 시간 후 재시도.
- `Origin` 헤더가 포함된 요청(브라우저)은 시세 REST·WebSocket 모두 10초당 1회만 허용 → 브라우저에서 직접 호출 금지.

## Phase 1 에서 사용하는 엔드포인트

| 기능 | 메서드 / 경로 | 파라미터 | 그룹 | 인증 |
| --- | --- | --- | --- | --- |
| 페어 목록 | `GET /v1/market/all` | `is_details` | market | 없음 |
| 현재가 | `GET /v1/ticker` | `markets=KRW-BTC,KRW-ETH` | ticker | 없음 |
| 초봉 | `GET /v1/candles/seconds` | `market`, `to`, `count`(≤200) | candle | 없음 |
| 분봉 | `GET /v1/candles/minutes/{unit}` unit ∈ 1,3,5,10,15,30,60,240 | `market`, `to`, `count` | candle | 없음 |
| 일봉 | `GET /v1/candles/days` | `market`, `to`, `count`, `converting_price_unit` | candle | 없음 |
| 주/월/연봉 | `GET /v1/candles/{weeks,months,years}` | `market`, `to`, `count` | candle | 없음 |
| 포켓 잔고 | `GET /v1/accounts` | — | default | [자산조회] |

- `to`: ISO 8601 (`2025-06-24T04:56:53Z`, `2025-06-24 04:56:53`, `2025-06-24T13:56:53+09:00`).
  지정 시각 **이전** 캔들부터 조회, 미지정 시 요청 시각 기준 최신. `count` 기본 1, 최대 200.
- 캔들 응답은 최신순. 공통 필드 `market`, `candle_date_time_utc`, `candle_date_time_kst`(시간대 없는
  `yyyy-MM-dd'T'HH:mm:ss` 문자열), `opening_price`, `high_price`, `low_price`, `trade_price`, `timestamp`(ms),
  `candle_acc_trade_price`, `candle_acc_trade_volume`. 분봉 `unit`, 일봉 `prev_closing_price`/`change_price`/
  `change_rate`/`converted_trade_price`, 주·월·연봉 `first_day_of_period`.
- 현재가 필드: `trade_price`(현재가), `signed_change_rate`(전일 대비 비율, 0.015 = +1.5%), `acc_trade_price_24h`,
  `trade_timestamp`(ms) 등. `change` ∈ EVEN/RISE/FALL.
- 잔고 필드: `currency`, `balance`(주문 가능), `locked`(묶임), `avg_buy_price`, `avg_buy_price_modified`,
  `unit_currency` — 숫자가 **문자열** 로 오므로 `Decimal` 로 다룬다.

## 주문 API (Phase 7 구현, `reference/new-order` · `order-test` · `get-order` · `cancel-order` · `list-open-orders` · `available-order-information`, 2026-09 기준)

- `POST /v1/orders` 주문 생성 (order 그룹 12회/초, 포켓). 본문 JSON: `market`, `side`(bid/ask), `ord_type`(limit/price/market/best), `volume`, `price`, `identifier`, `time_in_force`(fok/ioc/post_only), `smp_type`. 응답 201 Order.
  * 시장가 매수: `ord_type=price` + `price`=매수 총액(KRW), volume 없음. 시장가 매도: `ord_type=market` + `volume`=수량, price 없음.
  * `identifier`: 계정 전체에서 고유, 최대 64자, 실패한 주문의 값도 재사용 불가(`duplicated_identifier`). 2024-10-18 이후 주문에만 응답에 포함.
- `POST /v1/orders/test` 주문 생성 테스트 (order-test 그룹 8회/초): 같은 본문으로 검증만 하고 주문을 만들지 않는다.
- `GET /v1/order?uuid=|identifier=` 개별 조회(체결 목록 `trades[]`: price, volume, funds, side, created_at, trend 포함).
- `DELETE /v1/order?uuid=|identifier=` 취소 접수. `GET /v1/orders/open?market&states[]&page&limit&order_by` 체결 대기, `GET /v1/orders/closed` 종료 주문.
- `GET /v1/orders/chance?market=` 주문 가능 정보: `bid_fee`/`ask_fee`, `market.state`, `market.bid.min_total`/`ask.min_total`, `max_total`, `bid_types`/`ask_types`, `bid_account`/`ask_account` 잔고.
- Order 필드: `uuid`, `state`(wait/watch/done/cancel), `executed_volume`, `paid_fee`, `locked`, `trades_count`, `remaining_volume`, `reserved_fee` 등. 숫자는 문자열 소수.
- 권한: 조회는 [주문조회], 생성·취소는 [주문하기]. 에러: `insufficient_funds_bid/ask`, `under_min_total_bid/ask`, `create_bid_error`, `duplicated_identifier`, `out_of_scope`(권한 없음).

## 주문 관련 참고 (원문 유지)

- 원화 마켓 최소 주문 금액 **5,000 KRW**, 가격 구간별 호가 단위 표는 `docs/krw-market-info` (2025-07-31 정책 변경 반영).
- 주문 생성 `POST /v1/orders`(order 그룹), 주문 생성 테스트 `POST /v1/orders/test`(order-test 그룹, 실제 주문 없음),
  주문 가능 정보 `GET /v1/orders/chance`, 체결 대기 주문 `GET /v1/orders/open`, 일괄 취소 `DELETE /v1/orders/open`.
- 자전거래 체결 방지(SMP) 옵션, `identifier`(클라이언트 주문 ID, 중복 시 `duplicated_identifier`) 지원 → 중복 주문 방지에 활용 예정.

## 기타

- 공식 Python SDK `upbit-sdk`(Python 3.9+)가 있다(`docs/python-sdk`). 이 프로젝트는 재시도·Rate Limit·테스트용 전송 계층
  주입을 직접 제어하기 위해 httpx 기반의 얇은 클라이언트를 사용한다. 필요하면 SDK 로 교체할 수 있는 구조다.
## WebSocket (`reference/websocket-guide` 2026-09-23, `websocket-{ticker,trade,orderbook,candle}` 2026-09-22 갱신)

- 엔드포인트: Public `wss://api.upbit.com/websocket/v1` (ticker/trade/orderbook/candle),
  Private `wss://api.upbit.com/websocket/v1/private` (myOrder/myAsset/announcement, `Authorization: Bearer {JWT}` 헤더,
  query_hash 없음), `.../websocket/v1/info` (indicator).
- 요청: JSON 배열 `[{"ticket": "고유문자열"}, {"type": "trade", "codes": ["KRW-BTC"]}, ..., {"format": "DEFAULT"}]`.
  `codes` 는 대문자. 옵션 `is_only_snapshot` / `is_only_realtime`. 포맷 `DEFAULT|SIMPLE|JSON_LIST|SIMPLE_LIST`.
- 호가: `codes` 에 `KRW-BTC.15` 처럼 호가 쌍 개수(1/5/15/30, 기본 30) 지정, `level` 로 모아보기 단위(KRW 마켓만).
- 캔들: `type` 이 `candle.1s|1m|3m|5m|10m|15m|30m|60m|240m`. 1초 주기, 체결이 있을 때만 전송, 같은 기준 시각이 여러 번 오면 마지막이 최신.
- 수신 메시지 공통: `type`, `code`, `timestamp`(ms), `stream_type`(SNAPSHOT|REALTIME). 체결은 `sequential_id`(고유),
  `ask_bid`(ASK 매도/BID 매수), `best_ask/bid_price·size`. 호가는 `orderbook_units[]`(ask_price, bid_price, ask_size, bid_size),
  첫 원소가 최우선 호가. 현재가의 `is_trading_suspended`, `market_warning` 은 Deprecated.
- 연결 관리: 120초 송수신 없으면 서버가 끊음 → PING 프레임 또는 "PING" 텍스트(응답 `{"status":"UP"}` 10초 간격). 압축(permessage-deflate) 지원.
- 에러: `{"error": {"name", "message"}}` — `INVALID_AUTH`, `WRONG_FORMAT`, `NO_TICKET`, `NO_TYPE`, `NO_CODES`, `INVALID_PARAM`, `TOO_MANY_REQUEST`.
- Rate Limit: 연결 초당 5회(비인증 IP/인증 포켓), 데이터 요청 메시지 초당 5회·분당 100회(커넥션). 잔여 요청 수 헤더는 없음 → 클라이언트가 직접 관리.
- 구독 중인 스트림 목록 조회 REST: `reference/list-subscriptions`.
