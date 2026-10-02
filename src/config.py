"""Configuração centralizada do agente, carregada de variáveis de ambiente / .env."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Parâmetros de execução do agente CineData."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Banco
    db_path: Path = PROJECT_ROOT / "data" / "cinerocket.db"
    query_timeout_s: float = Field(default=30.0, gt=0)
    max_rows: int = Field(default=200, gt=0)

    # LLM (OpenRouter). Ordem de `llm_models` = ordem de fallback.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_models: list[str] = [
        "nvidia/nemotron-3-super-120b-a12b:free",
        "google/gemma-4-31b-it:free",
        "qwen/qwen3.8-27b:free",
    ]
    llm_timeout_s: float = Field(default=60.0, gt=0)
    llm_temperature: float = Field(default=0.0, ge=0, le=2)

    # Quota e cache
    daily_request_limit: int = Field(default=50, gt=0)
    quota_warning_at: int = Field(default=40, gt=0)
    cache_path: Path = PROJECT_ROOT / ".cache" / "agent_cache.db"

    def resolved_db_path(self) -> Path:
        """Resolve `db_path` relativo à raiz do projeto quando não for absoluto."""
        return _resolve(self.db_path)

    def resolved_cache_path(self) -> Path:
        """Resolve `cache_path` relativo à raiz do projeto quando não for absoluto."""
        return _resolve(self.cache_path)


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retorna a instância única de configurações."""
    return Settings()
