"""DB 연결·세션 관리.

- 개발 초기에는 SQLite(``sqlite:///./data/trader.db``), 나중에 PostgreSQL 로 바꿔도 코드는 그대로다.
- SQLite 는 WAL 모드로 열어 대시보드(읽기)와 봇(쓰기)이 동시에 접근해도 잠금이 덜 걸리게 한다.
- ``sqlite://`` (메모리) 는 테스트용으로 StaticPool 을 써서 하나의 연결을 공유한다.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base

log = logging.getLogger(__name__)


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        kwargs: dict = {}
        connect_args: dict = {}
        if url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            path = url.removeprefix("sqlite:///")
            if url in ("sqlite://", "sqlite:///:memory:") or path == ":memory:":
                kwargs["poolclass"] = StaticPool
            else:
                Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url, connect_args=connect_args, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - 드라이버 훅
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        log.info("DB 준비 완료: %s", self.url)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ping(self) -> bool:
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True

    def dispose(self) -> None:
        self.engine.dispose()
