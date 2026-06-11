"""Feature flags de marketing.

Backend Redis si BF_REDIS_URL esta definido; si no, archivo JSON atomico.
La semantica es identica: los workers consultan antes de CADA accion, por
lo que apagar `master` detiene el pipeline 5 en el siguiente tick.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Protocol

from bookforge.core.config import settings
from bookforge.core.models import MarketingFlags

FLAGS_KEY = "bookforge:marketing_flags"


class FlagStore(Protocol):
    def get(self) -> MarketingFlags: ...
    def set(self, flags: MarketingFlags) -> None: ...


class FileFlagStore:
    def __init__(self, path: Path | None = None):
        self._path = path or settings.flags_file
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def get(self) -> MarketingFlags:
        if not self._path.exists():
            return MarketingFlags()
        try:
            return MarketingFlags.model_validate_json(
                self._path.read_text(encoding="utf-8")
            )
        except Exception:
            # Archivo corrupto = todo apagado. Fail-safe.
            return MarketingFlags()

    def set(self, flags: MarketingFlags) -> None:
        # Escritura atomica: tmp + rename
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(flags.model_dump_json(indent=2))
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


class RedisFlagStore:
    def __init__(self, url: str):
        import redis  # import perezoso: solo si se usa
        self._r = redis.Redis.from_url(url, decode_responses=True)

    def get(self) -> MarketingFlags:
        raw = self._r.get(FLAGS_KEY)
        if not raw:
            return MarketingFlags()
        try:
            return MarketingFlags.model_validate_json(raw)
        except Exception:
            return MarketingFlags()

    def set(self, flags: MarketingFlags) -> None:
        self._r.set(FLAGS_KEY, flags.model_dump_json())


def get_flag_store() -> FlagStore:
    if settings.redis_url:
        return RedisFlagStore(settings.redis_url)
    return FileFlagStore()


# Helpers de operacion -------------------------------------------------------

def kill_switch(store: FlagStore | None = None) -> MarketingFlags:
    """Apaga TODO el marketing. Idempotente."""
    store = store or get_flag_store()
    flags = store.get()
    flags.master = False
    store.set(flags)
    return flags


def set_channel(channel: str, value: bool,
                store: FlagStore | None = None) -> MarketingFlags:
    store = store or get_flag_store()
    flags = store.get()
    if channel == "master":
        flags.master = value
    elif channel in ("organic", "paid"):
        getattr(flags, channel).enabled = value
    elif hasattr(flags.organic, channel):
        setattr(flags.organic, channel, value)
    elif hasattr(flags.paid, channel):
        setattr(flags.paid, channel, value)
    else:
        raise ValueError(f"Canal desconocido: {channel}")
    store.set(flags)
    return flags
