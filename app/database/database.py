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

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import PROJECT_ROOT
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
                file_path = Path(path).expanduser()
                if not file_path.is_absolute():
                    file_path = PROJECT_ROOT / file_path
                    url = f"sqlite:///{file_path.as_posix()}"
                    self.url = url
                file_path.parent.mkdir(parents=True, exist_ok=True)
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
        added = self.add_missing_columns()
        if added:
            log.info("DB 스키마 보강: %s", ", ".join(added))
        log.info("DB 준비 완료: %s", self.url)

    def add_missing_columns(self) -> list[str]:
        """모델에는 있는데 기존 테이블에 없는 컬럼을 ``ALTER TABLE ... ADD COLUMN`` 으로 보강한다.

        ``create_all`` 은 새 테이블만 만들고 기존 테이블의 컬럼은 건드리지 않는다. 이전 버전에서 만든 DB 를
        그대로 쓰는 경우(예: Phase 7 → 8 에서 ``engine_status`` 에 컬럼 추가)를 위한 최소 마이그레이션이다.
        컬럼 삭제·타입 변경은 하지 않는다. 반환값은 ``테이블.컬럼`` 목록.
        """
        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())
        added: list[str] = []
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                if table.name not in existing_tables:
                    continue
                present = {c["name"] for c in inspector.get_columns(table.name)}
                for col in table.columns:
                    if col.name in present:
                        continue
                    col_type = col.type.compile(dialect=self.engine.dialect)
                    ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type}"
                    literal = _scalar_default_literal(col)
                    if literal is not None:
                        ddl += f" NOT NULL DEFAULT {literal}" if not col.nullable else f" DEFAULT {literal}"
                    conn.execute(text(ddl))
                    added.append(f"{table.name}.{col.name}")
        return added

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


def _scalar_default_literal(col) -> str | None:
    """컬럼의 파이썬 기본값이 단순 스칼라면 SQL 리터럴로 돌려준다(bool → 1/0, 문자열은 따옴표)."""
    default = col.default
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None
