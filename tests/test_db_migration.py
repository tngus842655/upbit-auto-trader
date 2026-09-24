"""이전 버전 DB 를 그대로 열었을 때 빠진 컬럼이 자동 보강되는지 — Phase 7 DB 를 Phase 8 에서 여는 상황."""

from __future__ import annotations

from sqlalchemy import inspect, text

from app.database import Database


def test_missing_columns_are_added(tmp_path) -> None:
    db_file = tmp_path / "old.db"
    db = Database(f"sqlite:///{db_file.as_posix()}")
    # Phase 7 시절의 engine_status: settings_version / restart_required 가 없다
    with db.engine.begin() as conn:
        conn.execute(text("CREATE TABLE engine_status (mode VARCHAR(10) PRIMARY KEY, status VARCHAR(12), pid INTEGER)"))
        conn.execute(text("INSERT INTO engine_status (mode, status, pid) VALUES ('paper', 'RUNNING', 7)"))

    db.create_all()

    cols = {c["name"] for c in inspect(db.engine).get_columns("engine_status")}
    assert {"settings_version", "restart_required", "strategy", "markets", "updated_at"} <= cols
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, pid, settings_version, restart_required FROM engine_status WHERE mode='paper'")
        ).one()
    assert tuple(row) == ("RUNNING", 7, 0, 0)  # 기존 행 유지 + 기본값 채움
    assert db.add_missing_columns() == []  # 두 번째 호출은 아무것도 안 함
    db.dispose()


def test_fresh_db_needs_no_migration(tmp_path) -> None:
    db = Database(f"sqlite:///{(tmp_path / 'new.db').as_posix()}")
    db.create_all()
    assert db.add_missing_columns() == []
    db.dispose()
