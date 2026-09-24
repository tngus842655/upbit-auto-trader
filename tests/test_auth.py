"""JWT 인증 모듈 테스트 — 공식 인증 문서의 규칙을 그대로 검증한다."""

from __future__ import annotations

import hashlib

import jwt
import pytest

from app.exchange.auth import (
    JWT_ALGORITHM,
    UpbitAuth,
    build_query_hash,
    build_query_string,
    normalize_params,
)

ACCESS = "a7Xd92LmQW3vBtRzYpMj5CxNKeT1HuVs0fFgJcAw"
SECRET = "super-secret-key-not-base64"


class TestQueryString:
    def test_doc_example_with_array_param(self) -> None:
        # 공식 문서 Python 예제와 동일한 입력·출력
        params = {"market": "KRW-BTC", "states[]": ["wait", "watch"], "limit": 10}
        assert build_query_string(params) == "market=KRW-BTC&states[]=wait&states[]=watch&limit=10"

    def test_comma_separated_value_is_not_encoded(self) -> None:
        assert build_query_string({"markets": "KRW-BTC,KRW-ETH"}) == "markets=KRW-BTC,KRW-ETH"

    def test_order_is_preserved(self) -> None:
        assert build_query_string([("b", 1), ("a", 2)]) == "b=1&a=2"

    def test_none_is_dropped_and_bool_is_lowercase(self) -> None:
        assert normalize_params({"to": None, "is_details": True, "count": 5}) == [
            ("is_details", "true"),
            ("count", 5),
        ]
        assert build_query_string({"to": None}) == ""

    def test_empty(self) -> None:
        assert build_query_string(None) == ""
        assert build_query_string({}) == ""

    def test_query_hash_is_sha512_hex(self) -> None:
        qs = "market=KRW-BTC&limit=10"
        assert build_query_hash(qs) == hashlib.sha512(qs.encode()).hexdigest()
        assert len(build_query_hash(qs)) == 128


class TestUpbitAuth:
    def test_token_without_query(self) -> None:
        auth = UpbitAuth(ACCESS, SECRET)
        token = auth.create_token()
        header = jwt.get_unverified_header(token)
        assert header["alg"] == JWT_ALGORITHM == "HS512"
        payload = jwt.decode(token, SECRET, algorithms=["HS512"])
        assert payload["access_key"] == ACCESS
        assert payload["nonce"]
        assert "query_hash" not in payload
        assert "query_hash_alg" not in payload

    def test_token_with_query_contains_hash(self) -> None:
        auth = UpbitAuth(ACCESS, SECRET)
        qs = "market=KRW-BTC&states[]=wait&states[]=watch&limit=10"
        payload = jwt.decode(auth.create_token(qs), SECRET, algorithms=["HS512"])
        assert payload["query_hash"] == hashlib.sha512(qs.encode()).hexdigest()
        assert payload["query_hash_alg"] == "SHA512"

    def test_nonce_changes_every_time(self) -> None:
        auth = UpbitAuth(ACCESS, SECRET)
        nonces = {jwt.decode(auth.create_token(), SECRET, algorithms=["HS512"])["nonce"] for _ in range(5)}
        assert len(nonces) == 5

    def test_signature_uses_raw_secret(self) -> None:
        # Secret Key 는 Base64 디코딩 없이 그대로 서명 키로 쓴다 → 다른 키로는 검증 실패
        token = UpbitAuth(ACCESS, SECRET).create_token()
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(token, "wrong-secret", algorithms=["HS512"])

    def test_authorization_header_format(self) -> None:
        header = UpbitAuth(ACCESS, SECRET).authorization_header()
        assert list(header) == ["Authorization"]
        assert header["Authorization"].startswith("Bearer ey")

    @pytest.mark.parametrize("access, secret", [("", SECRET), (ACCESS, ""), ("   ", SECRET)])
    def test_empty_keys_rejected(self, access: str, secret: str) -> None:
        with pytest.raises(ValueError):
            UpbitAuth(access, secret)

    def test_repr_does_not_leak_secret(self) -> None:
        text = repr(UpbitAuth(ACCESS, SECRET))
        assert SECRET not in text
        assert ACCESS not in text
