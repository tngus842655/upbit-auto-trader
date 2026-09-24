"""로깅 설정.

- 콘솔 + 회전 파일(``logs/trader.log``, 10MB x 5개) 두 곳에 기록한다.
- 형식: ``2026-09-24 10:00:00 [INFO] app.exchange.upbit_client: 메시지``
- API Key·Secret Key 는 어디에서도 로그에 남기지 않는다 (클라이언트가 Authorization 헤더를 로그에 쓰지 않음).
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOG_FILENAME = "trader.log"


def setup_logging(
    level: str = "INFO",
    log_dir: Path | str | None = None,
    *,
    filename: str = DEFAULT_LOG_FILENAME,
) -> None:
    """루트 로거를 초기화한다. 여러 번 호출해도 핸들러가 중복되지 않는다."""
    root = logging.getLogger()
    root.setLevel(level.upper())

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_dir is not None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            directory / filename,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # HTTP 라이브러리의 요청 로그는 우리 클라이언트가 직접 남기므로 경고 이상만 통과시킨다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
