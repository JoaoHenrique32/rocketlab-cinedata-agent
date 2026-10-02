"""Suíte das perguntas oficiais do case.

1. Sempre: cada SQL gabarito (e cada few-shot) passa pelos guardrails e roda no
   banco real dentro do timeout, retornando dados.
2. Opt-in (`RUN_LLM_EVAL=1`): o agente responde cada pergunta e o resultado é
   comparado ao do gabarito. Custa 14 req na 1ª execução (sem resumo); as
   seguintes saem do cache (0 req).
"""

import os
from typing import Any, Sequence

import pytest

from src.agent.guardrails import validate_sql
from src.agent.prompts import FEW_SHOTS
from src.agent.workflow import CineDataAgent
from src.config import get_settings
from src.database.connection import ReadOnlyDatabase
from tests.golden_queries import GOLDEN_CASES

pytestmark = [
    pytest.mark.real_db,
    pytest.mark.skipif(
        not get_settings().resolved_db_path().is_file(),
        reason="data/cinerocket.db ausente",
    ),
]


@pytest.fixture(scope="module")
def db() -> ReadOnlyDatabase:
    with ReadOnlyDatabase() as database:
        yield database


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda c: c.id)
def test_golden_sql_runs_and_returns_data(db: ReadOnlyDatabase, case) -> None:
    result = db.execute(validate_sql(case.sql))
    assert result.row_count > 0
    assert not result.truncated


@pytest.mark.parametrize("shot", FEW_SHOTS, ids=lambda s: s.question[:40])
def test_few_shot_sql_runs_and_returns_data(db: ReadOnlyDatabase, shot) -> None:
    assert db.execute(validate_sql(shot.sql)).row_count > 0


def test_golden_ids_are_unique() -> None:
    ids = [c.id for c in GOLDEN_CASES]
    assert len(ids) == len(set(ids)) == 14


# ---- Avaliação do agente (consome quota do OpenRouter) ----------------------
def _normalize(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 1)
    return value


def key_values_match(
    gold_rows: Sequence[Sequence[Any]],
    key_column: int,
    agent_columns: int,
    agent_rows: Sequence[Sequence[Any]],
) -> bool:
    """O agente acerta se alguma coluna sua contém exatamente os mesmos valores
    da coluna-chave do gabarito (como conjunto: empates podem mudar a ordem)."""
    expected = {_normalize(row[key_column]) for row in gold_rows}
    return any(
        {_normalize(row[j]) for row in agent_rows} == expected
        for j in range(agent_columns)
    )


@pytest.fixture(scope="module")
def agent() -> CineDataAgent:
    instance = CineDataAgent.from_settings()
    yield instance
    instance.close()


@pytest.mark.llm
@pytest.mark.skipif(
    os.getenv("RUN_LLM_EVAL") != "1",
    reason="avaliação com LLM desligada (defina RUN_LLM_EVAL=1; custa ~14 req)",
)
@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda c: c.id)
def test_agent_matches_golden(agent: CineDataAgent, db: ReadOnlyDatabase, case) -> None:
    gold = db.execute(validate_sql(case.sql))
    response = agent.ask(case.question, summarize=False)
    assert response.ok, response.error
    assert key_values_match(
        gold.rows, case.key_column, len(response.columns), response.rows
    ), f"SQL do agente:\n{response.sql}"


def test_key_values_match_ignores_order_and_column_position() -> None:
    gold = [("A", 1.0), ("B", 2.0)]
    assert key_values_match(gold, 0, 2, [(2.04, "B"), (1.0, "A")])
    assert not key_values_match(gold, 0, 2, [(1.0, "A"), (3.0, "C")])
