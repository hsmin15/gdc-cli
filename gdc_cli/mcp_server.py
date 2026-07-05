"""MCP server that exposes the GDC CLI as structured tools (approach 2: direct
function calls, no subprocess).

Run with the ``gdc-mcp`` script (stdio transport) after installing the optional
``mcp`` dependency::

    pip install -e ".[mcp]"
    gdc-mcp

The tools mirror the CLI but return structured JSON so an LLM client can chain
them. Heavy downloads are deliberately kept out of the fast request/response
path: ``gdc_build_dataset_preview`` only previews a cohort (dry run), and the
actual multi-GB download (``gdc_build_dataset_run``) refuses to run unless the
caller passes ``confirm=True`` after reviewing a preview.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from rich.console import Console

from . import __version__
from .assemble import read_frame
from .client import GDCClient
from .cli import build_dataset, facet_buckets, search_metadata
from .filters import load_aliases
from .schema import SchemaCache

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - only hit when the extra is missing
    raise SystemExit(
        "The 'mcp' package is required for the GDC MCP server. "
        "Install it with:  pip install -e \".[mcp]\""
    ) from exc

mcp = FastMCP("gdc")

# Cap on rows returned inline by gdc_search so a huge cohort can't blow up the
# MCP response; the full result is always written to a TSV whose path is returned.
_MAX_INLINE_ROWS = 200


def _rows_from_tsv(path: Path, limit: int) -> tuple[list[dict[str, Any]], int]:
    """Read a TSV written by search_metadata into JSON-safe rows (NaN -> None)."""
    frame = pd.read_csv(path, sep="\t", dtype=str)
    total = int(len(frame))
    clipped = frame.head(limit).where(pd.notna(frame.head(limit)), None)
    return clipped.to_dict(orient="records"), total


@mcp.tool()
def gdc_fields(endpoint: str, search: Optional[str] = None) -> list[dict[str, str]]:
    """List queryable fields for a GDC endpoint (introspection).

    Use this before writing filters to confirm the exact field path.
    endpoint: one of cases, files, projects, annotations.
    search: optional case-insensitive substring to narrow the field names.
    Returns [{"field": ..., "type": ...}, ...].
    """
    fields = SchemaCache().fields(endpoint)
    term = search.lower() if search else None
    return [
        {"field": name, "type": str(meta.get("type", "unknown"))}
        for name, meta in fields.items()
        if not term or term in name.lower()
    ]


@mcp.tool()
def gdc_values(endpoint: str, field: str) -> dict[str, Any]:
    """Show the value distribution (facet) and counts for a single field.

    Essential for getting the exact spelling/casing of filter values
    (e.g. data_type, sample_type). Returns {"field", "values": [{"value","count"}]}.
    """
    invalid = SchemaCache().validate_fields(endpoint, [field])
    if invalid:
        suggestions = invalid[0][1]
        hint = f" (did you mean: {', '.join(suggestions)})" if suggestions else ""
        return {"ok": False, "error": f"Unknown field '{field}' on {endpoint}{hint}"}
    buckets = facet_buckets(GDCClient().facets(endpoint, field), field)
    values = [
        {"value": str(b.get("key")), "count": b.get("doc_count", b.get("count"))}
        for b in buckets
    ]
    return {"ok": True, "field": field, "values": values}


@mcp.tool()
def gdc_aliases() -> dict[str, Any]:
    """List the field aliases (short name -> real path) and filter aliases (flags).

    Field aliases expand inside `fields`; filter aliases are used as flag-style
    filters (e.g. gene_counts, star_counts, open, luad).
    """
    aliases = load_aliases()
    return {
        "fields": aliases.get("fields", {}),
        "filters": {
            name: {"field": spec.get("field"), "op": spec.get("op"), "value": spec.get("value")}
            for name, spec in aliases.get("filters", {}).items()
        },
    }


@mcp.tool()
def gdc_search(
    endpoint: str,
    filters: Optional[list[str]] = None,
    fields: Optional[str] = None,
    aliases: Optional[list[str]] = None,
    size: str = "100",
    combine: str = "and",
    filter_json: Optional[str] = None,
) -> dict[str, Any]:
    """Search a GDC endpoint and return rows plus the path to the full TSV.

    endpoint: cases | files | projects | annotations.
    filters: expressions like ["cases.primary_site in bronchus and lung", "stage not missing"].
             Values with internal commas must be quoted inside the string.
    fields: comma-separated output columns; field aliases are expanded here
            (e.g. "case,sex,age,stage").
    aliases: filter-alias flags (e.g. ["gene_counts","open"]).
    size: max records ("all" for everything). combine: "and" or "or".
    filter_json: raw GDC filter JSON for nested AND/OR that the expression grammar can't reach.

    Returns up to 200 rows inline (`rows`), the true `total_rows`, and `out_path`
    of the complete TSV on disk.
    """
    try:
        out_path = search_metadata(
            endpoint=endpoint,
            filter_expressions=filters,
            alias_names=aliases,
            fields=fields,
            size=size,
            combine_op=combine,
            filter_json=filter_json,
        )
    except Exception as exc:  # noqa: BLE001 - surface as structured error
        return {"ok": False, "error": type(exc).__name__, "message": str(exc)}
    rows, total = _rows_from_tsv(out_path, _MAX_INLINE_ROWS)
    return {
        "ok": True,
        "endpoint": endpoint,
        "total_rows": total,
        "returned_rows": len(rows),
        "truncated": total > len(rows),
        "out_path": str(out_path),
        "rows": rows,
    }


def _run_build_dataset(dry_run: bool, out: str, **kwargs: Any) -> dict[str, Any]:
    """Shared driver for the preview/run tools: run build_dataset with a recording
    console so the printed cohort table can be returned as text, and read back the
    provenance sidecar for the structured counts."""
    buffer = io.StringIO()
    console = Console(file=buffer, record=True, width=100)
    out_path = Path(out)
    try:
        result = build_dataset(out=out_path, dry_run=dry_run, console=console, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__, "message": str(exc), "log": buffer.getvalue()}

    prov_path = out_path.parent / f"{out_path.stem}_provenance.json"
    provenance = json.loads(prov_path.read_text(encoding="utf-8")) if prov_path.exists() else {}
    payload: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "console": buffer.getvalue(),
        "counts": provenance.get("counts", {}),
        "provenance_path": str(prov_path) if prov_path.exists() else None,
    }
    if result is not None:
        payload["out_path"] = str(result)
    return payload


@mcp.tool()
def gdc_build_dataset_preview(
    omics: str = "expr,mut",
    project: Optional[str] = None,
    cohort_filters: Optional[list[str]] = None,
    clinical: str = "",
    paired: bool = False,
    tumor_only: bool = False,
    sample_type: Optional[str] = None,
    age_unit: str = "days",
    level: Optional[str] = None,
    out: str = "out/dataset.parquet",
) -> dict[str, Any]:
    """Preview a build-dataset cohort WITHOUT downloading (dry run).

    Reports the cohort case count, the TRUE final table row count, how many cases
    are dropped (union cases lacking expression), and per-omics file counts/sizes.
    Always call this before gdc_build_dataset_run.

    omics: comma list from expr,mut,cna,mirna,methyl,prot.
    cohort_filters: case-level filters (e.g. ["cases.primary_site in bronchus and lung"]).
    project: TCGA/CPTAC project ids (comma list); omit to scan all of GDC.
    clinical: clinical field/alias list (e.g. "sex,age,stage"); required when omics has no expr.
    paired: keep only cases with every requested omics. tumor_only: drop normal tissue.
    Returns {counts, console (the rendered preview table), provenance_path}.
    """
    return _run_build_dataset(
        dry_run=True,
        out=out,
        omics=omics,
        project=project,
        cohort_filters=cohort_filters,
        clinical_fields=clinical,
        paired=paired,
        tumor_only=tumor_only,
        sample_type=sample_type,
        age_unit=age_unit,
        level=level,
        size="all",
    )


@mcp.tool()
def gdc_build_dataset_run(
    confirm: bool,
    omics: str = "expr,mut",
    project: Optional[str] = None,
    cohort_filters: Optional[list[str]] = None,
    clinical: str = "",
    paired: bool = False,
    tumor_only: bool = False,
    sample_type: Optional[str] = None,
    age_unit: str = "days",
    level: Optional[str] = None,
    out: str = "out/dataset.parquet",
) -> dict[str, Any]:
    """Actually download and assemble a dataset. LONG-RUNNING (can be many GB / minutes).

    Guardrail: refuses to run unless confirm=True. Run gdc_build_dataset_preview
    first, show the user the download size and final row count, and only pass
    confirm=True once they approve. Parameters are identical to the preview tool.
    Returns the final table path, per-omics counts, and provenance path.
    """
    if not confirm:
        return {
            "ok": False,
            "error": "confirmation_required",
            "message": "Refusing to download. Run gdc_build_dataset_preview first, then call "
            "again with confirm=True after the user approves the size.",
        }
    return _run_build_dataset(
        dry_run=False,
        out=out,
        omics=omics,
        project=project,
        cohort_filters=cohort_filters,
        clinical_fields=clinical,
        paired=paired,
        tumor_only=tumor_only,
        sample_type=sample_type,
        age_unit=age_unit,
        level=level,
        size="all",
    )


@mcp.tool()
def gdc_describe(path: str, max_columns: int = 40, top: int = 5) -> dict[str, Any]:
    """Lightweight QC for a built table (parquet/TSV).

    Returns overall shape, per-omics group column counts + mean completeness, and
    per-column stats (dtype, non-null fraction, unique count, distribution) for the
    non-omics (clinical/label) columns.
    """
    frame = read_frame(Path(path))
    prefixes = ["expr_", "mut_", "cna_", "mirna_", "methyl_", "prot_"]
    grouped: set[str] = set()
    omics_groups = []
    for prefix in prefixes:
        cols = [c for c in frame.columns if c.startswith(prefix)]
        if not cols:
            continue
        grouped.update(cols)
        comp = float(frame[cols].notna().mean().mean()) if len(frame) else 0.0
        omics_groups.append(
            {"group": prefix.rstrip("_"), "columns": len(cols), "mean_completeness": round(comp, 3)}
        )

    detail_cols = [c for c in frame.columns if c not in grouped][:max_columns]
    columns = []
    for col in detail_cols:
        series = frame[col]
        stat: dict[str, Any] = {
            "column": col,
            "dtype": str(series.dtype),
            "non_null": round(float(series.notna().mean()), 3) if len(frame) else None,
            "unique": int(series.nunique(dropna=True)),
        }
        if pd.api.types.is_numeric_dtype(series) and series.notna().any():
            stat["distribution"] = {
                "min": float(series.min()),
                "median": float(series.median()),
                "max": float(series.max()),
            }
        else:
            counts = series.dropna().astype(str).value_counts().head(top)
            stat["distribution"] = {str(k): int(v) for k, v in counts.items()}
        columns.append(stat)

    return {
        "ok": True,
        "path": path,
        "shape": {"rows": int(frame.shape[0]), "columns": int(frame.shape[1])},
        "omics_groups": omics_groups,
        "columns": columns,
    }


@mcp.tool()
def gdc_version() -> dict[str, str]:
    """Return the installed gdc-cli version and the configured GDC API base URL."""
    return {"version": __version__, "api_base": GDCClient().base_url}


def main() -> None:
    """Entry point for the ``gdc-mcp`` script (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
