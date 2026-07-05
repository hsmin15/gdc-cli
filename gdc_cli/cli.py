from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import __version__

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from .assemble import (
    assemble_cnv,
    assemble_expression,
    assemble_methylation,
    assemble_mirna,
    assemble_mutation,
    assemble_protein,
    build_ml_table,
    read_frame,
    write_frame,
)
from .client import GDCClient
from .downloader import _format_bytes, download_files
from .filters import (
    build_clauses,
    build_filter,
    expand_field_list,
    filter_fields,
    load_aliases,
    parse_filter_json,
)
from .schema import SchemaCache

app = typer.Typer(no_args_is_help=True)
console = Console()
SEARCH_CONTEXT = {"allow_extra_args": True, "ignore_unknown_options": True}


@app.command("fields")
def fields_command(
    endpoint: str,
    search: Optional[str] = typer.Option(None, "--search", "-s"),
    refresh: bool = typer.Option(False, "--refresh"),
) -> None:
    fields = SchemaCache().fields(endpoint, refresh=refresh)
    table = Table("field", "type")
    term = search.lower() if search else None
    for name, meta in fields.items():
        if term and term not in name.lower():
            continue
        table.add_row(name, str(meta.get("type", "unknown")))
    console.print(table)


@app.command("values")
def values_command(endpoint: str, field: str) -> None:
    invalid = SchemaCache().validate_fields(endpoint, [field])
    if invalid:
        raise typer.BadParameter(_format_invalid_fields(invalid))
    buckets = facet_buckets(GDCClient().facets(endpoint, field), field)
    table = Table("value", "count")
    for bucket in buckets:
        table.add_row(str(bucket.get("key")), str(bucket.get("doc_count", bucket.get("count", ""))))
    console.print(table)


@app.command("aliases")
def aliases_command() -> None:
    aliases = load_aliases()
    table = Table("type", "alias", "expansion")
    for name, fields in aliases.get("fields", {}).items():
        table.add_row("field", name, ", ".join(fields))
    for name, spec in aliases.get("filters", {}).items():
        value = spec.get("value")
        table.add_row("filter", name, f"{spec.get('field')} {spec.get('op')} {value}")
    console.print(table)


