"""Dublês de teste: LLM roteirizado e contador de quota em memória."""

from __future__ import annotations

from typing import Callable

import httpx2
import openai

from src.agent.llm import Messages

_REQUEST = httpx2.Request("POST", "https://openrouter.ai/api/v1/chat/completions")


def status_error(cls: type[openai.APIStatusError], status: int, message: str = ""):
    return cls(message, response=httpx2.Response(status, request=_REQUEST), body=None)


def timeout_error() -> openai.APITimeoutError:
    return openai.APITimeoutError(request=_REQUEST)


class MemoryQuota:
    def __init__(self, used: int = 0) -> None:
        self.used = used

    def requests_today(self) -> int:
        return self.used

    def register_request(self) -> int:
        self.used += 1
        return self.used


Step = str | Exception | Callable[[str, Messages], str]


class ScriptedLLM:
    """Completion fake: consome um roteiro de respostas/exceções, em ordem."""

    def __init__(self, *steps: Step) -> None:
        self.steps = list(steps)
        self.calls: list[tuple[str, Messages]] = []

    def __call__(self, model: str, messages: Messages) -> str:
        self.calls.append((model, messages))
        if not self.steps:
            raise AssertionError("LLM chamado mais vezes que o roteiro previa")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(model, messages)
        return step
