"""환경변수 기반 설정.

``.env`` 파일(프로젝트 루트) 또는 실제 환경변수에서 읽는다. API Key 는 코드에 절대 넣지 않는다.

안전장치:
- 기본 거래 모드는 PAPER, ``LIVE_TRADING_ENABLED`` 기본값은 false.
- 실제 주문은 ``TRADING_MODE=LIVE`` 이면서 ``LIVE_TRADING_ENABLED=true`` 일 때만 허용된다
  (``Settings.is_live_trading_allowed``). 둘 중 하나라도 아니면 실제 주문 API를 호출하지 않는다.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

if TYPE_CHECKING:
    from app.notify.base import EventKind
    from app.risk.config import RiskConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"

# 업비트 마켓 코드 형식: "KRW-BTC", "BTC-ETH", "USDT-BTC"
MARKET_CODE_RE = re.compile(r"^[A-Z]+-[A-Z0-9]+$")


class TradingMode(StrEnum):
    """거래 모드. 값은 대소문자 구분 없이 읽는다 (paper == PAPER)."""

    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"

    @classmethod
    def _missing_(cls, value: object) -> TradingMode | None:
        if isinstance(value, str):
            normalized = value.strip().upper()
            for member in cls:
                if member.value == normalized:
                    return member
        return None


def _coerce_scalar(value: str) -> Any:
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def parse_params_text(text: str | None) -> dict[str, Any]:
    """전략 파라미터 문자열 → dict.

    두 형식을 받는다.
    - JSON 객체: ``{"short_window": 20, "long_window": 60}``
    - key=value 목록(쉼표·세미콜론·공백 구분): ``short_window=20,long_window=60`` — 셸 따옴표 문제가 없어 CLI 에 권장.
      값은 int → float → bool(true/false) → null → 문자열 순으로 해석한다.
    """
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"파라미터 JSON 해석 실패: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("파라미터 JSON 은 객체({...})여야 합니다")
        return parsed
    out: dict[str, Any] = {}
    for pair in re.split(r"[,;\s]+", text):
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(f"파라미터는 'key=value' 형식이어야 합니다: {pair!r}")
        key, value = pair.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"파라미터 이름이 비어 있습니다: {pair!r}")
        out[key] = _coerce_scalar(value.strip())
    return out


def mask_secret(value: str | None, visible: int = 4) -> str:
    """로그·화면 표시용 마스킹. 앞 4자만 남긴다."""
    if not value:
        return "(없음)"
    if len(value) <= visible:
        return "*" * len(value)
    return value[:visible] + "*" * (len(value) - visible)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(DEFAULT_ENV_FILE),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    # ----- Upbit API -----
    upbit_access_key: SecretStr | None = None
    upbit_secret_key: SecretStr | None = None
    upbit_api_url: str = "https://api.upbit.com"
    upbit_ws_url: str = "wss://api.upbit.com/websocket/v1"
    upbit_ws_private_url: str = "wss://api.upbit.com/websocket/v1/private"

    # ----- 거래 모드 / 안전장치 -----
    trading_mode: TradingMode = TradingMode.PAPER
    live_trading_enabled: bool = False

    # ----- 거래 대상 -----
    markets: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["KRW-BTC", "KRW-ETH"])

    # ----- 전략 -----
    strategy_name: str = "ma_cross"
    # NoDecode: pydantic-settings 의 JSON 자동 해석을 끄고 아래 validator 가 key=value / JSON 을 직접 해석한다.
    strategy_params: Annotated[dict[str, Any], NoDecode] = Field(default_factory=dict)
    candle_interval: str = "60m"

    # ----- 모의매매 (Phase 5) -----
    paper_initial_cash: float = Field(default=1_000_000.0, gt=0, description="모의 계좌 초기 현금 (KRW)")
    paper_fee_rate: float = Field(default=0.0005, ge=0, lt=0.1, description="모의 체결 수수료율")
    paper_slippage_rate: float = Field(default=0.0005, ge=0, lt=0.1, description="모의 체결 슬리피지")
    position_fraction: float = Field(default=1.0, gt=0, le=1.0, description="매수 시 현금 사용 비율")
    warmup_candles: int = Field(default=300, ge=10, le=2000, description="시작 시 받아 둘 과거 캔들 수")
    snapshot_interval_seconds: int = Field(default=300, ge=10, description="자산 스냅샷 주기(초)")
    candle_grace_seconds: float = Field(default=3.0, ge=0, le=60, description="캔들 경계 후 확정 대기(초)")
    price_max_age_seconds: float = Field(default=30.0, gt=0, description="이보다 오래된 시세로는 체결하지 않음")
    candle_anomaly_pct: float = Field(default=0.3, gt=0, le=5,
                                      description="직전 종가 대비 이 비율 넘게 튄 캔들은 이상치로 보고 신호 실행 보류")
    exit_confirm_ticks: int = Field(default=2, ge=1, le=10,
                                    description="청산 조건이 연속 이만큼의 시세에서 관측돼야 매도 (단일 이상 틱 방어)")

    # ----- 리스크 관리 (Phase 6) — RiskConfig 로 묶여 백테스트·모의매매·실거래가 공유한다 -----
    risk_max_order_amount: float | None = Field(default=None, gt=0, description="거래당 최대 투자금 KRW")
    risk_max_position_ratio: float = Field(default=1.0, gt=0, le=1.0, description="전체 자산 대비 코인 평가액 상한")
    risk_max_open_positions: int = Field(default=1, ge=1, le=50, description="동시 보유 최대 마켓 수")
    risk_daily_loss_limit_pct: float | None = Field(default=0.03, gt=0, lt=1, description="당일 손실 한도")
    risk_max_consecutive_losses: int | None = Field(default=3, ge=1, le=100, description="연속 손실 한도 (비우면 끔)")
    risk_stop_loss_pct: float | None = Field(default=0.05, gt=0, lt=1, description="손절 비율 (비우면 끔)")
    risk_take_profit_pct: float | None = Field(default=None, gt=0, description="익절 비율 (비우면 끔)")
    risk_trailing_stop_pct: float | None = Field(default=None, gt=0, lt=1, description="추적 손절 비율 (비우면 끔)")
    risk_price_deviation_limit: float | None = Field(default=0.10, gt=0, lt=1, description="시세 괴리 한도")
    risk_cooldown_seconds: float = Field(default=0.0, ge=0, description="청산 후 같은 마켓 재진입 대기(초)")

    # ----- 대시보드 (Phase 8) -----
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = Field(default=8000, ge=1, le=65535)
    dashboard_token: SecretStr | None = None  # 설정하면 변경·제어 API 에 X-Auth-Token 헤더 필요
    # 포켓 자산 이전용 메인포켓 키 (권한: 포켓관리만). 없으면 대시보드에서 메인→봇 포켓 이전은 불가
    upbit_pocket_access_key: SecretStr | None = None
    upbit_pocket_secret_key: SecretStr | None = None

    # ----- 알림 (Phase 9) -----
    telegram_bot_token: SecretStr | None = None  # @BotFather 가 준 봇 토큰
    telegram_chat_id: str | None = None  # 알림 받을 채팅 ID (개인·그룹)
    discord_webhook_url: SecretStr | None = None  # Discord 채널 웹훅 URL
    # all / off / 쉼표 목록 (buy, sell, order_filled, stop_loss, daily_loss_limit, api_error, bot_start, bot_stop ...)
    notify_events: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["all"])
    notify_error_cooldown_seconds: float = Field(default=300.0, ge=0, description="같은 API 오류 반복 알림 억제(초)")
    notify_log: bool = True  # 알림 본문을 로그에도 남긴다 (채널이 없어도 동작 확인 가능)

    # ----- 저장소 / 로그 -----
    database_url: str = "sqlite:///./data/trader.db"
    log_level: str = "INFO"
    log_dir: Path = Path("logs")

    # ----- HTTP 클라이언트 -----
    http_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    http_max_retries: int = Field(default=3, ge=0, le=10)

    # ------------------------------------------------------------------
    # 검증
    # ------------------------------------------------------------------
    @field_validator("notify_events", mode="before")
    @classmethod
    def _parse_notify_events(cls, value: Any) -> list[str]:
        from app.notify.base import parse_event_kinds  # notify.base 는 설정을 모르므로 순환 import 없음

        return [kind.value for kind in parse_event_kinds(value)]

    @field_validator("markets", mode="before")
    @classmethod
    def _parse_markets(cls, value: Any) -> list[str]:
        """"KRW-BTC,KRW-ETH" 같은 쉼표 문자열 또는 리스트를 정규화한다."""
        if isinstance(value, str):
            value = value.split(",")
        items = [str(item).strip().upper() for item in value if str(item).strip()]
        if not items:
            raise ValueError("MARKETS 에는 최소 1개의 마켓 코드가 필요합니다 (예: KRW-BTC)")
        invalid = [item for item in items if not MARKET_CODE_RE.match(item)]
        if invalid:
            raise ValueError(f"잘못된 마켓 코드 {invalid}: 'KRW-BTC' 형식이어야 합니다")
        return list(dict.fromkeys(items))  # 순서 유지 중복 제거

    @field_validator(
        "risk_max_order_amount", "risk_daily_loss_limit_pct", "risk_max_consecutive_losses",
        "risk_stop_loss_pct", "risk_take_profit_pct", "risk_trailing_stop_pct", "risk_price_deviation_limit",
        mode="before",
    )
    @classmethod
    def _blank_or_off_to_none(cls, value: Any) -> Any:
        """'' / off / none / 0 은 '끔'(None) 으로 읽는다."""
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("", "off", "none", "null", "0"):
                return None
        if isinstance(value, (int, float)) and value == 0:
            return None
        return value

    @field_validator(
        "upbit_access_key", "upbit_secret_key", "upbit_pocket_access_key", "upbit_pocket_secret_key", "dashboard_token",
        "telegram_bot_token", "telegram_chat_id", "discord_webhook_url",
        mode="before",
    )
    @classmethod
    def _blank_key_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("strategy_params", mode="before")
    @classmethod
    def _parse_strategy_params(cls, value: Any) -> Any:
        """환경변수에서는 문자열로 온다: JSON 객체 또는 key=value 목록."""
        if isinstance(value, str):
            return parse_params_text(value)
        return value

    @field_validator("candle_interval")
    @classmethod
    def _validate_interval(cls, value: str) -> str:
        from app.exchange.models import CandleInterval  # 순환 import 방지용 지연 import

        return CandleInterval.parse(value).value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"LOG_LEVEL 값이 잘못되었습니다: {value}")
        return level

    # ------------------------------------------------------------------
    # 편의 속성
    # ------------------------------------------------------------------
    @property
    def has_api_keys(self) -> bool:
        return bool(
            self.upbit_access_key
            and self.upbit_secret_key
            and self.upbit_access_key.get_secret_value().strip()
            and self.upbit_secret_key.get_secret_value().strip()
        )

    @property
    def has_pocket_keys(self) -> bool:
        return bool(self.upbit_pocket_access_key and self.upbit_pocket_secret_key)

    @property
    def notify_channels(self) -> list[str]:
        """설정된 알림 채널 이름 (비밀값은 노출하지 않는다)."""
        channels: list[str] = []
        if self.telegram_bot_token and self.telegram_chat_id:
            channels.append("telegram")
        if self.discord_webhook_url:
            channels.append("discord")
        if self.notify_log:
            channels.append("log")
        return channels

    def notify_event_kinds(self) -> list[EventKind]:
        from app.notify.base import parse_event_kinds

        return parse_event_kinds(self.notify_events)

    @property
    def is_live_trading_allowed(self) -> bool:
        """실제 주문 허용 여부. 반드시 두 조건이 모두 참이어야 한다."""
        return self.trading_mode is TradingMode.LIVE and self.live_trading_enabled is True

    def risk_config(self) -> RiskConfig:
        """Settings 의 risk_* 값으로 RiskConfig 를 만든다 (나중에 DB·웹 입력으로 대체 가능)."""
        from app.risk.config import RiskConfig  # 순환 import 방지

        return RiskConfig(
            position_fraction=self.position_fraction,
            max_order_amount=self.risk_max_order_amount,
            max_position_ratio=self.risk_max_position_ratio,
            max_open_positions=self.risk_max_open_positions,
            daily_loss_limit_pct=self.risk_daily_loss_limit_pct,
            max_consecutive_losses=self.risk_max_consecutive_losses,
            stop_loss_pct=self.risk_stop_loss_pct,
            take_profit_pct=self.risk_take_profit_pct,
            trailing_stop_pct=self.risk_trailing_stop_pct,
            price_deviation_limit=self.risk_price_deviation_limit,
            cooldown_seconds=self.risk_cooldown_seconds,
        )

    def safety_warnings(self) -> list[str]:
        """시작 시 사용자에게 보여줄 안전 관련 경고 목록."""
        warnings: list[str] = []
        if self.trading_mode is TradingMode.LIVE and not self.live_trading_enabled:
            warnings.append(
                "TRADING_MODE=LIVE 이지만 LIVE_TRADING_ENABLED=false 이므로 실제 주문은 차단됩니다."
            )
        if self.trading_mode is not TradingMode.LIVE and self.live_trading_enabled:
            warnings.append(
                f"LIVE_TRADING_ENABLED=true 이지만 TRADING_MODE={self.trading_mode.value} 이므로 "
                "실제 주문은 차단됩니다."
            )
        if self.is_live_trading_allowed:
            warnings.append("!!! LIVE 모드가 활성화되어 있습니다. 실제 자금이 거래됩니다 !!!")
        if self.trading_mode is TradingMode.LIVE and not self.has_api_keys:
            warnings.append("LIVE 모드인데 API Key 가 없습니다. 잔고·주문 API를 사용할 수 없습니다.")
        return warnings

    def summary(self) -> dict[str, Any]:
        """비밀값을 마스킹한 설정 요약 (로그·상태 API용)."""
        access = self.upbit_access_key.get_secret_value() if self.upbit_access_key else None
        return {
            "trading_mode": self.trading_mode.value,
            "live_trading_enabled": self.live_trading_enabled,
            "live_trading_allowed": self.is_live_trading_allowed,
            "has_api_keys": self.has_api_keys,
            "upbit_access_key": mask_secret(access),
            "notify_channels": self.notify_channels,
            "notify_events": [k.value for k in self.notify_event_kinds()],
            "upbit_api_url": self.upbit_api_url,
            "upbit_ws_url": self.upbit_ws_url,
            "markets": list(self.markets),
            "strategy_name": self.strategy_name,
            "strategy_params": dict(self.strategy_params),
            "candle_interval": self.candle_interval,
            "database_url": self.database_url,
            "log_level": self.log_level,
            "log_dir": str(self.log_dir),
            "http_timeout_seconds": self.http_timeout_seconds,
            "http_max_retries": self.http_max_retries,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """프로세스 전역 설정 (한 번만 읽는다). 테스트에서는 ``Settings(...)`` 를 직접 만든다."""
    return Settings()