@app.command("search", context_settings=SEARCH_CONTEXT)
def search_command(
    ctx: typer.Context,
    endpoint: str,
    filter_expression: Optional[list[str]] = typer.Option(None, "--filter", "-f"),
    filter_json: Optional[str] = typer.Option(None, "--filter-json"),
    alias: Optional[list[str]] = typer.Option(None, "--alias"),
    fields: Optional[str] = typer.Option(None, "--fields"),
    output_format: str = typer.Option("tsv", "--format"),
    size: str = typer.Option("100", "--size"),
    sort: Optional[str] = typer.Option(None, "--sort"),
    out: Optional[Path] = typer.Option(None, "--out"),
    or_group: bool = typer.Option(False, "--or"),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    alias_names = (alias or []) + alias_flags_from_extra(ctx.args)
    out_path = search_metadata(
        endpoint=endpoint,
        filter_expressions=filter_expression,
        alias_names=alias_names,
        fields=fields,
        output_format=output_format,
        size=size,
        sort=sort,
        out=out,
        combine_op="or" if or_group else "and",
        filter_json=filter_json,
        verbose=verbose,
        console=console,
    )
    console.print(f"Wrote {out_path}")


@app.command("download", context_settings=SEARCH_CONTEXT)
def download_command(
    ctx: typer.Context,
    from_search: Optional[Path] = typer.Option(None, "--from-search"),
    filter_expression: Optional[list[str]] = typer.Option(None, "--filter", "-f"),
    alias: Optional[list[str]] = typer.Option(None, "--alias"),
    mode: str = typer.Option("manifest", "--mode"),
    out_dir: Optional[Path] = typer.Option(None, "--out-dir"),
    yes: bool = typer.Option(False, "--yes"),
    verbose: bool = typer.Option(False, "--verbose"),
) -> None:
    alias_names = (alias or []) + alias_flags_from_extra(ctx.args)
    out_path = download_files(
        from_search=from_search,
        filter_expressions=filter_expression,
        alias_names=alias_names,
        mode=mode,
        out_dir=out_dir,
        yes=yes,
        verbose=verbose,
        console=console,
    )
    console.print(f"Wrote {out_path}")


@app.command("assemble-expr")
def assemble_expr_command(
    meta: Path = typer.Option(..., "--meta"),
    files_dir: Path = typer.Option(..., "--files-dir"),
    value: str = typer.Option("tpm_unstranded", "--value"),
    gene: str = typer.Option("gene_name", "--gene"),
    out: Path = typer.Option(..., "--out"),
) -> None:
    frame = assemble_expression(meta, files_dir, value_col=value, gene_key=gene, console=console)
    write_frame(frame, out)
    console.print(f"Wrote {out} ({frame.shape[0]} samples x {frame.shape[1]} columns)")


@app.command("assemble-cnv")
def assemble_cnv_command(
    meta: Path = typer.Option(..., "--meta"),
    files_dir: Path = typer.Option(..., "--files-dir"),
    value: str = typer.Option("copy_number", "--value"),
    gene: str = typer.Option("gene_name", "--gene"),
    out: Path = typer.Option(..., "--out"),
) -> None:
    frame = assemble_cnv(meta, files_dir, value_col=value, gene_key=gene, console=console)
    write_frame(frame, out)
    console.print(f"Wrote {out} ({frame.shape[0]} cases x {frame.shape[1]} columns)")


@app.command("assemble-mut")
def assemble_mut_command(
    meta: Path = typer.Option(..., "--meta"),
    files_dir: Path = typer.Option(..., "--files-dir"),
    mode: str = typer.Option("count", "--mode"),
    variant_class: Optional[str] = typer.Option(None, "--variant-class"),
    out: Path = typer.Option(..., "--out"),
) -> None:
    classes = [part.strip() for part in variant_class.split(",")] if variant_class else None
    frame = assemble_mutation(meta, files_dir, mode=mode, variant_classes=classes, console=console)
    write_frame(frame, out)
    console.print(f"Wrote {out} ({frame.shape[0]} cases x {frame.shape[1]} columns)")


@app.command("assemble-mirna")
def assemble_mirna_command(
    meta: Path = typer.Option(..., "--meta"),
    files_dir: Path = typer.Option(..., "--files-dir"),
    value: str = typer.Option("reads_per_million_miRNA_mapped", "--value"),
    out: Path = typer.Option(..., "--out"),
) -> None:
    frame = assemble_mirna(meta, files_dir, value_col=value, console=console)
    write_frame(frame, out)
    console.print(f"Wrote {out} ({frame.shape[0]} cases x {frame.shape[1]} columns)")


@app.command("assemble-methyl")
def assemble_methyl_command(
    meta: Path = typer.Option(..., "--meta"),
    files_dir: Path = typer.Option(..., "--files-dir"),
    out: Path = typer.Option(..., "--out"),
) -> None:
    frame = assemble_methylation(meta, files_dir, console=console)
    write_frame(frame, out)
    console.print(f"Wrote {out} ({frame.shape[0]} cases x {frame.shape[1]} columns)")


@app.command("assemble-protein")
def assemble_protein_command(
    meta: Path = typer.Option(..., "--meta"),
    files_dir: Path = typer.Option(..., "--files-dir"),
    value: str = typer.Option("protein_expression", "--value"),
    out: Path = typer.Option(..., "--out"),
) -> None:
    frame = assemble_protein(meta, files_dir, value_col=value, console=console)
    write_frame(frame, out)
    console.print(f"Wrote {out} ({frame.shape[0]} cases x {frame.shape[1]} columns)")


@app.command("build-ml")
def build_ml_command(
    clinical: Optional[Path] = typer.Option(None, "--clinical"),
    expr: Optional[Path] = typer.Option(None, "--expr"),
    mut: Optional[Path] = typer.Option(None, "--mut"),
    cna: Optional[Path] = typer.Option(None, "--cna"),
    mirna: Optional[Path] = typer.Option(None, "--mirna"),
    methyl: Optional[Path] = typer.Option(None, "--methyl"),
    protein: Optional[Path] = typer.Option(None, "--protein"),
    out: Path = typer.Option(..., "--out"),
) -> None:
    frame = build_ml_table(
        clinical,
        expr_path=expr,
        mut_path=mut,
        cna_path=cna,
        mirna_path=mirna,
        methyl_path=methyl,
        protein_path=protein,
        console=console,
    )
    write_frame(frame, out)
    console.print(f"Wrote {out} ({frame.shape[0]} rows x {frame.shape[1]} columns)")


@app.command("describe")
def describe_command(
    path: Path,
    max_columns: int = typer.Option(40, "--max-columns"),
    top: int = typer.Option(5, "--top"),
) -> None:
    """Lightweight QC for a built table (parquet/TSV): shape, per-omics completeness,
    and per-column stats for the clinical/label columns."""
    frame = read_frame(path)
    console.print(f"[bold]{path}[/bold]: {frame.shape[0]} rows x {frame.shape[1]} columns")

    prefixes = ["expr_", "mut_", "cna_", "mirna_", "methyl_", "prot_"]
    grouped: set[str] = set()
    group_table = Table("omics group", "columns", "mean completeness")
    any_group = False
    for prefix in prefixes:
        cols = [c for c in frame.columns if c.startswith(prefix)]
        if not cols:
            continue
        any_group = True
        grouped.update(cols)
        comp = float(frame[cols].notna().mean().mean()) if len(frame) else 0.0
        group_table.add_row(prefix.rstrip("_"), str(len(cols)), f"{comp:.0%}")
    if any_group:
        console.print(group_table)

    detail_cols = [c for c in frame.columns if c not in grouped]
    table = Table("column", "dtype", "non-null", "unique", "distribution")
    for col in detail_cols[:max_columns]:
        series = frame[col]
        nonnull = f"{series.notna().mean():.0%}" if len(frame) else "-"
        unique = str(series.nunique(dropna=True))
        if pd.api.types.is_numeric_dtype(series) and series.notna().any():
            dist = f"min={_num(series.min())} med={_num(series.median())} max={_num(series.max())}"
        else:
            counts = series.dropna().astype(str).value_counts().head(top)
            dist = ", ".join(f"{key}={val}" for key, val in counts.items())
        table.add_row(col, str(series.dtype), nonnull, unique, dist[:60])
    console.print(table)
    if len(detail_cols) > max_columns:
        console.print(f"... {len(detail_cols) - max_columns} more column(s); raise --max-columns to see them.")


def _num(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.4g}"


@app.command("build-dataset")
def build_dataset_command(
    out: Path = typer.Option(..., "--out"),
    project: Optional[str] = typer.Option(None, "--project"),
    clinical: str = typer.Option("", "--clinical"),
    omics: str = typer.Option("expr,mut", "--omics"),
    paired: bool = typer.Option(False, "--paired"),
    size: str = typer.Option("all", "--size"),
    level: Optional[str] = typer.Option(None, "--level", help="Row unit: case or sample (default: sample if expr else case)."),
    tumor_only: bool = typer.Option(False, "--tumor-only", help="Drop normal-tissue samples from the cohort."),
    sample_type: Optional[str] = typer.Option(None, "--sample-type", help="Keep only these sample types (comma-separated, e.g. 'Primary Tumor')."),
    prefer_sample_type: Optional[str] = typer.Option(None, "--prefer-sample-type", help="Preferred sample type when collapsing a case to one row."),
    age_unit: str = typer.Option("days", "--age-unit", help="days (raw GDC) or years (convert age columns)."),
    multi_value: str = typer.Option("join", "--multi-value", help="Multi-valued clinical fields: join, first, or last."),
    skip_existing: bool = typer.Option(False, "--skip-existing", help="Reuse already-assembled omics matrices in the work dir."),
    methyl_top_variance: Optional[int] = typer.Option(None, "--methyl-top-variance", help="Keep only the N highest-variance methylation probes."),
    expr_value: str = typer.Option("tpm_unstranded", "--expr-value"),
    mut_mode: str = typer.Option("count", "--mut-mode"),
    cnv_workflow: str = typer.Option("ASCAT3", "--cnv-workflow"),
    mirna_value: str = typer.Option("reads_per_million_miRNA_mapped", "--mirna-value"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    cohort_filter: Optional[list[str]] = typer.Option(None, "--cohort-filter"),
    work_dir: Optional[Path] = typer.Option(None, "--work-dir"),
    json_errors: bool = typer.Option(False, "--json-errors", help="On failure, print a {\"ok\":false,...} JSON error instead of a traceback."),
) -> None:
    try:
        out_path = build_dataset(
            project=project,
            clinical_fields=clinical,
            omics=omics,
            out=out,
            paired=paired,
            size=size,
            level=level,
            tumor_only=tumor_only,
            sample_type=sample_type,
            prefer_sample_type=prefer_sample_type,
            age_unit=age_unit,
            multi_value=multi_value,
            skip_existing=skip_existing,
            methyl_top_variance=methyl_top_variance,
            expr_value=expr_value,
            mut_mode=mut_mode,
            cnv_workflow=cnv_workflow,
            mirna_value=mirna_value,
            dry_run=dry_run,
            cohort_filters=cohort_filter,
            work_dir=work_dir,
            console=console,
        )
    except Exception as exc:  # noqa: BLE001 - surface as structured error when asked
        if json_errors:
            console.print_json(data={"ok": False, "error": type(exc).__name__, "message": str(exc)})
            raise typer.Exit(1)
        raise
    if out_path is not None:
        console.print(f"Wrote {out_path}")


# Omics registry: alias flags for the file search, the search field set, extra raw
# filters, the assemble function, and the build_ml_table keyword it feeds.
_SAMPLE_FIELDS = "file,filename,filesize,md5sum,file_case,file_project,file_sample_type,cases.samples.submitter_id"
_MUT_FIELDS = "file,filename,filesize,md5sum,file_case,file_project,cases.samples.submitter_id"
OMICS = {
    "expr": {"aliases": ["gene_counts", "star_counts", "open"], "fields": _SAMPLE_FIELDS, "ml_key": "expr_path"},
    "cna": {"aliases": ["gene_cnv", "open"], "fields": _SAMPLE_FIELDS, "ml_key": "cna_path"},
    "mut": {"aliases": ["somatic_mutation", "open"], "fields": _MUT_FIELDS, "ml_key": "mut_path"},
    "mirna": {"aliases": ["mirna_counts", "open"], "fields": _SAMPLE_FIELDS, "extra": ["data_format in txt"], "ml_key": "mirna_path"},
    "methyl": {"aliases": ["methylation_beta", "open"], "fields": _SAMPLE_FIELDS, "ml_key": "methyl_path"},
    "prot": {"aliases": ["protein_expression", "open"], "fields": _SAMPLE_FIELDS, "ml_key": "protein_path"},
}


def build_dataset(
    project: str | None,
    clinical_fields: str,
    omics: str,
    out: Path,
    paired: bool = False,
    size: str = "all",
    level: str | None = None,
    tumor_only: bool = False,
    sample_type: str | None = None,
    prefer_sample_type: str | None = None,
    age_unit: str = "days",
    multi_value: str = "join",
    skip_existing: bool = False,
    methyl_top_variance: int | None = None,
    expr_value: str = "tpm_unstranded",
    mut_mode: str = "count",
    cnv_workflow: str = "ASCAT3",
    mirna_value: str = "reads_per_million_miRNA_mapped",
    dry_run: bool = False,
    cohort_filters: list[str] | None = None,
    work_dir: Path | None = None,
    console: Console | None = None,
) -> Path | None:
    """Cohort -> download -> assemble -> join pipeline in one call.

    Cohort is the projects in --project and/or the case-level cohort_filters (raw file
    filter expressions on cases.* paths, e.g. "cases.primary_site in bronchus and lung"),
    or all of GDC when both are omitted. With paired=True only cases that have every
    requested omics are kept (intersection); otherwise cases missing an omics are kept
    with NaN (union). Supported omics: expr, mut, cna, mirna, methyl, prot.

    Row unit (`level`): "sample" keeps one row per expression sample; "case" collapses to
    one row per case. Default is "sample" when expr is requested, else "case". IMPORTANT:
    when expr is requested it is the row base, so in union mode cases that have other omics
    but NO expression cannot appear in the final table and are dropped (reported below);
    the preview shows both the cohort case count and the true final row count.

    Sample policy: `tumor_only` drops normal-tissue samples; `sample_type` keeps only the
    listed types (e.g. "Primary Tumor"); `prefer_sample_type` breaks ties when collapsing
    a case to one row. `age_unit="years"` converts day-unit age columns. `multi_value`
    controls multi-valued clinical fields (join/first/last).

    dry_run=True stops after the metadata search and prints the cohort preview without
    downloading. A `<out>_provenance.json` sidecar records file ids/md5s, filters, alias
    expansions, case counts, and the API base for reproducibility. The final table is
    parquet when `out` ends in .parquet, else TSV (parquet recommended for wide matrices).
    """
    console = console or Console()
    omics_list = [item.strip() for item in omics.split(",") if item.strip()]
    unknown = [item for item in omics_list if item not in OMICS]
    if unknown:
        raise ValueError(f"Unknown omics: {', '.join(unknown)} (choose from {', '.join(OMICS)})")
    if not omics_list:
        raise ValueError(f"Request at least one omics ({', '.join(OMICS)}).")
    if "expr" not in omics_list and not clinical_fields:
        raise ValueError("Without expr you must pass --clinical (used as the per-case row base).")
    if age_unit not in {"days", "years"}:
        raise ValueError("age_unit must be 'days' or 'years'")
    if multi_value not in {"join", "first", "last"}:
        raise ValueError("multi_value must be join, first, or last")

    has_expr = "expr" in omics_list
    level = level or ("sample" if has_expr else "case")
    if level not in {"case", "sample"}:
        raise ValueError("level must be 'case' or 'sample'")
    if level == "sample" and not has_expr:
        console.print("[yellow]level=sample needs expr as the per-sample base; falling back to level=case.[/yellow]")
        level = "case"

    sample_types = [s.strip() for s in sample_type.split(",")] if sample_type else None
    cohort_filters = cohort_filters or []
    if not project and not cohort_filters and not paired:
        console.print("[yellow]No --project/cohort filter and no --paired: this spans all of GDC and may download everything.[/yellow]")

    work = work_dir or (out.parent / f"{out.stem}_work")
    work.mkdir(parents=True, exist_ok=True)
    base_file_filters = ([f"cases.project.project_id in {project}"] if project else []) + cohort_filters
    extra_filters = {"cna": [f"analysis.workflow_type in {cnv_workflow}"]}
    client = GDCClient()

    # 1) Search each omics' file metadata (no download yet); apply the sample policy;
    #    collect its case ids.
    metas: dict[str, pd.DataFrame] = {}
    case_by_omics: dict[str, set[str]] = {}
    for name in omics_list:
        spec = OMICS[name]
        extra = spec.get("extra", []) + extra_filters.get(name, [])
        meta_path = work / f"{name}_meta.tsv"
        console.print(f"[cyan]Searching {name} files...[/cyan]")
        search_metadata(
            endpoint="files",
            filter_expressions=base_file_filters + extra,
            alias_names=spec["aliases"],
            fields=spec["fields"],
            size=size,
            out=meta_path,
            client=client,
            console=console,
        )
        frame = pd.read_csv(meta_path, sep="\t", dtype=str)
        frame = _filter_samples(frame, tumor_only, sample_types)
        metas[name] = frame
        case_by_omics[name] = set(frame.get("cases.case_id", pd.Series(dtype=str)).dropna())

    # 2) Target case set: intersection (paired) or union.
    case_sets = list(case_by_omics.values())
    target = set.intersection(*case_sets) if paired else set.union(*case_sets)
    if not target:
        raise ValueError("No cases satisfy the requested omics criteria.")

    # When expr is the per-sample/per-case base, only cases WITH expression survive the
    # join; union cases lacking expr are dropped from the final table (data-integrity trap).
    dropped_no_expr: set[str] = set()
    final_cases = set(target)
    if has_expr:
        final_cases = target & case_by_omics["expr"]
        dropped_no_expr = target - case_by_omics["expr"]

    # Per-omics file counts + sizes for the target cases (the dry-run preview).
    preview: dict[str, tuple[int, int]] = {}
    for name in omics_list:
        frame = metas[name]
        if "cases.case_id" in frame.columns:
            frame = frame[frame["cases.case_id"].isin(target)]
        sizes = pd.to_numeric(frame.get("file_size", pd.Series(dtype=str)), errors="coerce").fillna(0)
        preview[name] = (len(frame), int(sizes.sum()))

    final_rows = _final_row_count(metas, has_expr, level, final_cases)
    _print_cohort_preview(console, omics_list, target, paired, preview, level, final_rows, dropped_no_expr)

    provenance = _provenance_base(
        project=project,
        cohort_filters=cohort_filters,
        omics_list=omics_list,
        clinical_fields=clinical_fields,
        paired=paired,
        level=level,
        tumor_only=tumor_only,
        sample_types=sample_types,
        prefer_sample_type=prefer_sample_type,
        age_unit=age_unit,
        multi_value=multi_value,
        base_file_filters=base_file_filters,
        extra_filters=extra_filters,
        metas=metas,
        target=target,
        final_cases=final_cases,
        dropped_no_expr=dropped_no_expr,
        preview=preview,
        client=client,
    )

    if dry_run:
        prov_path = _write_provenance(out, {**provenance, "dry_run": True})
        console.print("[green]Dry run: nothing downloaded.[/green]")
        console.print(f"Wrote provenance {prov_path}")
        return None

    assemblers = {
        "expr": lambda mp, fd: assemble_expression(mp, fd, value_col=expr_value, console=console),
        "cna": lambda mp, fd: assemble_cnv(mp, fd, console=console, prefer_sample_type=prefer_sample_type),
        "mut": lambda mp, fd: assemble_mutation(mp, fd, mode=mut_mode, console=console),
        "mirna": lambda mp, fd: assemble_mirna(mp, fd, value_col=mirna_value, console=console, prefer_sample_type=prefer_sample_type),
        "methyl": lambda mp, fd: assemble_methylation(mp, fd, console=console, top_variance=methyl_top_variance, prefer_sample_type=prefer_sample_type),
        "prot": lambda mp, fd: assemble_protein(mp, fd, console=console, prefer_sample_type=prefer_sample_type),
    }

    # 3) Per omics: keep only target-case files, download, assemble (matrices as parquet).
    matrices: dict[str, Path] = {}
    for name in omics_list:
        matrix_path = work / f"{name}_matrix.parquet"
        if skip_existing and matrix_path.exists():
            console.print(f"[green]Reusing existing {name} matrix ({matrix_path.name}).[/green]")
            matrices[name] = matrix_path
            continue
        frame = metas[name]
        if "cases.case_id" in frame.columns:
            frame = frame[frame["cases.case_id"].isin(target)]
        meta_path = work / f"{name}_meta.tsv"
        frame.to_csv(meta_path, sep="\t", index=False)
        files_dir = work / f"{name}_files"
        console.print(f"[cyan]Downloading {name} files ({len(frame)})...[/cyan]")
        download_files(from_search=meta_path, mode="data", out_dir=files_dir, yes=True, client=client, console=console)
        write_frame(assemblers[name](meta_path, files_dir), matrix_path)
        matrices[name] = matrix_path

    # 4) Clinical for the target cases, queried directly by case_id (chunked).
    clinical_path = None
    if clinical_fields:
        clinical_path = work / "clinical.tsv"
        console.print("[cyan]Collecting clinical labels...[/cyan]")
        _collect_clinical(
            case_ids=final_cases,
            clinical_fields=clinical_fields,
            out_path=clinical_path,
            multi_value=multi_value,
            client=client,
            console=console,
        )

    # 5) Join into one wide table.
    console.print("[cyan]Joining into one wide table...[/cyan]")
    ml_kwargs = {OMICS[name]["ml_key"]: matrices[name] for name in omics_list}
    table = build_ml_table(
        clinical_path,
        expr_level=level,
        age_unit=age_unit,
        prefer_sample_type=prefer_sample_type,
        console=console,
        **ml_kwargs,
    )
    write_frame(table, out)

    summary = _dataset_summary(table, omics_list)
    _print_dataset_summary(console, out, summary)
    prov_path = _write_provenance(out, {**provenance, "dry_run": False, "final_table": summary})
    console.print(f"Wrote provenance {prov_path}")
    return out


def _filter_samples(
    frame: pd.DataFrame,
    tumor_only: bool,
    sample_types: list[str] | None,
) -> pd.DataFrame:
    """Apply the sample-type cohort policy to a file-metadata frame.

    `sample_types` keeps only rows whose cases.samples.sample_type matches (exact, case-
    insensitive); otherwise `tumor_only` drops rows whose sample type contains 'normal'.
    Frames without the sample_type column (e.g. per-case masked mutation) pass through."""
    col = "cases.samples.sample_type"
    if col not in frame.columns:
        return frame
    stype = frame[col].fillna("").str.lower()
    if sample_types:
        wanted = {s.lower() for s in sample_types}
        return frame[stype.isin(wanted)]
    if tumor_only:
        return frame[~stype.str.contains("normal")]
    return frame


def _final_row_count(
    metas: dict[str, pd.DataFrame],
    has_expr: bool,
    level: str,
    final_cases: set[str],
) -> int:
    """The true number of rows the final table will have (what the user actually gets)."""
    if not has_expr:
        return len(final_cases)
    expr = metas["expr"]
    if "cases.case_id" in expr.columns:
        expr = expr[expr["cases.case_id"].isin(final_cases)]
    if level == "case":
        return int(expr.get("cases.case_id", pd.Series(dtype=str)).nunique())
    return len(expr)


def _collect_clinical(
    case_ids: set[str],
    clinical_fields: str,
    out_path: Path,
    multi_value: str,
    client: GDCClient,
    console: Console,
    chunk_size: int = 400,
) -> None:
    """Query clinical fields for exactly the target cases, by case_id in chunks.

    Querying by case_id (rather than re-searching every case in the cohort's projects and
    filtering afterwards) avoids pulling thousands of irrelevant cases; case_id `in` lists
    are chunked to keep each request small."""
    ids = sorted(case_ids)
    frames: list[pd.DataFrame] = []
    for start in range(0, len(ids), chunk_size):
        chunk = ids[start : start + chunk_size]
        chunk_path = out_path.parent / "_clinical_chunk.tsv"
        search_metadata(
            endpoint="cases",
            filter_expressions=[f"case_id in {','.join(chunk)}"],
            fields="case," + clinical_fields,
            size="all",
            out=chunk_path,
            multi_value=multi_value,
            client=client,
            console=console,
        )
        frames.append(pd.read_csv(chunk_path, sep="\t", dtype=str))
        chunk_path.unlink(missing_ok=True)
    clin = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    clin.to_csv(out_path, sep="\t", index=False)


def _print_cohort_preview(
    console: Console,
    omics_list: list[str],
    target: set[str],
    paired: bool,
    preview: dict[str, tuple[int, int]],
    level: str,
    final_rows: int,
    dropped_no_expr: set[str],
) -> None:
    label = "Paired cases (all omics)" if paired else "Cohort cases (any omics)"
    console.print(f"[green]{label}: {len(target)}[/green]")
    console.print(f"[green]Final table rows ({level}-level): {final_rows}[/green]")
    if dropped_no_expr:
        console.print(
            f"[yellow]{len(dropped_no_expr)} case(s) have other omics but no expression; "
            f"they cannot appear in an expression-based table and are dropped.[/yellow]"
        )
    table = Table("omics", "files", "download size")
    grand = 0
    for name in omics_list:
        count, size = preview[name]
        grand += size
        table.add_row(name, str(count), _format_bytes(size))
    table.add_row("[bold]total", "", f"[bold]{_format_bytes(grand)}")
    console.print(table)


def _provenance_base(
    project: str | None,
    cohort_filters: list[str],
    omics_list: list[str],
    clinical_fields: str,
    paired: bool,
    level: str,
    tumor_only: bool,
    sample_types: list[str] | None,
    prefer_sample_type: str | None,
    age_unit: str,
    multi_value: str,
    base_file_filters: list[str],
    extra_filters: dict[str, list[str]],
    metas: dict[str, pd.DataFrame],
    target: set[str],
    final_cases: set[str],
    dropped_no_expr: set[str],
    preview: dict[str, tuple[int, int]],
    client: GDCClient,
) -> dict[str, object]:
    """Assemble the reproducibility record for a build_dataset run."""
    files: dict[str, list[dict[str, str]]] = {}
    for name in omics_list:
        frame = metas[name]
        if "cases.case_id" in frame.columns:
            frame = frame[frame["cases.case_id"].isin(target)]
        id_col = _col_name(frame, "file_id", "id")
        md5_col = _col_name(frame, "md5sum")
        entries = []
        for _, row in frame.iterrows():
            entry = {"file_id": str(row[id_col])} if id_col else {}
            if md5_col and pd.notna(row.get(md5_col)):
                entry["md5"] = str(row[md5_col])
            entries.append(entry)
        files[name] = entries
    return {
        "tool": "gdc build-dataset",
        "version": __version__,
        "timestamp": datetime.now().astimezone().isoformat(),
        "gdc_api_base": client.base_url,
        "command": " ".join(sys.argv),
        "params": {
            "project": project,
            "omics": omics_list,
            "clinical_fields": [f for f in clinical_fields.split(",") if f] if clinical_fields else [],
            "paired": paired,
            "level": level,
            "tumor_only": tumor_only,
            "sample_types": sample_types,
            "prefer_sample_type": prefer_sample_type,
            "age_unit": age_unit,
            "multi_value": multi_value,
        },
        "filters": {
            "base_file_filters": base_file_filters,
            "extra_filters": extra_filters,
            "alias_expansions": {name: OMICS[name]["aliases"] for name in omics_list},
        },
        "counts": {
            "cohort_cases": len(target),
            "final_cases": len(final_cases),
            "dropped_no_expr": len(dropped_no_expr),
            "files_per_omics": {name: preview[name][0] for name in omics_list},
        },
        "files": files,
    }


def _write_provenance(out: Path, provenance: dict[str, object]) -> Path:
    prov_path = out.parent / f"{out.stem}_provenance.json"
    prov_path.parent.mkdir(parents=True, exist_ok=True)
    prov_path.write_text(json.dumps(provenance, indent=2, default=str), encoding="utf-8")
    return prov_path


def _dataset_summary(table: pd.DataFrame, omics_list: list[str]) -> dict[str, object]:
    prefixes = {"expr": "expr_", "mut": "mut_", "cna": "cna_", "mirna": "mirna_", "methyl": "methyl_", "prot": "prot_"}
    omics_cols = {name: int(sum(c.startswith(prefixes[name]) for c in table.columns)) for name in omics_list}
    non_omics_prefixes = tuple(prefixes.values())
    clinical_cols = [c for c in table.columns if not c.startswith(non_omics_prefixes) and c != "case_id"]
    completeness = {}
    if len(table):
        for col in clinical_cols:
            completeness[col] = round(float(table[col].notna().mean()), 3)
    return {
        "rows": int(table.shape[0]),
        "columns": int(table.shape[1]),
        "omics_columns": omics_cols,
        "clinical_completeness": completeness,
    }


def _print_dataset_summary(console: Console, out: Path, summary: dict[str, object]) -> None:
    console.print(
        f"[bold green]Dataset: {summary['rows']} rows x {summary['columns']} columns[/bold green]"
    )
    table = Table("omics", "columns")
    for name, count in summary["omics_columns"].items():  # type: ignore[union-attr]
        table.add_row(name, str(count))
    console.print(table)
    completeness = summary.get("clinical_completeness") or {}
    if completeness:
        ctable = Table("clinical field", "completeness")
        for col, frac in completeness.items():  # type: ignore[union-attr]
            ctable.add_row(col, f"{frac:.0%}")
        console.print(ctable)


def _col_name(frame: pd.DataFrame, *candidates: str) -> str | None:
    for name in candidates:
        if name in frame.columns:
            return name
    return None


def search_metadata(
    endpoint: str,
    filter_expressions: list[str] | None = None,
    alias_names: list[str] | None = None,
    fields: str | list[str] | None = None,
    output_format: str = "tsv",
    size: str = "100",
    sort: str | None = None,
    out: Path | None = None,
    combine_op: str = "and",
    filter_json: str | None = None,
    multi_value: str = "join",
    verbose: bool = False,
    client: GDCClient | None = None,
    schema: SchemaCache | None = None,
    console: Console | None = None,
) -> Path:
    if output_format not in {"tsv", "json"}:
        raise ValueError("format must be tsv or json")
    client = client or GDCClient()
    schema = schema or SchemaCache(client=client)
    console = console or Console()
    aliases = load_aliases()

    selected_fields = expand_field_list(fields, aliases)
    clauses, warnings = build_clauses(filter_expressions, alias_names, aliases)
    for warning in warnings:
        console.print(f"[yellow]{warning}[/yellow]")
    if filter_json:
        clauses.append(parse_filter_json(filter_json))
    filter_payload = build_filter(clauses, combine_op=combine_op)
    fields_to_validate = selected_fields + filter_fields(filter_payload)
    invalid = schema.validate_fields(endpoint, fields_to_validate)
    if invalid:
        raise ValueError(_format_invalid_fields(invalid))

    payload: dict[str, object] = {}
    if selected_fields:
        payload["fields"] = ",".join(selected_fields)
    if filter_payload:
        payload["filters"] = filter_payload
    if sort:
        payload["sort"] = sort
    if verbose:
        console.print_json(data=payload)

    max_records = None if size == "all" else int(size)
    page_size = 1000 if max_records is None else max(1, min(1000, max_records))
    records = client.paginate(endpoint, payload, page_size=page_size, max_records=max_records)
    out_path = out or _default_out_path(endpoint, output_format)
    write_records(records, out_path, output_format, selected_fields=selected_fields, multi_value=multi_value)
    return out_path


def write_records(
    records: list[dict[str, object]],
    out_path: Path,
    output_format: str,
    selected_fields: list[str] | None = None,
    multi_value: str = "join",
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        out_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
        return
    if selected_fields:
        frame = pd.DataFrame([flatten_record(record, selected_fields, multi_value) for record in records])
    else:
        frame = pd.json_normalize(records, sep=".")
    frame.to_csv(out_path, sep="\t", index=False)


def flatten_record(
    record: dict[str, object],
    selected_fields: list[str],
    multi_value: str = "join",
) -> dict[str, object]:
    row: dict[str, object] = {}
    if "id" in record:
        row["id"] = record["id"]
    for field in selected_fields:
        row[field] = _stringify_cell(_extract_field(record, field), multi_value)
    return row


def _extract_field(value: object, field: str) -> object:
    if isinstance(value, dict) and field in value:
        return value[field]
    parts = field.split(".")
    return _extract_parts(value, parts)


def _extract_parts(value: object, parts: list[str]) -> object:
    if not parts:
        return value
    if isinstance(value, list):
        return [_extract_parts(item, parts) for item in value]
    if isinstance(value, dict):
        remaining = ".".join(parts)
        if remaining in value:
            return value[remaining]
        return _extract_parts(value.get(parts[0]), parts[1:])
    return None


def _stringify_cell(value: object, multi_value: str = "join") -> object:
    """Render a possibly-multivalued GDC field to a single cell.

    A case can carry several diagnoses/treatments; `multi_value` picks the policy:
    "join" concatenates the distinct values with ';' (default), "first"/"last" keep a
    single value (first ~ primary diagnosis; last ~ most recent record).
    """
    if isinstance(value, list):
        flattened = []
        for item in value:
            if isinstance(item, list):
                flattened.extend(item)
            else:
                flattened.append(item)
        clean = list(dict.fromkeys(str(item) for item in flattened if item not in (None, "")))
        if not clean:
            return None
        if multi_value == "first":
            return clean[0]
        if multi_value == "last":
            return clean[-1]
        if multi_value == "join":
            return ";".join(clean)
        raise ValueError("multi_value must be join, first, or last")
    return value


def facet_buckets(response: dict[str, object], field: str) -> list[dict[str, object]]:
    data = response.get("data", {}) if isinstance(response, dict) else {}
    aggregations = data.get("aggregations", {}) if isinstance(data, dict) else {}
    if not isinstance(aggregations, dict):
        return []
    candidates = [field, field.replace(".", "_"), field.replace(".", "__")]
    for key in candidates:
        aggregate = aggregations.get(key)
        if isinstance(aggregate, dict) and isinstance(aggregate.get("buckets"), list):
            return aggregate["buckets"]
    for aggregate in aggregations.values():
        if isinstance(aggregate, dict) and isinstance(aggregate.get("buckets"), list):
            return aggregate["buckets"]
    return []


def alias_flags_from_extra(args: list[str]) -> list[str]:
    aliases: list[str] = []
    for arg in args:
        if not arg.startswith("--"):
            raise typer.BadParameter(f"Unexpected extra argument: {arg}")
        aliases.append(arg[2:].replace("-", "_"))
    return aliases


def _default_out_path(endpoint: str, output_format: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path("out") / f"{endpoint}_{timestamp}.{output_format}"


def _format_invalid_fields(invalid: list[tuple[str, list[str]]]) -> str:
    parts = []
    for field, suggestions in invalid:
        if suggestions:
            parts.append(f"{field} (did you mean: {', '.join(suggestions)})")
        else:
            parts.append(field)
    return "Invalid field(s): " + "; ".join(parts)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
