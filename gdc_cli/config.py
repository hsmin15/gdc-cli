from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .env import load_env_file


@dataclass(frozen=True)
class Settings:
    base_url: str
    cache_dir: Path
    token: str | None
    cache_ttl_seconds: int
    request_delay_seconds: float


def get_settings() -> Settings:
    load_env_file()
    return Settings(
        base_url=os.getenv("GDC_API_BASE_URL", "https://api.gdc.cancer.gov").rstrip("/"),
        cache_dir=Path(os.getenv("GDC_CACHE_DIR", ".gdc_cache")),
        token=os.getenv("GDC_TOKEN") or None,
        cache_ttl_seconds=int(os.getenv("GDC_CACHE_TTL_SECONDS", "86400")),
        request_delay_seconds=float(os.getenv("GDC_REQUEST_DELAY_SECONDS", "0.1")),
    )
