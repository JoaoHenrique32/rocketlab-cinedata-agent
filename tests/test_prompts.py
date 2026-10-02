"""Testes dos prompts: schema fiel ao banco, few-shots e contrato de mensagens."""

import json
import sqlite3

import pytest

from src.agent.guardrails import validate_sql
from src.agent.prompts import (
    FEW_SHOTS,
    SCHEMA,
    SQL_SYSTEM_PROMPT,
    SUMMARY_MAX_ROWS,
    build_sql_messages,
    build_summary_messages,
)
from src.config import get_settings
from tests.golden_queries import GOLDEN_CASES

REAL_DB = get_settings().resolved_db_path()
requires_real_db = pytest.mark.skipif(
    not REAL_DB.is_file(), reason="data/cinerocket.db ausente"
)


@pytest.mark.real_db
@requires_real_db
def test_schema_mirrors_real_database() -> None:
    conn = sqlite3.connect(f"{REAL_DB.resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT IN ('alembic_version') AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == set(SCHEMA)
        for table, columns in SCHEMA.items():
            real = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            assert [c.name for c in columns] == real, table
    finally:
        conn.close()


@pytest.mark.parametrize("shot", FEW_SHOTS, ids=lambda s: s.question[:40])
def test_few_shots_pass_guardrails(shot) -> None:
    validate_sql(shot.sql)


def test_few_shots_do_not_leak_golden_questions() -> None:
    golden = {c.question.lower() for c in GOLDEN_CASES}
    golden_sql = {" ".join(c.sql.split()).lower() for c in GOLDEN_CASES}
    for shot in FEW_SHOTS:
        assert shot.question.lower() not in golden
        assert " ".join(shot.sql.split()).lower() not in golden_sql


def test_system_prompt_is_compact() -> None:
    # ~4 chars/token: mantém o prompt abaixo de ~3k tokens para modelos :free.
    assert len(SQL_SYSTEM_PROMPT) < 12_000


def test_build_sql_messages_without_retry() -> None:
    messages = build_sql_messages("Quantos filmes existem?")
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "Quantos filmes existem?" in messages[1]["content"]


def test_build_sql_messages_with_retry_feedback() -> None:
    messages = build_sql_messages("P?", previous_sql="SELECT x", error="no such column")
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert json.loads(messages[2]["content"]) == {"sql": "SELECT x"}
    assert "no such column" in messages[3]["content"]


def test_build_summary_messages_caps_rows_and_flags_truncation() -> None:
    rows = [(i, f"t{i}") for i in range(SUMMARY_MAX_ROWS + 20)]
    messages = build_summary_messages(
        "P?", ["premissa A"], ["id", "titulo"], rows, False
    )
    content = messages[1]["content"]
    assert "premissa A" in content
    assert "truncado" in content
    assert f"t{SUMMARY_MAX_ROWS - 1}" in content
    assert f"t{SUMMARY_MAX_ROWS}" not in content
