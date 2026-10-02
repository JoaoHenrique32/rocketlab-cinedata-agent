"""Testes offline do cliente LLM: fallback, quota e parsing de JSON."""

import openai
import pytest

from src.agent.llm import (
    LLMClient,
    LLMError,
    LLMResponseError,
    QuotaExceededError,
    parse_json_object,
    strip_reasoning,
)
from tests.fakes import MemoryQuota, ScriptedLLM, status_error, timeout_error

MODELS = ["m1:free", "m2:free", "m3:free"]
MSG = [{"role": "user", "content": "oi"}]


def make_client(llm: ScriptedLLM, quota: MemoryQuota | None = None, limit: int = 50):
    return LLMClient(llm, MODELS, quota or MemoryQuota(), daily_limit=limit)


def test_first_model_success() -> None:
    llm = ScriptedLLM("ok")
    response = make_client(llm).complete(MSG)
    assert (response.content, response.model, response.attempts) == ("ok", "m1:free", 1)


@pytest.mark.parametrize(
    "failure",
    [
        status_error(openai.RateLimitError, 429, "rate limited upstream"),
        status_error(openai.InternalServerError, 503),
        status_error(openai.NotFoundError, 404),
        status_error(openai.BadRequestError, 400),
        timeout_error(),
        "   ",
    ],
    ids=["429", "503", "404", "400", "timeout", "vazio"],
)
def test_falls_back_to_next_model(failure) -> None:
    llm = ScriptedLLM(failure, "ok")
    quota = MemoryQuota()
    response = make_client(llm, quota).complete(MSG)
    assert response.model == "m2:free"
    assert [model for model, _ in llm.calls] == ["m1:free", "m2:free"]
    assert quota.used == 2  # toda tentativa conta


def test_all_models_fail_raises_with_details() -> None:
    llm = ScriptedLLM(timeout_error(), timeout_error(), timeout_error())
    with pytest.raises(LLMError, match="m3:free"):
        make_client(llm).complete(MSG)


@pytest.mark.parametrize(
    "fatal",
    [
        status_error(openai.AuthenticationError, 401),
        status_error(openai.PermissionDeniedError, 403),
        status_error(openai.APIStatusError, 402),
    ],
    ids=["401", "403", "402"],
)
def test_fatal_errors_do_not_fall_back(fatal) -> None:
    llm = ScriptedLLM(fatal)
    with pytest.raises(LLMError):
        make_client(llm).complete(MSG)
    assert len(llm.calls) == 1


def test_openrouter_daily_limit_aborts() -> None:
    llm = ScriptedLLM(
        status_error(
            openai.RateLimitError, 429, "Rate limit exceeded: free-models-per-day"
        )
    )
    with pytest.raises(QuotaExceededError):
        make_client(llm).complete(MSG)
    assert len(llm.calls) == 1


def test_local_quota_blocks_before_calling() -> None:
    llm = ScriptedLLM()
    with pytest.raises(QuotaExceededError):
        make_client(llm, MemoryQuota(used=50), limit=50).complete(MSG)
    assert llm.calls == []


def test_local_quota_stops_fallback_midway() -> None:
    llm = ScriptedLLM(timeout_error())
    with pytest.raises(QuotaExceededError):
        make_client(llm, MemoryQuota(used=49), limit=50).complete(MSG)
    assert len(llm.calls) == 1


# ---- parse_json_object ----------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        '{"sql": "SELECT 1", "premissas": []}',
        '```json\n{"sql": "SELECT 1", "premissas": []}\n```',
        '<think>raciocínio {não json}</think>{"sql": "SELECT 1", "premissas": []}',
        'Claro! Segue: {"sql": "SELECT 1", "premissas": []} Espero ter ajudado.',
        '{"sql": "SELECT 1", "premissas": ["linha 1\nlinha 2"]}',
        r'{"sql": "SELECT 1", "premissas": ["status = \'Lançado\'"]}',
    ],
    ids=["puro", "cerca", "think", "texto-ao-redor", "newline-cru", "escape-aspas"],
)
def test_parse_json_object_tolerates_noise(text: str) -> None:
    assert parse_json_object(text)["sql"] == "SELECT 1"


@pytest.mark.parametrize("text", ["sem json aqui", "{quebrado", "[1, 2]"])
def test_parse_json_object_rejects_invalid(text: str) -> None:
    with pytest.raises(LLMResponseError):
        parse_json_object(text)


# Formato real observado no nemotron (avaliação de 2026-10-02): após fechar a
# string "sql", o restante do objeto vem com escape duplo.
DOUBLE_ESCAPED = (
    r'{"sql": "SELECT a\nFROM t\nWHERE x = '
    "'Lançado'"
    r'",\n\"premissas\": '
    r'[\n  \"Premissa A.\",\n  \"Premissa B.\"\n]}"}'
)


def test_parse_json_object_repairs_double_escaped_tail() -> None:
    parsed = parse_json_object(DOUBLE_ESCAPED)
    assert parsed["sql"] == "SELECT a\nFROM t\nWHERE x = 'Lançado'"
    assert parsed["premissas"] == ["Premissa A.", "Premissa B."]


def test_parse_json_object_salvages_sql_field_as_last_resort() -> None:
    text = r'{"sql": "SELECT \"x\"\nFROM t", "premissas": [quebrado'
    assert parse_json_object(text) == {"sql": 'SELECT "x"\nFROM t', "premissas": []}


def test_strip_reasoning() -> None:
    assert strip_reasoning("<think>x</think>  Resumo.") == "Resumo."
