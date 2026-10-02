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

    openrouter_api_key: str = ""
    db_path: Path = PROJECT_ROOT / "data" / "cinerocket.db"
    query_timeout_s: float = Field(default=30.0, gt=0)
    max_rows: int = Field(default=200, gt=0)

    def resolved_db_path(self) -> Path:
        """Resolve `db_path` relativo à raiz do projeto quando não for absoluto."""
        path = self.db_path
        return path if path.is_absolute() else PROJECT_ROOT / path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retorna a instância única de configurações."""
    return Settings()
