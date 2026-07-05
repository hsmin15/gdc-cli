from __future__ import annotations

import json
import time
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from .client import GDCClient
from .config import get_settings


class SchemaCache:
    def __init__(
        self,
        client: GDCClient | None = None,
        cache_dir: Path | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        settings = get_settings()
        self.client = client or GDCClient()
        self.cache_dir = cache_dir or settings.cache_dir
        self.ttl_seconds = settings.cache_ttl_seconds if ttl_seconds is None else ttl_seconds

    def _path(self, endpoint: str) -> Path:
        safe = endpoint.replace("/", "_")
        return self.cache_dir / f"{safe}_mapping.json"

    def get_mapping(self, endpoint: str, refresh: bool = False) -> dict[str, Any]:
        path = self._path(endpoint)
        if not refresh and path.exists():
            age = time.time() - path.stat().st_mtime
            if age < self.ttl_seconds:
                return json.loads(path.read_text(encoding="utf-8"))

        mapping = self.client.mapping(endpoint)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(mapping, indent=2, sort_keys=True), encoding="utf-8")
        return mapping

    def fields(self, endpoint: str, refresh: bool = False) -> dict[str, dict[str, Any]]:
        return extract_fields(self.get_mapping(endpoint, refresh=refresh))

    def validate_fields(
        self,
        endpoint: str,
        field_names: list[str],
    ) -> list[tuple[str, list[str]]]:
        available = self.fields(endpoint)
        known = set(available)
        invalid: list[tuple[str, list[str]]] = []
        for field in field_names:
            if field and field not in known:
                invalid.append((field, get_close_matches(field, known, n=5)))
        return invalid


def extract_fields(mapping: dict[str, Any]) -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}

    def add(name: str, node: dict[str, Any]) -> None:
        field_type = node.get("type") or ("object" if "properties" in node else "unknown")
        fields[name] = {"type": field_type}

    def walk(prefix: str, node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                if isinstance(item, str):
                    fields.setdefault(item, {"type": "unknown"})
            return
        if not isinstance(node, dict):
            return

        properties = node.get("properties")
        if prefix and ("type" in node or isinstance(properties, dict)):
            add(prefix, node)
        if isinstance(properties, dict):
            for key, child in properties.items():
                child_name = f"{prefix}.{key}" if prefix else key
                walk(child_name, child)
            return

        for key, child in node.items():
            if key in {"type", "format", "analyzer", "index", "doc_values", "normalizer"}:
                continue
            if key == "fields" and isinstance(child, list):
                walk(prefix, child)
                continue
            if isinstance(child, dict):
                child_name = f"{prefix}.{key}" if prefix else key
                walk(child_name, child)

        if prefix and prefix not in fields:
            add(prefix, node)

    roots: list[Any] = []
    if "_mapping" in mapping:
        roots.append(mapping["_mapping"])
    if "fields" in mapping:
        roots.append(mapping["fields"])
    if not roots:
        roots.append(mapping)

    for root in roots:
        if isinstance(root, dict):
            for key, node in root.items():
                walk(key, node)
        else:
            walk("", root)

    return dict(sorted(fields.items()))
