"""Testes da conexão read-only.

Usam um banco SQLite temporário, para rodar sem o cinerocket.db (ex.: CI). O
teste marcado com `real_db` valida o banco real quando ele estiver presente.
"""

import sqlite3
from pathlib import Path

import pytest

from src.config import get_settings
from src.database.connection import (
    DatabaseError,
    QueryTimeoutError,
    ReadOnlyDatabase,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE filmes (id INTEGER PRIMARY KEY, titulo TEXT)")
    conn.executemany(
        "INSERT INTO filmes (titulo) VALUES (?)", [(f"Filme {i}",) for i in range(50)]
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def db(db_path: Path) -> ReadOnlyDatabase:
    with ReadOnlyDatabase(db_path, timeout_s=5, max_rows=10) as database:
        yield database


def test_select_returns_columns_and_rows(db: ReadOnlyDatabase) -> None:
    result = db.execute("SELECT id, titulo FROM filmes ORDER BY id LIMIT 3")
    assert result.columns == ["id", "titulo"]
    assert result.rows == [(1, "Filme 0"), (2, "Filme 1"), (3, "Filme 2")]
    assert not result.truncated


def test_caps_rows_and_flags_truncation(db: ReadOnlyDatabase) -> None:
    result = db.execute("SELECT * FROM filmes")
    assert result.row_count == 10
    assert result.truncated


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM filmes",
        "UPDATE filmes SET titulo = 'x'",
        "INSERT INTO filmes (titulo) VALUES ('x')",
        "DROP TABLE filmes",
        "CREATE TABLE y (a INT)",
        "CREATE TEMP TABLE y (a INT)",
        "PRAGMA query_only = OFF",
        "ATTACH DATABASE ':memory:' AS m",
    ],
)
def test_engine_blocks_writes_even_without_guardrails(
    db: ReadOnlyDatabase, db_path: Path, sql: str
) -> None:
    with pytest.raises(DatabaseError):
        db.execute(sql)
    check = sqlite3.connect(db_path)
    assert check.execute("SELECT COUNT(*) FROM filmes").fetchone()[0] == 50
    check.close()


def test_blocks_load_extension(db: ReadOnlyDatabase) -> None:
    with pytest.raises(DatabaseError):
        db.execute("SELECT load_extension('x')")


def test_timeout_interrupts_long_query(db_path: Path) -> None:
    slow = (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) "
        "SELECT COUNT(*) FROM n"
    )
    with ReadOnlyDatabase(db_path, timeout_s=0.2) as database:
        with pytest.raises(QueryTimeoutError):
            database.execute(slow)


def test_missing_database_raises(tmp_path: Path) -> None:
    with pytest.raises(DatabaseError, match="não encontrado"):
        ReadOnlyDatabase(tmp_path / "nao_existe.db").execute("SELECT 1")


@pytest.mark.real_db
@pytest.mark.skipif(
    not get_settings().resolved_db_path().is_file(),
    reason="data/cinerocket.db ausente",
)
def test_real_database_is_reachable_and_read_only() -> None:
    with ReadOnlyDatabase() as database:
        result = database.execute("SELECT COUNT(*) FROM dim_genres")
        assert result.rows == [(19,)]
        with pytest.raises(DatabaseError):
            database.execute("DELETE FROM dim_genres")
