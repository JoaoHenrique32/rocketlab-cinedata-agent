"""Testes da formatação da resposta."""

import pytest

from src.agent.formatter import (
    AgentResponse,
    format_value,
    local_summary,
    to_markdown,
    to_markdown_table,
)


@pytest.mark.parametrize(
    ("value", "column", "expected"),
    [
        (None, "x", "—"),
        (1234567, "qtd_filmes", "1.234.567"),
        (12390136500.54, "receita_brl", "12.390.136.500,54"),
        (6.3456, "nota", "6,35"),
        (11094720000.0, "receita", "11.094.720.000,00"),
        (2022, "ano_lancamento", "2022"),
        ("a|b", "titulo", "a\\|b"),
    ],
)
def test_format_value(value, column, expected) -> None:
    assert format_value(value, column) == expected


def test_table_limits_rows() -> None:
    table = to_markdown_table(["n"], [(i,) for i in range(30)], max_rows=5)
    assert len(table.splitlines()) == 2 + 5


def test_empty_results() -> None:
    assert "não retornou" in to_markdown_table(["n"], [])
    assert "não retornou" in local_summary(["n"], [])


def test_error_response_shows_failed_sql_for_audit() -> None:
    md = to_markdown(
        AgentResponse(question="P?", sql="SELECT x", error="Erro de coluna.")
    )
    assert "Não foi possível responder" in md
    assert "```sql\nSELECT x\n```" in md
    assert "Resumo executivo" not in md
