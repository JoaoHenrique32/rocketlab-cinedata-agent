"""Orquestração do agente CineData: pergunta -> SQL -> dados -> resumo.

Orçamento de chamadas ao LLM por pergunta (sem contar fallback entre modelos):
- cache hit com resumo: 0 req (o SQL é reexecutado localmente);
- geração de SQL: 1 req (+1 retry se o SQL falhar nos guardrails ou no banco);
- resumo executivo: 1 req (opcional; `summarize=False` usa template local).
"""

from __future__ import annotations

from src.agent.cache import AgentCache, CachedAnswer
from src.agent.formatter import AgentResponse, local_summary
from src.agent.guardrails import GuardrailViolation, validate_sql
from src.agent.llm import (
    LLMClient,
    LLMError,
    openrouter_completion_fn,
    parse_json_object,
    strip_reasoning,
)
from src.agent.prompts import build_sql_messages, build_summary_messages
from src.config import Settings, get_settings
from src.database.connection import DatabaseError, QueryResult, ReadOnlyDatabase

MAX_SQL_RETRIES = 1


class _SqlGenerationFailed(Exception):
    def __init__(self, message: str, sql: str | None = None) -> None:
        super().__init__(message)
        self.sql = sql


class CineDataAgent:
    """Agente Text-to-SQL somente-leitura sobre a camada Gold."""

    def __init__(
        self,
        db: ReadOnlyDatabase,
        llm: LLMClient,
        cache: AgentCache | None = None,
        max_rows: int = 200,
    ) -> None:
        self.db = db
        self.llm = llm
        self.cache = cache
        self.max_rows = max_rows

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> CineDataAgent:
        """Monta o agente com as dependências reais a partir do `.env`."""
        settings = settings or get_settings()
        cache = AgentCache(settings.resolved_cache_path())
        llm = LLMClient(
            completion_fn=openrouter_completion_fn(
                api_key=settings.openrouter_api_key,
                base_url=settings.openrouter_base_url,
                timeout_s=settings.llm_timeout_s,
                temperature=settings.llm_temperature,
            ),
            models=settings.llm_models,
            quota=cache,
            daily_limit=settings.daily_request_limit,
        )
        db = ReadOnlyDatabase(
            settings.resolved_db_path(),
            timeout_s=settings.query_timeout_s,
            max_rows=settings.max_rows,
        )
        return cls(db=db, llm=llm, cache=cache, max_rows=settings.max_rows)

    # ------------------------------------------------------------------
    def ask(
        self, question: str, summarize: bool = True, use_cache: bool = True
    ) -> AgentResponse:
        """Responde uma pergunta em linguagem natural."""
        question = (question or "").strip()
        response = AgentResponse(question=question)
        if not question:
            response.error = "Informe uma pergunta."
            return response

        cached = self.cache.get(question) if (use_cache and self.cache) else None
        try:
            if cached is not None:
                response.from_cache = True
                response.model = cached.model
                response.sql = cached.sql
                response.premissas = cached.premissas
                result = self.db.execute(cached.sql)
            else:
                result = self._generate_and_execute(question, response)
                if self.cache:
                    self.cache.put(
                        question, response.sql or "", response.premissas, response.model
                    )
        except (_SqlGenerationFailed, LLMError, DatabaseError) as exc:
            response.error = str(exc)
            if isinstance(exc, _SqlGenerationFailed) and exc.sql:
                response.sql = exc.sql
            return response

        response.columns = result.columns
        response.rows = result.rows
        response.truncated = result.truncated
        response.summary = self._summarize(question, response, cached, summarize)
        return response

    # ------------------------------------------------------------------
    def _generate_and_execute(
        self, question: str, response: AgentResponse
    ) -> QueryResult:
        previous_sql: str | None = None
        error: str | None = None
        for _ in range(MAX_SQL_RETRIES + 1):
            llm_response = self.llm.complete(
                build_sql_messages(question, previous_sql, error)
            )
            response.llm_calls += 1
            response.model = llm_response.model
            try:
                payload = parse_json_object(llm_response.content)
            except LLMError as exc:
                previous_sql, error = llm_response.content[:2000], str(exc)
                continue

            raw_sql = payload.get("sql")
            if not raw_sql:
                refusal = payload.get("erro") or "O modelo não gerou uma consulta SQL."
                raise _SqlGenerationFailed(str(refusal))
            premissas = payload.get("premissas") or []
            response.premissas = [str(p) for p in premissas]

            try:
                sql = validate_sql(str(raw_sql), max_rows=self.max_rows)
                result = self.db.execute(sql)
            except (GuardrailViolation, DatabaseError) as exc:
                previous_sql, error = str(raw_sql), str(exc)
                continue
            response.sql = sql
            return result

        raise _SqlGenerationFailed(
            f"Não foi possível gerar uma consulta válida: {error}", sql=previous_sql
        )

    def _summarize(
        self,
        question: str,
        response: AgentResponse,
        cached: CachedAnswer | None,
        summarize: bool,
    ) -> str:
        if cached is not None and cached.summary:
            return cached.summary
        if not summarize:
            return local_summary(response.columns, response.rows)
        try:
            llm_response = self.llm.complete(
                build_summary_messages(
                    question,
                    response.premissas,
                    response.columns,
                    response.rows,
                    response.truncated,
                )
            )
        except LLMError:
            # Degrada para o resumo local: os dados já foram obtidos com sucesso.
            return local_summary(response.columns, response.rows)
        response.llm_calls += 1
        summary = strip_reasoning(llm_response.content)
        if self.cache:
            self.cache.set_summary(question, summary)
        return summary

    def close(self) -> None:
        self.db.close()
        if self.cache:
            self.cache.close()
