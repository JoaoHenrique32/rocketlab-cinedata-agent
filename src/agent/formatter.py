"""Formatação da resposta: resumo executivo, tabela Markdown e SQL auditado."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

DISPLAY_MAX_ROWS = 20
# Colunas numéricas que são rótulos, não quantidades (sem separador de milhar).
_RAW_NUMBER_PREFIXES = ("ano", "id")


@dataclass
class AgentResponse:
    """Resultado completo de uma pergunta ao agente."""

    question: str
    summary: str = ""
    sql: str | None = None
    premissas: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    truncated: bool = False
    model: str | None = None
    from_cache: bool = False
    llm_calls: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def format_value(value: Any, column: str = "") -> str:
    """Formata números no padrão pt-BR (1.234.567,89); anos/ids ficam crus."""
    if value is None:
        return "—"
    if isinstance(value, bool) or column.lower().startswith(_RAW_NUMBER_PREFIXES):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}".replace(",", ".")
    if isinstance(value, float):
        text = f"{value:,.2f}"
        return text.replace(",", "_").replace(".", ",").replace("_", ".")
    return str(value).replace("|", "\\|").replace("\n", " ")


def to_markdown_table(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    max_rows: int = DISPLAY_MAX_ROWS,
) -> str:
    if not columns:
        return "_Sem colunas no resultado._"
    if not rows:
        return "_A consulta não retornou linhas._"
    header = "| " + " | ".join(columns) + " |"
    sep = "|" + "|".join(" --- " for _ in columns) + "|"
    body = [
        "| " + " | ".join(format_value(v, c) for c, v in zip(columns, row)) + " |"
        for row in rows[:max_rows]
    ]
    return "\n".join([header, sep, *body])


def local_summary(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """Resumo por template, sem LLM (modo econômico ou fallback)."""
    if not rows:
        return "A consulta não retornou resultados para os critérios aplicados."
    first = ", ".join(
        f"{col}: {format_value(val, col)}" for col, val in zip(columns, rows[0])
    )
    return (
        f"A consulta retornou {len(rows)} linha(s). Primeiro registro: {first}. "
        "(Resumo automático, gerado sem LLM.)"
    )


def to_markdown(response: AgentResponse) -> str:
    """Renderiza a resposta no formato padrão: resumo, dados e SQL auditado."""
    parts = [f"## Pergunta\n{response.question}"]
    if not response.ok:
        parts.append(f"## Não foi possível responder\n{response.error}")
    else:
        parts.append(f"## Resumo executivo\n{response.summary}")
        if response.premissas:
            parts.append(
                "### Premissas\n" + "\n".join(f"- {p}" for p in response.premissas)
            )
        table = to_markdown_table(response.columns, response.rows)
        shown = min(len(response.rows), DISPLAY_MAX_ROWS)
        if response.truncated or len(response.rows) > shown:
            table += f"\n\n_Exibindo {shown} linhas; resultado completo truncado._"
        parts.append(f"## Dados\n{table}")
    if response.sql:
        parts.append(f"## SQL executado (auditoria)\n```sql\n{response.sql}\n```")
    origin = "cache" if response.from_cache else (response.model or "—")
    parts.append(f"_Origem: {origin} · chamadas ao LLM: {response.llm_calls}_")
    return "\n\n".join(parts)
