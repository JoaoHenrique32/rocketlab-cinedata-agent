"""Validação read-only do SQL gerado pelo LLM, antes de qualquer acesso ao banco.

Camadas aplicadas por `validate_sql` (a garantia final fica no engine, ver
`src.database.connection`):

1. Normalização: remove `;` finais e rejeita SQL vazio;
2. Regex de palavras proibidas sobre o SQL *sem* strings e comentários, e
   rejeição de múltiplos statements;
3. Parser `sqlglot` (dialeto sqlite): exatamente um statement, do tipo consulta
   (`SELECT`, `WITH ... SELECT`, `UNION`...), sem nós de escrita na árvore;
4. Injeção de `LIMIT` quando a consulta externa não tiver um.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

FORBIDDEN_KEYWORDS: tuple[str, ...] = (
    "DROP",
    "DELETE",
    "UPDATE",
    "INSERT",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "REPLACE",
    "ATTACH",
    "DETACH",
    "PRAGMA",
    "VACUUM",
    "REINDEX",
)

# `REPLACE(x, 'a', 'b')` é função de string legítima; `REPLACE INTO` não.
_FORBIDDEN_RE = re.compile(
    r"\b(?:"
    + "|".join(k for k in FORBIDDEN_KEYWORDS if k != "REPLACE")
    + r")\b|\bREPLACE\b(?!\s*\()",
    re.IGNORECASE,
)
# Strings ('...'), identificadores ("...", `...`, [...]) e comentários.
_LITERALS_AND_COMMENTS_RE = re.compile(
    r"'(?:[^']|'')*'"
    r'|"(?:[^"]|"")*"'
    r"|`[^`]*`"
    r"|\[[^\]]*\]"
    r"|--[^\n]*"
    r"|/\*.*?\*/",
    re.DOTALL,
)
_WRITE_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Pragma,
)


class GuardrailViolation(Exception):
    """SQL rejeitado pelos guardrails; a mensagem é segura para o usuário."""


def _strip_literals_and_comments(sql: str) -> str:
    return _LITERALS_AND_COMMENTS_RE.sub(" ", sql)


def _check_keywords(sql: str) -> None:
    code_only = _strip_literals_and_comments(sql)
    if "/*" in code_only:
        raise GuardrailViolation("Comentário de bloco não fechado no SQL.")
    if ";" in code_only:
        raise GuardrailViolation("Apenas uma instrução SQL por consulta é permitida.")
    match = _FORBIDDEN_RE.search(code_only)
    if match:
        raise GuardrailViolation(
            f"Operação não permitida: '{match.group(0).upper()}'. "
            "O agente executa somente consultas de leitura."
        )


def _parse_single_query(sql: str) -> exp.Query:
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except ParseError as exc:
        raise GuardrailViolation(f"SQL inválido: {exc}") from exc

    if len(statements) != 1:
        raise GuardrailViolation("Apenas uma instrução SQL por consulta é permitida.")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise GuardrailViolation("Apenas consultas SELECT são permitidas.")
    if any(tree.find_all(*_WRITE_NODES)):
        raise GuardrailViolation("A consulta contém operações de escrita.")
    return tree


def validate_sql(sql: str, max_rows: int = 200) -> str:
    """Valida o SQL e retorna a versão a ser executada.

    O texto original é preservado (não é re-renderizado pelo sqlglot) para que o
    SQL exibido na auditoria seja exatamente o executado; só é acrescentado
    `LIMIT` quando a consulta externa não possui um.

    Raises:
        GuardrailViolation: se o SQL não for uma única consulta de leitura.
    """
    cleaned = (sql or "").strip()
    while cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
    if not cleaned:
        raise GuardrailViolation("Nenhuma consulta SQL foi informada.")

    _check_keywords(cleaned)
    tree = _parse_single_query(cleaned)

    if tree.args.get("limit") is None:
        # Quebra de linha evita que um comentário `--` final anule o LIMIT.
        cleaned = f"{cleaned}\nLIMIT {max_rows}"
    return cleaned
