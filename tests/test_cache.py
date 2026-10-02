"""Testes do cache de respostas e do contador de quota."""

from pathlib import Path

import pytest

from src.agent.cache import AgentCache, cache_key, normalize_question


@pytest.fixture
def cache(tmp_path: Path) -> AgentCache:
    c = AgentCache(tmp_path / "sub" / "cache.db")
    yield c
    c.close()


@pytest.mark.parametrize(
    "variant",
    [
        "Quais os 5 filmes mais populares?",
        "  quais os 5 filmes   MAIS populares ",
        "Quais os 5 filmes mais populares!?",
    ],
)
def test_trivial_variations_share_key(variant: str) -> None:
    assert cache_key(variant) == cache_key("quais os 5 filmes mais populares")


def test_different_questions_have_different_keys() -> None:
    assert cache_key("top 5 filmes") != cache_key("top 10 filmes")


def test_normalize_keeps_accents() -> None:
    assert normalize_question("Gênero  Ação?") == "gênero ação"


def test_put_get_and_summary_roundtrip(cache: AgentCache) -> None:
    assert cache.get("P?") is None
    cache.put("P?", "SELECT 1", ["premissa"], "m1:free")
    hit = cache.get("p")
    assert hit is not None
    assert (hit.sql, hit.premissas, hit.summary, hit.model) == (
        "SELECT 1",
        ["premissa"],
        None,
        "m1:free",
    )
    cache.set_summary("P?", "Resumo.")
    assert cache.get("P?").summary == "Resumo."


def test_clear_removes_responses_but_keeps_quota(cache: AgentCache) -> None:
    cache.put("P?", "SELECT 1", [], None)
    cache.register_request()
    cache.clear()
    assert cache.get("P?") is None
    assert cache.requests_today() == 1


def test_quota_counter_persists(tmp_path: Path) -> None:
    path = tmp_path / "cache.db"
    first = AgentCache(path)
    assert first.requests_today() == 0
    assert first.register_request() == 1
    assert first.register_request() == 2
    first.close()
    second = AgentCache(path)
    assert second.requests_today() == 2
    second.close()
