from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: Path | None = None) -> Path | None:
    env_path = path or _default_env_path()
    if not env_path or not env_path.exists():
        return None

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value
    return env_path


def _default_env_path() -> Path | None:
    explicit = os.getenv("GDC_ENV_FILE")
    if explicit:
        return Path(explicit)

    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[1] / ".env",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]
