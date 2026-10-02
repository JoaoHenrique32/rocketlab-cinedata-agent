"""Testes offline do workflow do agente (LLM roteirizado + banco temporário)."""

import json
import sqlite3
from pathlib import Path

import pytest

from src.agent.cache import AgentCache
from src.agent.formatter import to_markdown
from src.agent.llm import LLMClient
from src.agent.workflow import CineDataAgent
from src.database.connection import ReadOnlyDatabase
from tests.fakes import MemoryQuota, ScriptedLLM, timeout_error

QUESTION = "Quais são os filmes?"


def sql_answer(sql: str, premissas: list[str] | None = None) -> str:
    return json.dumps({"sql": sql, "premissas": premissas or []})


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "gold.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE filmes (titulo TEXT, receita REAL)")
    conn.executemany(
        "INSERT INTO filmes VALUES (?, ?)", [("A", 300.0), ("B", 200.0), ("C", 100.0)]
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def make_agent(db_path: Path, tmp_path: Path):
    created: list[CineDataAgent] = []

    def factory(llm: ScriptedLLM, quota: MemoryQuota | None = None) -> CineDataAgent:
        cache = AgentCache(tmp_path / "cache.db")
        client = LLMClient(llm, ["m1:free", "m2:free"], quota or MemoryQuota(), 50)
        agent = CineDataAgent(ReadOnlyDatabase(db_path, timeout_s=5), client, cache)
        created.append(agent)
        return agent

    yield factory
    for agent in created:
        agent.close()


def test_happy_path_uses_two_calls(make_agent) -> None:
    llm = ScriptedLLM(
        sql_answer("SELECT titulo, receita FROM filmes ORDER BY receita DESC", ["p1"]),
        "O filme A lidera.",
    )
    response = make_agent(llm).ask(QUESTION)
    assert response.ok
    assert response.rows[0] == ("A", 300.0)
    assert response.premissas == ["p1"]
    assert response.summary == "O filme A lidera."
    assert response.sql.endswith("LIMIT 200")
    assert response.llm_calls == 2
    assert not response.from_cache


def test_second_ask_hits_cache_with_zero_calls(make_agent) -> None:
    llm = ScriptedLLM(sql_answer("SELECT titulo FROM filmes"), "Resumo.")
    agent = make_agent(llm)
    agent.ask(QUESTION)
    again = agent.ask("quais são os filmes")
    assert again.from_cache and again.llm_calls == 0
    assert again.summary == "Resumo."
    assert len(again.rows) == 3


def test_retry_after_invalid_sql_sends_error_feedback(make_agent) -> None:
    llm = ScriptedLLM(
        sql_answer("SELECT coluna_inexistente FROM filmes"),
        sql_answer("SELECT titulo FROM filmes"),
        "Resumo.",
    )
    response = make_agent(llm).ask(QUESTION)
    assert response.ok and response.llm_calls == 3
    retry_messages = llm.calls[1][1]
    assert "no such column" in retry_messages[-1]["content"]


def test_write_sql_from_llm_is_blocked_and_never_executed(
    make_agent, db_path: Path
) -> None:
    llm = ScriptedLLM(sql_answer("DELETE FROM filmes"), sql_answer("DROP TABLE filmes"))
    response = make_agent(llm).ask(QUESTION)
    assert not response.ok
    assert "não permitida" in response.error
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM filmes").fetchone()[0] == 3
    conn.close()


def test_model_refusal_is_reported_without_execution(make_agent) -> None:
    llm = ScriptedLLM(json.dumps({"sql": None, "erro": "Pedido de alteração."}))
    response = make_agent(llm).ask("Apague os filmes")
    assert response.error == "Pedido de alteração."
    assert response.llm_calls == 1


def test_failed_answers_are_not_cached(make_agent) -> None:
    llm = ScriptedLLM(sql_answer("SELECT x FROM y"), sql_answer("SELECT x FROM y"))
    agent = make_agent(llm)
    assert not agent.ask(QUESTION).ok
    assert agent.cache.get(QUESTION) is None


def test_no_summary_mode_uses_one_call(make_agent) -> None:
    llm = ScriptedLLM(sql_answer("SELECT titulo FROM filmes"))
    response = make_agent(llm).ask(QUESTION, summarize=False)
    assert response.llm_calls == 1
    assert "sem LLM" in response.summary


def test_summary_failure_degrades_to_local_summary(make_agent) -> None:
    llm = ScriptedLLM(
        sql_answer("SELECT titulo FROM filmes"), timeout_error(), timeout_error()
    )
    response = make_agent(llm).ask(QUESTION)
    assert response.ok
    assert "sem LLM" in response.summary


def test_markdown_output_has_summary_table_and_sql(make_agent) -> None:
    llm = ScriptedLLM(
        sql_answer("SELECT titulo, receita FROM filmes", ["Receita em USD."]),
        "Resumo executivo.",
    )
    md = to_markdown(make_agent(llm).ask(QUESTION))
    for section in ("## Resumo executivo", "### Premissas", "## Dados", "```sql"):
        assert section in md
    assert "| titulo | receita |" in md
    assert "300,00" in md


def test_empty_question(make_agent) -> None:
    llm = ScriptedLLM()
    assert make_agent(llm).ask("   ").error == "Informe uma pergunta."
    assert llm.calls == []
