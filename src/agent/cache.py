"""Cache local de respostas e contador de quota diária (SQLite em `.cache/`).

- Respostas: chave = sha256(pergunta normalizada + PROMPT_VERSION). Guarda o SQL,
  as premissas e o resumo separadamente: num hit, o SQL é reexecutado localmente
  (0 req) e o resumo só é gerado se ainda não existir.
- Quota: conta as requisições feitas ao OpenRouter por dia (UTC), para avisar
  antes de estourar o limite de 50 req/dia do tier :free.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.agent.prompts import PROMPT_VERSION

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    key        TEXT PRIMARY KEY,
    question   TEXT NOT NULL,
    sql        TEXT NOT NULL,
    premissas  TEXT NOT NULL,
    summary    TEXT,
    model      TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quota (
    day   TEXT PRIMARY KEY,
    count INTEGER NOT NULL
);
"""


def normalize_question(question: str) -> str:
    """Normaliza a pergunta para que variações triviais caiam na mesma chave."""
    text = unicodedata.normalize("NFKC", question).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip("?!. ")


def cache_key(question: str) -> str:
    payload = f"{PROMPT_VERSION}\n{normalize_question(question)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class CachedAnswer:
    sql: str
    premissas: list[str]
    summary: str | None
    model: str | None


class AgentCache:
    """Persistência local de respostas e uso de quota."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)

    # ---- respostas -------------------------------------------------------
    def get(self, question: str) -> CachedAnswer | None:
        row = self._conn.execute(
            "SELECT sql, premissas, summary, model FROM responses WHERE key = ?",
            (cache_key(question),),
        ).fetchone()
        if row is None:
            return None
        return CachedAnswer(
            sql=row[0], premissas=json.loads(row[1]), summary=row[2], model=row[3]
        )

    def put(
        self,
        question: str,
        sql: str,
        premissas: list[str],
        model: str | None,
        summary: str | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO responses "
                "(key, question, sql, premissas, summary, model, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    cache_key(question),
                    question,
                    sql,
                    json.dumps(premissas, ensure_ascii=False),
                    summary,
                    model,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def set_summary(self, question: str, summary: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE responses SET summary = ? WHERE key = ?",
                (summary, cache_key(question)),
            )

    def clear(self) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM responses")

    # ---- quota -----------------------------------------------------------
    def requests_today(self) -> int:
        row = self._conn.execute(
            "SELECT count FROM quota WHERE day = ?", (_today(),)
        ).fetchone()
        return row[0] if row else 0

    def register_request(self) -> int:
        """Incrementa o contador do dia e retorna o novo total."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO quota (day, count) VALUES (?, 1) "
                "ON CONFLICT(day) DO UPDATE SET count = count + 1",
                (_today(),),
            )
        return self.requests_today()

    def close(self) -> None:
        self._conn.close()
