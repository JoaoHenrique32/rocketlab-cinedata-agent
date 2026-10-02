"""Cliente LLM (OpenRouter) com fallback entre modelos `:free` e controle de quota.

Política de fallback por tentativa:
- 429, timeout, erro de conexão, 5xx, 404 (modelo removido), 400 ou resposta
  vazia -> tenta o próximo modelo da lista;
- 401/402/403 (chave inválida, sem crédito, sem permissão) -> aborta: os demais
  modelos falhariam igual e só gastariam quota;
- 429 de limite DIÁRIO do tier free -> aborta com `QuotaExceededError`.

Toda tentativa é registrada no contador de quota *antes* do envio (contagem
conservadora: uma requisição que falha no meio do caminho também conta).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

import openai

Messages = list[dict[str, str]]
CompletionFn = Callable[[str, Messages], str]

logger = logging.getLogger(__name__)

_FATAL_ERRORS = (openai.AuthenticationError, openai.PermissionDeniedError)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_LENIENT_DECODER = json.JSONDecoder(strict=False)
_SQL_FIELD_RE = re.compile(r'"sql"\s*:\s*"((?:[^"\\]|\\.)*)"', re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class LLMError(Exception):
    """Falha ao obter resposta do LLM; mensagem segura para o usuário."""


class QuotaExceededError(LLMError):
    """Limite diário de requisições atingido (local ou no OpenRouter)."""


class LLMResponseError(LLMError):
    """O modelo respondeu, mas fora do formato esperado."""


class QuotaTracker(Protocol):
    def requests_today(self) -> int: ...

    def register_request(self) -> int: ...


@dataclass(frozen=True)
class LLMResponse:
    content: str
    model: str
    attempts: int
    # Falhas dos modelos anteriores, quando houve fallback ("modelo: motivo").
    fallbacks: tuple[str, ...] = ()


def openrouter_completion_fn(
    api_key: str, base_url: str, timeout_s: float, temperature: float
) -> CompletionFn:
    """Cria a função de completion real (SDK openai apontando para o OpenRouter)."""
    if not api_key:
        raise LLMError(
            "OPENROUTER_API_KEY não configurada. Copie .env.example para .env "
            "e informe sua chave."
        )
    client = openai.OpenAI(
        api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=0
    )

    def complete(model: str, messages: Messages) -> str:
        response = client.chat.completions.create(
            model=model, messages=messages, temperature=temperature
        )
        if not response.choices:
            return ""
        return response.choices[0].message.content or ""

    return complete


def _is_daily_limit(exc: openai.RateLimitError) -> bool:
    text = str(exc).lower()
    return "per-day" in text or "per day" in text


class LLMClient:
    """Executa chamadas com fallback ordenado entre modelos."""

    def __init__(
        self,
        completion_fn: CompletionFn,
        models: Sequence[str],
        quota: QuotaTracker,
        daily_limit: int,
    ) -> None:
        if not models:
            raise ValueError("Informe ao menos um modelo.")
        self._complete = completion_fn
        self.models = list(models)
        self._quota = quota
        self.daily_limit = daily_limit

    def complete(self, messages: Messages) -> LLMResponse:
        failures: list[str] = []
        for attempt, model in enumerate(self.models, start=1):
            if self._quota.requests_today() >= self.daily_limit:
                raise QuotaExceededError(
                    f"Limite local de {self.daily_limit} requisições/dia atingido. "
                    "Respostas em cache continuam disponíveis."
                )
            self._quota.register_request()
            try:
                content = self._complete(model, messages)
            except _FATAL_ERRORS as exc:
                raise LLMError(f"Falha de autenticação no OpenRouter: {exc}") from exc
            except openai.RateLimitError as exc:
                if _is_daily_limit(exc):
                    raise QuotaExceededError(
                        "Limite diário do OpenRouter atingido para modelos :free."
                    ) from exc
                failures.append(f"{model}: rate limit (429)")
                logger.warning("Fallback de modelo: %s", failures[-1])
                continue
            except openai.APIStatusError as exc:
                if exc.status_code == 402:
                    raise LLMError("Conta OpenRouter sem crédito (HTTP 402).") from exc
                failures.append(f"{model}: HTTP {exc.status_code}")
                logger.warning("Fallback de modelo: %s", failures[-1])
                continue
            except (openai.APITimeoutError, openai.APIConnectionError) as exc:
                failures.append(f"{model}: {type(exc).__name__}")
                logger.warning("Fallback de modelo: %s", failures[-1])
                continue

            if content.strip():
                return LLMResponse(
                    content=content,
                    model=model,
                    attempts=attempt,
                    fallbacks=tuple(failures),
                )
            failures.append(f"{model}: resposta vazia")
            logger.warning("Fallback de modelo: %s", failures[-1])

        raise LLMError(
            "Nenhum modelo disponível no momento (" + "; ".join(failures) + ")."
        )


def strip_reasoning(text: str) -> str:
    """Remove blocos `<think>...</think>` emitidos por modelos de raciocínio."""
    return _THINK_RE.sub("", text).strip()


def parse_json_object(text: str) -> dict[str, Any]:
    """Extrai o primeiro objeto JSON da resposta do modelo.

    Tolera blocos `<think>`, cercas de markdown e texto antes ou depois do JSON,
    comuns em modelos gratuitos sem suporte a `response_format`.
    """
    cleaned = strip_reasoning(text)
    fence = _FENCE_RE.search(cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    start = cleaned.find("{")
    if start == -1:
        raise LLMResponseError("O modelo não retornou JSON.")
    candidate = cleaned[start:]
    last_error: json.JSONDecodeError | None = None
    for attempt in _repair_candidates(candidate):
        try:
            parsed, _ = _LENIENT_DECODER.raw_decode(attempt)
            break
        except json.JSONDecodeError as exc:
            last_error = exc
    else:
        salvaged = _salvage_sql_field(candidate)
        if salvaged is None:
            logger.debug("Resposta não-JSON do modelo: %r", text)
            raise LLMResponseError(f"JSON inválido na resposta: {last_error}")
        logger.warning("JSON malformado; campo 'sql' recuperado sem as premissas.")
        return salvaged
    if not isinstance(parsed, dict):
        raise LLMResponseError("A resposta JSON não é um objeto.")
    return parsed


def _repair_candidates(candidate: str) -> tuple[str, ...]:
    """Variações para os desvios de formato observados em modelos `:free`.

    1. Original (`strict=False` já aceita quebras de linha cruas em strings).
    2. `\\'` -> `'` (escape inválido em JSON).
    3. Escape duplo: o modelo fecha a string `sql` e escreve o resto do objeto
       como se ainda estivesse dentro de uma string (`,\\n\\"premissas\\": ...`).
       Desfazer `\\"` e `\\n` recupera a estrutura (as quebras viram literais,
       aceitas pelo decoder leniente).
    """
    unquoted = candidate.replace(r"\'", "'")
    unescaped = unquoted.replace(r"\"", '"').replace(r"\n", "\n")
    return (candidate, unquoted, unescaped)


def _salvage_sql_field(candidate: str) -> dict[str, Any] | None:
    """Último recurso: extrai só o campo `sql` (evita gastar 1 req de retry)."""
    match = _SQL_FIELD_RE.search(candidate)
    if match is None:
        return None
    try:
        sql = json.loads(f'"{match.group(1)}"', strict=False)
    except json.JSONDecodeError:
        return None
    return {"sql": sql, "premissas": []} if sql.strip() else None
