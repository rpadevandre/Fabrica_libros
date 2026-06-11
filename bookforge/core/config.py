"""Configuracion central. Todo via variables de entorno o .env."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="BF_",
                                      extra="ignore")

    # LLM
    anthropic_api_key: str = ""
    model_heavy: str = "claude-opus-4-8"      # architect, viability, qa
    model_volume: str = "claude-sonnet-4-6"   # writers, editors, research
    max_retries: int = 3

    # Imagen
    openai_api_key: str = ""                  # gpt-image-2
    image_model: str = "gpt-image-2"

    # Infra
    redis_url: str = ""                       # vacio = flags en memoria/JSON
    database_url: str = "sqlite:///bookforge.db"
    data_dir: Path = Path("data")

    # Economia
    qa_threshold: float = 80.0
    max_qa_cycles: int = 2
    max_cost_per_book_usd: Decimal = Decimal("25.00")

    # Publicacion
    kdp_dry_run: bool = True                  # NUNCA cambiar sin revision
    kdp_email: str = ""
    kdp_ai_disclosure: bool = True            # obligatorio, no tocar

    # Marketing
    flags_file: Path = Path("data/marketing_flags.json")


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
