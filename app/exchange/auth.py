"""업비트 JWT 인증 토큰 생성.

공식 문서 https://docs.upbit.com/kr/reference/auth (2026-09-22 갱신본) 기준:

- 헤더 ``alg`` 는 ``HS512`` 권장 (HMAC with SHA-512).
- 페이로드: ``access_key``, ``nonce`` (매 요청 새 UUID),
  쿼리 파라미터 또는 본문이 있으면 ``query_hash`` + ``query_hash_alg="SHA512"``.
- ``query_hash`` 는 **URL 인코딩되지 않은** 쿼리 문자열을 그대로 SHA512 한 값이며
  파라미터 순서를 바꾸지 않는다.
  * 배열 파라미터(``states[]``)는 키 반복: ``states[]=wait&states[]=watch``
  * 쉼표 구분 파라미터(``markets``)는 ``markets=KRW-BTC,KRW-ETH`` 그대로
- POST 요청은 JSON 본문의 모든 key-value 를 ``key=value&key=value`` 로 가공해 해시한다.
- Secret Key 는 Base64 인코딩되어 있지 않다 (디코딩 금지).
- 전송: ``Authorization: Bearer {JWT}``
"""

from __future__ import annotations

import hashlib
import uuid
import warnings
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import unquote, urlencode

import jwt

try:  # PyJWT >= 2.10 은 HMAC 키가 해시 길이(HS512: 64바이트)보다 짧으면 경고를 낸다.
    from jwt.warnings import InsecureKeyLengthWarning
except ImportError:  # pragma: no cover - 구버전 PyJWT
    InsecureKeyLengthWarning = None  # type: ignore[assignment,misc]

JWT_ALGORITHM = "HS512"
QUERY_HASH_ALG = "SHA512"

QueryParams = Mapping[str, Any] | Sequence[tuple[str, Any]]


def _to_str(value: Any) -> Any:
    """bool 은 업비트가 기대하는 소문자 문자열로 바꾼다. 나머지는 그대로 둔다."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def normalize_params(params: QueryParams | None) -> list[tuple[str, Any]]:
    """None 값을 제거하고 순서를 유지한 (key, value) 목록으로 정규화한다.

    리스트 값은 그대로 두어 ``urlencode(doseq=True)`` 가 키를 반복하도록 한다.
    """
    if not params:
        return []
    items = params.items() if isinstance(params, Mapping) else params
    normalized: list[tuple[str, Any]] = []
    for key, value in items:
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            values = [_to_str(v) for v in value if v is not None]
            if values:
                normalized.append((key, values))
        else:
            normalized.append((key, _to_str(value)))
    return normalized


def build_query_string(params: QueryParams | None) -> str:
    """해시 계산용 쿼리 문자열. 공식 예제와 동일하게 ``unquote(urlencode(params, doseq=True))``."""
    normalized = normalize_params(params)
    if not normalized:
        return ""
    return unquote(urlencode(normalized, doseq=True))


def build_query_hash(query_string: str) -> str:
    """쿼리 문자열의 SHA512 hex digest."""
    return hashlib.sha512(query_string.encode("utf-8")).hexdigest()


class UpbitAuth:
    """Access/Secret Key 로 요청별 JWT 를 만든다. 키 값은 절대 로그·repr 에 노출하지 않는다."""

    def __init__(self, access_key: str, secret_key: str) -> None:
        if not access_key or not access_key.strip():
            raise ValueError("access_key 가 비어 있습니다")
        if not secret_key or not secret_key.strip():
            raise ValueError("secret_key 가 비어 있습니다")
        self._access_key = access_key.strip()
        self._secret_key = secret_key.strip()

    @property
    def access_key(self) -> str:
        return self._access_key

    def build_payload(self, query_string: str = "") -> dict[str, str]:
        payload = {"access_key": self._access_key, "nonce": str(uuid.uuid4())}
        if query_string:
            payload["query_hash"] = build_query_hash(query_string)
            payload["query_hash_alg"] = QUERY_HASH_ALG
        return payload

    def create_token(self, query_string: str = "") -> str:
        """쿼리 문자열(인코딩 전)을 받아 서명된 JWT 를 돌려준다.

        업비트가 발급하는 Secret Key 는 40자(=40바이트)라 RFC 7518 이 권장하는 HS512 최소 키 길이
        (64바이트)보다 짧다. 키 형식은 거래소가 정한 것이므로 PyJWT 의 길이 경고만 억제한다.
        """
        payload = self.build_payload(query_string)
        with warnings.catch_warnings():
            if InsecureKeyLengthWarning is not None:
                warnings.simplefilter("ignore", InsecureKeyLengthWarning)
            token = jwt.encode(payload, self._secret_key, algorithm=JWT_ALGORITHM)
        return token if isinstance(token, str) else token.decode("utf-8")

    def authorization_header(self, query_string: str = "") -> dict[str, str]:
        return {"Authorization": f"Bearer {self.create_token(query_string)}"}

    def __repr__(self) -> str:
        return f"UpbitAuth(access_key={self._access_key[:4]}****)"
