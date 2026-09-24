"""프로젝트 전역 예외 계층.

모든 예외는 ``TraderError`` 를 상속한다. 업비트 API 관련 예외는 ``UpbitError`` 아래에 둔다.

HTTP 상태 코드 → 예외 매핑은 공식 문서
https://docs.upbit.com/kr/reference/rest-api-guide (2026-08-31 갱신본) 기준:

- 400 / 404 : 요청 오류(파라미터·잔고 부족·최소 주문 금액 미달 등) → 재시도 금지
- 401 / 403 : 인증·권한 오류(jwt_verification, nonce_used, no_authorization_ip, out_of_scope 등) → 재시도 금지
- 418       : 429 누적으로 일시 차단 → 재시도 금지, 안내된 시간 이후 수동 재개
- 429       : 초당 요청 한도 초과 → 다음 초 경계까지 대기 후 재시도 가능
- 5xx       : 서버 내부 오류 → 제한적 재시도 가능
"""

from __future__ import annotations


class TraderError(Exception):
    """프로젝트 공통 최상위 예외."""


class ConfigError(TraderError):
    """설정(환경변수) 오류."""


class LiveTradingDisabledError(TraderError):
    """LIVE 주문이 허용되지 않은 상태에서 실제 주문을 시도한 경우."""


class MarketDataError(TraderError):
    """캔들·시세 데이터가 비었거나 규약(정렬·중복·OHLC 일관성)을 어긴 경우."""


class StrategyError(TraderError):
    """전략 설정·파라미터 오류 또는 Look-ahead 검사 실패."""


class UpbitError(TraderError):
    """업비트 API 관련 최상위 예외."""


class UpbitNetworkError(UpbitError):
    """타임아웃·연결 실패 등 응답 자체를 받지 못한 경우."""


class UpbitResponseError(UpbitError):
    """응답은 받았지만 형식이 예상과 달라 해석할 수 없는 경우."""


class UpbitAPIError(UpbitError):
    """HTTP 에러 응답(4xx/5xx)을 받은 경우.

    ``name`` 은 Exchange API 에서는 문자열 에러 코드(예: ``insufficient_funds_bid``),
    Quotation API 에서는 정수(HTTP 상태 코드)로 내려온다.
    """

    #: 이 예외 유형이 자동 재시도 대상인지 여부 (하위 클래스에서 재정의)
    retryable: bool = False

    def __init__(
        self,
        status_code: int,
        name: str | int | None,
        message: str,
        *,
        remaining_req: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.name = name
        self.message = message
        self.remaining_req = remaining_req
        super().__init__(f"HTTP {status_code} [{name}] {message}")


class UpbitBadRequestError(UpbitAPIError):
    """400 / 404: 요청 내용 자체가 잘못됨."""


class UpbitAuthError(UpbitAPIError):
    """401 / 403: 인증 실패 또는 권한 부족."""


class UpbitRateLimitError(UpbitAPIError):
    """429: 초당 요청 한도 초과."""

    retryable = True


class UpbitBlockedError(UpbitAPIError):
    """418: 반복 위반으로 일시 차단됨. 자동 재시도하지 않는다."""


class UpbitServerError(UpbitAPIError):
    """5xx: 업비트 서버 오류."""

    retryable = True


def make_api_error(
    status_code: int,
    name: str | int | None,
    message: str,
    *,
    remaining_req: str | None = None,
) -> UpbitAPIError:
    """HTTP 상태 코드에 맞는 ``UpbitAPIError`` 하위 예외를 만든다."""
    cls: type[UpbitAPIError]
    if status_code == 429:
        cls = UpbitRateLimitError
    elif status_code == 418:
        cls = UpbitBlockedError
    elif status_code in (401, 403):
        cls = UpbitAuthError
    elif 400 <= status_code < 500:
        cls = UpbitBadRequestError
    elif status_code >= 500:
        cls = UpbitServerError
    else:
        cls = UpbitAPIError
    return cls(status_code, name, message, remaining_req=remaining_req)
