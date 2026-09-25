"""설정 테스트 — 특히 LIVE 이중 안전장치와 마켓 파싱."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config.settings import Settings, TradingMode, mask_secret, parse_params_text


class TestDefaults:
    def test_safe_defaults(self, make_settings) -> None:
        s = make_settings()
        assert s.trading_mode is TradingMode.PAPER
        assert s.live_trading_enabled is False
        assert s.is_live_trading_allowed is False
        assert s.has_api_keys is False
        assert s.markets == ["KRW-BTC", "KRW-ETH"]
        assert s.upbit_api_url == "https://api.upbit.com"
        assert s.safety_warnings() == []


class TestLiveGuardFlags:
    def test_live_mode_alone_is_not_enough(self, make_settings) -> None:
        s = make_settings(trading_mode="LIVE")
        assert s.is_live_trading_allowed is False
        assert any("LIVE_TRADING_ENABLED=false" in w for w in s.safety_warnings())

    def test_flag_alone_is_not_enough(self, make_settings) -> None:
        s = make_settings(live_trading_enabled=True)
        assert s.is_live_trading_allowed is False
        assert any("TRADING_MODE=PAPER" in w for w in s.safety_warnings())

    def test_backtest_with_flag_is_not_allowed(self, make_settings) -> None:
        s = make_settings(trading_mode="backtest", live_trading_enabled=True)
        assert s.trading_mode is TradingMode.BACKTEST
        assert s.is_live_trading_allowed is False

    @pytest.mark.parametrize("raw", ["1", "yes", "on", "Y", "t", "TRUE ", "enabled"])
    def test_live_flag_rejects_loose_truthy_strings(self, make_settings, raw: str) -> None:
        """감사 LOW-4 — 실제 자금 스위치는 정확히 true/false 만 받는다 (pydantic 기본 파싱의 1/yes/on 거부)."""
        if raw.strip().lower() == "true":
            assert make_settings(trading_mode="LIVE", live_trading_enabled=raw).is_live_trading_allowed is True
            return
        with pytest.raises(Exception, match="true 또는 false"):
            make_settings(live_trading_enabled=raw)

    def test_live_flag_accepts_exact_strings_and_bools(self, make_settings) -> None:
        assert make_settings(live_trading_enabled="false").live_trading_enabled is False
        assert make_settings(live_trading_enabled="").live_trading_enabled is False
        assert make_settings(live_trading_enabled="true").live_trading_enabled is True
        assert make_settings(live_trading_enabled=False).live_trading_enabled is False

    def test_both_conditions_allow_live(self, make_settings) -> None:
        s = make_settings(trading_mode="LIVE", live_trading_enabled=True)
        assert s.is_live_trading_allowed is True
        assert any("실제 자금" in w for w in s.safety_warnings())

    @pytest.mark.parametrize("raw", ["paper", "Paper", " PAPER ", "live", "BACKTEST"])
    def test_mode_is_case_insensitive(self, make_settings, raw: str) -> None:
        assert make_settings(trading_mode=raw).trading_mode.value == raw.strip().upper()

    def test_invalid_mode_rejected(self, make_settings) -> None:
        with pytest.raises(ValidationError):
            make_settings(trading_mode="REAL")


class TestMarkets:
    def test_comma_string_is_normalized(self, make_settings) -> None:
        s = make_settings(markets="krw-btc, KRW-ETH ,KRW-BTC,")
        assert s.markets == ["KRW-BTC", "KRW-ETH"]

    def test_list_input(self, make_settings) -> None:
        assert make_settings(markets=["KRW-XRP"]).markets == ["KRW-XRP"]

    @pytest.mark.parametrize("raw", ["BTC", "KRW_BTC", "krw-", ",,,", ""])
    def test_invalid_market_rejected(self, make_settings, raw: str) -> None:
        with pytest.raises(ValidationError):
            make_settings(markets=raw)

    def test_env_var_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MARKETS", "KRW-SOL,KRW-ADA")
        monkeypatch.setenv("TRADING_MODE", "backtest")
        monkeypatch.setenv("HTTP_MAX_RETRIES", "5")
        s = Settings(_env_file=None)
        assert s.markets == ["KRW-SOL", "KRW-ADA"]
        assert s.trading_mode is TradingMode.BACKTEST
        assert s.http_max_retries == 5


class TestApiKeys:
    def test_blank_keys_become_none(self, make_settings) -> None:
        s = make_settings(upbit_access_key="", upbit_secret_key="   ")
        assert s.upbit_access_key is None
        assert s.upbit_secret_key is None
        assert s.has_api_keys is False

    def test_both_keys_required(self, make_settings) -> None:
        assert make_settings(upbit_access_key="abc").has_api_keys is False
        assert make_settings(upbit_access_key="abcd1234", upbit_secret_key="xyz").has_api_keys is True

    def test_summary_masks_key(self, make_settings) -> None:
        """감사 LOW-3 — 요약에는 키의 어떤 부분도, DB URL 의 비밀번호도 나오지 않는다."""
        s = make_settings(upbit_access_key="abcdefgh12345678", upbit_secret_key="secret-value",
                          database_url="postgresql://bot:pa%40ss@db.local:5432/trader")
        summary = s.summary()
        assert summary["upbit_access_key"] == "설정됨" and "abcd" not in str(summary)
        assert summary["upbit_pocket_keys"] == "없음"
        assert summary["database_url"] == "postgresql://bot:***@db.local:5432/trader"
        assert "secret" not in str(summary) and "pa%40ss" not in str(summary)
        assert "secret-value" not in repr(s)
        assert make_settings().summary()["upbit_access_key"] == "없음"

    def test_mask_url_password(self) -> None:
        from app.config.settings import mask_url_password

        assert mask_url_password("sqlite:///data/trader.db") == "sqlite:///data/trader.db"
        assert mask_url_password("postgresql://u:p@h/db") == "postgresql://u:***@h/db"
        assert mask_url_password("postgresql://u@h/db") == "postgresql://u@h/db"
        assert mask_url_password("mysql://u:p:q@h:3306/db") == "mysql://u:***@h:3306/db"

    def test_mask_secret(self) -> None:
        assert mask_secret(None) == "(없음)"
        assert mask_secret("ab") == "**"
        assert mask_secret("abcdefgh") == "abcd****"


class TestValidation:
    def test_http_bounds(self, make_settings) -> None:
        with pytest.raises(ValidationError):
            make_settings(http_timeout_seconds=0)
        with pytest.raises(ValidationError):
            make_settings(http_max_retries=11)

    def test_log_level(self, make_settings) -> None:
        assert make_settings(log_level="debug").log_level == "DEBUG"
        with pytest.raises(ValidationError):
            make_settings(log_level="VERBOSE")


class TestStrategyParamsText:
    def test_key_value_list_with_type_inference(self) -> None:
        text = "short_window=20, long_window=60;volume_factor=1.5 flag=true nothing=null name=abc"
        assert parse_params_text(text) == {
            "short_window": 20, "long_window": 60, "volume_factor": 1.5, "flag": True, "nothing": None, "name": "abc",
        }

    def test_json_object(self) -> None:
        assert parse_params_text(' {"window": 10, "oversold": 25.5} ') == {"window": 10, "oversold": 25.5}

    @pytest.mark.parametrize("raw", ["", "   ", None])
    def test_empty(self, raw) -> None:
        assert parse_params_text(raw) == {}

    @pytest.mark.parametrize("raw", ["window", "=10", "{bad json", "[1, 2]"])
    def test_invalid(self, raw: str) -> None:
        with pytest.raises(ValueError):
            parse_params_text(raw)

    def test_settings_accepts_both_forms(self, monkeypatch: pytest.MonkeyPatch, make_settings) -> None:
        monkeypatch.setenv("STRATEGY_PARAMS", "short_window=5,long_window=20")
        assert Settings(_env_file=None).strategy_params == {"short_window": 5, "long_window": 20}
        assert make_settings(strategy_params='{"window": 10}').strategy_params == {"window": 10}
        with pytest.raises(ValidationError):
            make_settings(strategy_params="oops")
