"""Suíte das perguntas oficiais do case.

Etapa atual: valida que cada SQL gabarito (e cada few-shot) passa pelos
guardrails e roda no banco real dentro do timeout, retornando dados. Quando o
workflow do agente existir, esta suíte comparará o resultado do SQL gerado pelo
LLM com o do gabarito (via cache, para não consumir quota a cada execução).
"""

import pytest

from src.agent.guardrails import validate_sql
from src.agent.prompts import FEW_SHOTS
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
