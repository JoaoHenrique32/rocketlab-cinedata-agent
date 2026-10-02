"""Acesso somente-leitura ao cinerocket.db.

Esta é a camada de garantia real do read-only: mesmo que uma query de escrita
escape dos guardrails textuais (`src.agent.guardrails`), o engine do SQLite a
recusa por três mecanismos independentes:

1. Arquivo aberto com `mode=ro` (URI);
2. `PRAGMA query_only = ON`;
3. `set_authorizer` permitindo apenas ações de leitura.

Além disso, cada query tem timeout (via `set_progress_handler`) e teto de linhas.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import get_settings

# Ações que uma consulta analítica legítima precisa executar.
_ALLOWED_ACTIONS: frozenset[int] = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
)
_BLOCKED_FUNCTIONS: frozenset[str] = frozenset({"load_extension"})
# Nº de instruções da VM do SQLite entre checagens de timeout.
_PROGRESS_STEPS = 10_000
# Tuning de leitura para o arquivo de ~580 MB (self-join em bridge_movie_person:
# 105s -> 16s no benchmark de 2026-10-02). Rodam antes do authorizer.
_PERFORMANCE_PRAGMAS: tuple[str, ...] = (
    "PRAGMA mmap_size = 1073741824",
    "PRAGMA cache_size = -262144",
    "PRAGMA temp_store = MEMORY",
)


class DatabaseError(Exception):
    """Erro de acesso ao banco com mensagem segura para o usuário final."""


class QueryTimeoutError(DatabaseError):
    """A consulta excedeu o tempo máximo permitido."""


@dataclass(frozen=True)
class QueryResult:
    """Resultado tabular de uma consulta."""

    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool
    elapsed_ms: float

    @property
    def row_count(self) -> int:
        return len(self.rows)


def _authorizer(
    action: int,
    arg1: str | None,
    arg2: str | None,
    db_name: str | None,
    trigger: str | None,
) -> int:
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    # Para SQLITE_FUNCTION, o nome da função vem em arg2.
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in (
        _BLOCKED_FUNCTIONS
    ):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class ReadOnlyDatabase:
    """Conexão SQLite blindada para leitura, com timeout e teto de linhas."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        timeout_s: float | None = None,
        max_rows: int | None = None,
    ) -> None:
        settings = get_settings()
        self.db_path = (
            Path(db_path) if db_path is not None else settings.resolved_db_path()
        )
        self.timeout_s = (
            timeout_s if timeout_s is not None else settings.query_timeout_s
        )
        self.max_rows = max_rows if max_rows is not None else settings.max_rows
        self._conn: sqlite3.Connection | None = None
        self._deadline: float = 0.0

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise DatabaseError(f"Banco de dados não encontrado em '{self.db_path}'.")
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        # A ordem importa: PRAGMAs precisam rodar antes do authorizer, que os negaria.
        conn.execute("PRAGMA query_only = ON")
        for pragma in _PERFORMANCE_PRAGMAS:
            conn.execute(pragma)
        conn.set_authorizer(_authorizer)
        conn.set_progress_handler(self._check_deadline, _PROGRESS_STEPS)
        return conn

    def _check_deadline(self) -> int:
        # Retorno != 0 interrompe a query com OperationalError("interrupted").
        return int(time.monotonic() > self._deadline)

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def execute(self, sql: str) -> QueryResult:
        """Executa uma consulta de leitura e retorna até `max_rows` linhas."""
        start = time.monotonic()
        self._deadline = start + self.timeout_s
        try:
            cursor = self.connection.execute(sql)
            rows = cursor.fetchmany(self.max_rows + 1)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise QueryTimeoutError(
                    f"A consulta excedeu o limite de {self.timeout_s:.0f}s."
                ) from exc
            raise DatabaseError(f"Erro ao executar a consulta: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise DatabaseError(f"Erro ao executar a consulta: {exc}") from exc

        columns = [col[0] for col in cursor.description or []]
        truncated = len(rows) > self.max_rows
        return QueryResult(
            columns=columns,
            rows=rows[: self.max_rows],
            truncated=truncated,
            elapsed_ms=(time.monotonic() - start) * 1000,
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> ReadOnlyDatabase:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
