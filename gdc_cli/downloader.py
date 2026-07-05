from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from .client import GDCClient
from .filters import build_clauses, build_filter, filter_fields, load_aliases
from .schema import SchemaCache


DEFAULT_DOWNLOAD_FIELDS = "file_id,file_name,file_size,access,md5sum,state"


def download_files(
    from_search: Path | None = None,
    filter_expressions: list[str] | None = None,
    alias_names: list[str] | None = None,
    mode: str = "manifest",
    out_dir: Path | None = None,
    yes: bool = False,
    verbose: bool = False,
    client: GDCClient | None = None,
    console: Console | None = None,
) -> Path:
    if mode not in {"data", "manifest"}:
        raise ValueError("mode must be data or manifest")
    console = console or Console()
    client = client or GDCClient()
    records = _records_from_input(
        client=client,
        from_search=from_search,
        filter_expressions=filter_expressions,
        alias_names=alias_names,
        verbose=verbose,
        console=console,
    )
    if not records:
        raise ValueError("No file records found to download.")

    output_dir = out_dir or Path("out")
    output_dir.mkdir(parents=True, exist_ok=True)
    ids = [_file_id(record) for record in records]

    if mode == "manifest":
        manifest_text = client.manifest(ids)
        out_path = output_dir / f"gdc_manifest_{_timestamp()}.txt"
        out_path.write_text(manifest_text, encoding="utf-8")
        return out_path

    total_size = sum(_file_size(record) for record in records)
    console.print(f"Files: {len(records)}; total size: {_format_bytes(total_size)}")
    if not yes and not typer.confirm("Download data files now?"):
        raise typer.Abort()

    if not client.token:
        controlled = [r for r in records if _access(r) == "controlled"]
        if controlled:
            console.print(f"Skipping {len(controlled)} controlled-access file(s) without GDC_TOKEN.")
        records = [r for r in records if _access(r) != "controlled"]
    if not records:
        console.print("No downloadable files remain.")
        return output_dir

    ids = [_file_id(record) for record in records]
    if _download_via_gdc_client(client, ids, output_dir, console):
        return output_dir

    _download_builtin(client, records, output_dir, console)
    return output_dir


def _download_builtin(
    client: GDCClient,
    records: list[dict[str, Any]],
    output_dir: Path,
    console: Console,
) -> None:
    """Download files one by one with a progress display (overall + per-file)."""
    total_bytes = sum(_file_size(record) for record in records)
    count = len(records)
    columns = [
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    ]
    with Progress(*columns, console=console) as progress:
        overall = progress.add_task("[green]Overall", total=total_bytes or None)
        for index, record in enumerate(records, 1):
            file_id = _file_id(record)
            # Store each file under its own file_id/ subdir (same layout as gdc-client)
            # so files that share a file_name across cases never overwrite each other.
            destination = output_dir / file_id / _safe_file_name(record)
            size = _file_size(record) or None
            file_task = progress.add_task(f"[{index}/{count}] {destination.name[:36]}", total=size)
            client.download_data(
                file_id,
                destination,
                on_progress=lambda done, total, task=file_task, fallback=size: progress.update(
                    task, completed=done, total=total or fallback
                ),
            )
            _verify_md5(destination, record, console)
            progress.update(file_task, visible=False)
            if total_bytes:
                progress.update(overall, advance=_file_size(record))


def _verify_md5(destination: Path, record: dict[str, Any], console: Console) -> None:
    """Check the download against the GDC-provided md5sum when the search meta carries it.
    Raises on mismatch so a corrupt file never silently poisons the assembled matrix."""
    expected = record.get("md5sum") or record.get("files.md5sum")
    if not expected or (isinstance(expected, float) and pd.isna(expected)):
        return
    digest = hashlib.md5()
    with destination.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != str(expected):
        got = digest.hexdigest()
        destination.unlink(missing_ok=True)  # remove corrupt file so a rerun re-downloads cleanly
        raise ValueError(f"md5 mismatch for {destination.name}: expected {expected}, got {got}")


def _download_via_gdc_client(
    client: GDCClient,
    ids: list[str],
    output_dir: Path,
    console: Console,
) -> bool:
    """Download via the official GDC Data Transfer Tool if it is on PATH.

    Returns True when gdc-client handled the download, False when it is not
    installed (caller then falls back to the built-in per-file downloader).
    """
    exe = shutil.which("gdc-client")
    if not exe:
        console.print("[yellow]gdc-client not found on PATH; using built-in downloader.[/yellow]")
        return False

    manifest_path = output_dir / f"gdc_manifest_{_timestamp()}.txt"
    manifest_path.write_text(client.manifest(ids), encoding="utf-8")
    cmd = [exe, "download", "-m", str(manifest_path), "-d", str(output_dir)]
    token_file: Path | None = None
    if client.token:
        token_file = output_dir / ".gdc_token.tmp"
        token_file.write_text(client.token, encoding="utf-8")
        try:  # best-effort: keep the token file owner-readable only (no-op on Windows)
            token_file.chmod(0o600)
        except OSError:
            pass
        cmd += ["-t", str(token_file)]
    console.print(f"[cyan]Running gdc-client for {len(ids)} file(s)...[/cyan]")
    try:
        subprocess.run(cmd, check=True)
    finally:
        if token_file and token_file.exists():
            token_file.unlink()
    return True


def _records_from_input(
    client: GDCClient,
    from_search: Path | None,
    filter_expressions: list[str] | None,
    alias_names: list[str] | None,
    verbose: bool,
    console: Console,
) -> list[dict[str, Any]]:
    if from_search:
        return _read_search_records(from_search)
    aliases = load_aliases()
    clauses, warnings = build_clauses(filter_expressions, alias_names, aliases)
    for warning in warnings:
        console.print(f"[yellow]{warning}[/yellow]")
    filter_json = build_filter(clauses)
    schema = SchemaCache(client=client)
    invalid = schema.validate_fields("files", filter_fields(filter_json))
    if invalid:
        details = "; ".join(_format_invalid(field, suggestions) for field, suggestions in invalid)
        raise ValueError(f"Invalid file filter field(s): {details}")
    payload: dict[str, Any] = {"fields": DEFAULT_DOWNLOAD_FIELDS}
    if filter_json:
        payload["filters"] = filter_json
    if verbose:
        console.print_json(data=payload)
    return client.paginate("files", payload)


def _read_search_records(path: Path) -> list[dict[str, Any]]:
    frame = pd.read_csv(path, sep=None, engine="python")
    return frame.to_dict(orient="records")


def _file_id(record: dict[str, Any]) -> str:
    for key in ("file_id", "id", "files.file_id"):
        value = record.get(key)
        if value:
            return str(value)
    raise ValueError("Search result must contain file_id or id column.")


def _file_size(record: dict[str, Any]) -> int:
    for key in ("file_size", "files.file_size"):
        value = record.get(key)
        if pd.notna(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    return 0


def _access(record: dict[str, Any]) -> str:
    return str(record.get("access", record.get("files.access", "open"))).lower()


def _safe_file_name(record: dict[str, Any]) -> str:
    name = str(record.get("file_name") or record.get("files.file_name") or _file_id(record))
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def _format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _format_invalid(field: str, suggestions: list[str]) -> str:
    if suggestions:
        return f"{field} (did you mean: {', '.join(suggestions)})"
    return field


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")
