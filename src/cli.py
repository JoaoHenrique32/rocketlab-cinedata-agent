"""Interface de terminal do agente CineData.

Uso:
    python -m src.cli "Quais são os 5 filmes mais populares?"
    python -m src.cli                      # modo interativo
    python -m src.cli --no-summary "..."   # resumo local, economiza 1 req
    python -m src.cli --quota              # requisições usadas hoje
"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.markdown import Markdown

from src.agent.cache import AgentCache
from src.agent.formatter import to_markdown
from src.agent.llm import LLMError
from src.agent.workflow import CineDataAgent
from src.config import Settings, get_settings

console = Console()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="Agente Text-to-SQL da CineData Analytics (somente leitura).",
    )
    parser.add_argument("question", nargs="*", help="pergunta em linguagem natural")
    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="gera o resumo localmente, sem chamar o LLM (economiza 1 req)",
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="ignora respostas em cache"
    )
    parser.add_argument(
        "--quota", action="store_true", help="mostra o uso de requisições de hoje"
    )
    parser.add_argument(
        "--clear-cache", action="store_true", help="apaga as respostas em cache"
    )
    return parser.parse_args(argv)


def _quota_line(cache: AgentCache, settings: Settings) -> str:
    used = cache.requests_today()
    line = f"Requisições hoje: {used}/{settings.daily_request_limit}"
    if used >= settings.quota_warning_at:
        line = f"[bold yellow]⚠ {line} — perto do limite diário[/]"
    return line


def _answer(agent: CineDataAgent, question: str, args: argparse.Namespace) -> None:
    with console.status("Consultando a camada Gold..."):
        response = agent.ask(
            question, summarize=not args.no_summary, use_cache=not args.no_cache
        )
    console.print(Markdown(to_markdown(response)))
    if agent.cache:
        console.print(_quota_line(agent.cache, get_settings()), style="dim")
    console.rule()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()

    if args.quota or args.clear_cache:
        cache = AgentCache(settings.resolved_cache_path())
        if args.clear_cache:
            cache.clear()
            console.print("Cache de respostas apagado.")
        if args.quota:
            console.print(_quota_line(cache, settings))
        cache.close()
        return 0

    try:
        agent = CineDataAgent.from_settings(settings)
    except LLMError as exc:
        console.print(f"[bold red]Erro de configuração:[/] {exc}")
        return 1

    try:
        if args.question:
            _answer(agent, " ".join(args.question), args)
            return 0
        console.print(
            "[bold]CineData Analytics[/] — faça sua pergunta "
            "([dim]'sair' para encerrar[/])"
        )
        while True:
            try:
                question = console.input("[bold cyan]> [/]").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if question.lower() in {"sair", "exit", "quit"}:
                break
            if question:
                _answer(agent, question, args)
        return 0
    finally:
        agent.close()


if __name__ == "__main__":
    sys.exit(main())
