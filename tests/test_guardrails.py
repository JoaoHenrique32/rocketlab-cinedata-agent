"""Testes offline dos guardrails textuais (nenhum acesso a banco ou LLM)."""

import pytest

from src.agent.guardrails import GuardrailViolation, validate_sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT titulo FROM dim_movies",
        "select titulo from dim_movies;",
        "WITH t AS (SELECT 1 AS x) SELECT x FROM t",
        "SELECT 1 UNION ALL SELECT 2",
        "SELECT REPLACE(titulo, 'a', 'b') FROM dim_movies",
        "SELECT replace (titulo, 'a', 'b') FROM dim_movies",
        "SELECT created_at, updated FROM movie_reviews",
        "SELECT 'DROP TABLE x; DELETE' AS texto",
        'SELECT "insert" FROM t',
        "SELECT 1 -- comentário com DELETE",
        "SELECT /* UPDATE */ 1",
        "SELECT strftime('%Y', 'now')",
    ],
)
def test_allows_read_only_queries(sql: str) -> None:
    assert validate_sql(sql).startswith(sql.rstrip(";").strip())


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE dim_movies",
        "DELETE FROM dim_movies",
        "UPDATE dim_movies SET titulo = 'x'",
        "INSERT INTO dim_genres VALUES ('1', 'x')",
        "INSERT OR REPLACE INTO dim_genres VALUES ('1', 'x')",
        "REPLACE INTO dim_genres VALUES ('1', 'x')",
        "ALTER TABLE dim_movies ADD COLUMN y INT",
        "CREATE TABLE x (a INT)",
        "CREATE TEMP VIEW v AS SELECT 1",
        "ATTACH DATABASE 'x.db' AS x",
        "PRAGMA writable_schema = ON",
        "VACUUM",
        "SELECT 1; DROP TABLE dim_movies",
        "SELECT 1; SELECT 2",
        "select 1;\n delete from dim_movies",
        "WITH x AS (SELECT 1) DELETE FROM dim_movies",
        "SELECT 1 /* comentário não fechado",
    ],
)
def test_blocks_writes_and_multi_statements(sql: str) -> None:
    with pytest.raises(GuardrailViolation):
        validate_sql(sql)


@pytest.mark.parametrize("sql", ["", "   ", ";", None])
def test_rejects_empty(sql: str) -> None:
    with pytest.raises(GuardrailViolation):
        validate_sql(sql)


def test_rejects_invalid_sql() -> None:
    with pytest.raises(GuardrailViolation, match="inválido"):
        validate_sql("SELECT FROM WHERE (")


def test_rejects_non_query_statement() -> None:
    with pytest.raises(GuardrailViolation):
        validate_sql("VALUES (1)")


def test_injects_limit_when_missing() -> None:
    assert validate_sql("SELECT titulo FROM dim_movies", max_rows=50).endswith(
        "\nLIMIT 50"
    )


def test_limit_survives_trailing_line_comment() -> None:
    result = validate_sql("SELECT 1 -- fim", max_rows=10)
    assert result.splitlines()[-1] == "LIMIT 10"


def test_keeps_existing_limit() -> None:
    sql = "SELECT titulo FROM dim_movies ORDER BY titulo LIMIT 10"
    assert validate_sql(sql) == sql


def test_limit_in_subquery_does_not_count_as_outer_limit() -> None:
    sql = "SELECT * FROM (SELECT titulo FROM dim_movies LIMIT 5)"
    assert validate_sql(sql, max_rows=7).endswith("LIMIT 7")
