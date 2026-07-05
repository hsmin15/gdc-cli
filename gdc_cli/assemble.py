from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

PARQUET_SUFFIXES = {".parquet", ".pq"}

# STAR gene-counts summary rows that precede the real genes.
STAR_SUMMARY_ROWS = {"N_unmapped", "N_multimapping", "N_noFeature", "N_ambiguous"}
# MAF gene symbols that are not real genes.
MAF_NON_GENES = {"Unknown", ".", ""}
# Days in a year used to convert GDC day-unit ages to years.
DAYS_PER_YEAR = 365.25
# Clinical columns GDC records in days; converted to years under age_unit="years".
# Matched by substring so aliased/suffixed variants (e.g. "..._clinical") also convert.
AGE_DAY_FIELDS = (
    "age_at_diagnosis",
    "days_to_birth",
)


def _convert_age_units(frame: pd.DataFrame, age_unit: str) -> pd.DataFrame:
    """Convert GDC day-unit age columns to years (in place) when age_unit=='years'.

    GDC reports age_at_diagnosis / days_to_birth in days (e.g. 18250), which is a trap
    for a model expecting years. Converted columns are divided by 365.25, rounded to 2
    decimals, and renamed with a '_years' suffix so the unit is explicit; days_to_birth
    (negative) is made positive so it reads as an age.
    """
    if age_unit == "days":
        return frame
    if age_unit != "years":
        raise ValueError("age_unit must be 'days' or 'years'")
    renames: dict[str, str] = {}
    for col in list(frame.columns):
        if any(token in col for token in AGE_DAY_FIELDS):
            years = pd.to_numeric(frame[col], errors="coerce").abs() / DAYS_PER_YEAR
            frame[col] = years.round(2)
            renames[col] = f"{col}_years"
    return frame.rename(columns=renames)


def write_frame(frame: pd.DataFrame, out: Path) -> Path:
    """Write a table as parquet when the path ends in .parquet/.pq, else TSV.

    Parquet is the better default for the wide omics matrices (tens of thousands of
    gene columns): it is columnar, typed, and far smaller/faster than TSV.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() in PARQUET_SUFFIXES:
        frame.to_parquet(out, index=False)
    else:
        frame.to_csv(out, sep="\t", index=False)
    return out


def read_frame(path: Path) -> pd.DataFrame:
    """Read a table written by write_frame (parquet or TSV), auto-detected by suffix.

    Parquet is read via pyarrow with raised thrift limits: methylation matrices can have
    ~485k probe columns, and the parquet footer metadata for that many columns overflows
    pyarrow's default deserialization caps.
    """
    if Path(path).suffix.lower() in PARQUET_SUFFIXES:
        import pyarrow.parquet as pq

        table = pq.read_table(
            path,
            thrift_string_size_limit=1_000_000_000,
            thrift_container_size_limit=1_000_000_000,
        )
        return table.to_pandas()
    return pd.read_csv(path, sep="\t")


def _assembly_progress(console: Console, description: str, total: int) -> Progress:
    """A file-by-file progress bar for the assembly loops (which can iterate over
    hundreds of downloaded files each parsed into a wide matrix)."""
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    )
    progress.add_task(description, total=total)
    return progress


def _select_top_variance(matrix: pd.DataFrame, prefix: str, top_n: int | None) -> pd.DataFrame:
    """Keep only the `top_n` highest-variance value columns (by column prefix).

    Feature selection for very wide matrices (methylation arrays carry hundreds of
    thousands of probes); the low-variance probes carry little signal but dominate
    memory and file size. Identifier columns and column order are preserved.
    """
    if not top_n or top_n <= 0:
        return matrix
    value_cols = [c for c in matrix.columns if c.startswith(prefix)]
    if len(value_cols) <= top_n:
        return matrix
    variances = matrix[value_cols].var(axis=0, numeric_only=True)
    keep = set(variances.sort_values(ascending=False).head(top_n).index)
    id_cols = [c for c in matrix.columns if c not in set(value_cols)]
    return matrix[id_cols + [c for c in value_cols if c in keep]]


def _col(frame: pd.DataFrame, *candidates: str) -> str | None:
    for name in candidates:
        if name in frame.columns:
            return name
    return None


def _val(row: pd.Series, col: str | None) -> object:
    return row[col] if col else None


def _find_file(files_dir: Path, file_id: str, file_name: str) -> Path | None:
    """Locate a downloaded file. gdc-client stores it as <dir>/<file_id>/<file_name>;
    the built-in downloader stores it flat under <dir>."""
    direct = files_dir / str(file_id) / str(file_name)
    if direct.exists():
        return direct
    for candidate in files_dir.rglob(str(file_name)):
        return candidate
    id_dir = files_dir / str(file_id)
    if id_dir.is_dir():
        for candidate in id_dir.iterdir():
            if candidate.is_file():
                return candidate
    return None


def assemble_expression(
    meta_path: Path,
    files_dir: Path,
    value_col: str = "tpm_unstranded",
    gene_key: str = "gene_name",
    console: Console | None = None,
) -> pd.DataFrame:
    """Transpose per-sample STAR gene-counts files into one sample x gene matrix.

    Returns columns [case_id, sample_barcode, sample_type, expr_<gene>...],
    one row per expression file (= one sequenced sample).
    """
    return _assemble_gene_matrix(meta_path, files_dir, value_col, gene_key, "expr_", console)


def assemble_cnv(
    meta_path: Path,
    files_dir: Path,
    value_col: str = "copy_number",
    gene_key: str = "gene_name",
    console: Console | None = None,
    prefer_sample_type: str | None = None,
) -> pd.DataFrame:
    """Aggregate gene-level copy-number files into one case x gene matrix.

    Gene-level CNV is the tumor copy number, but its metadata lists the tumor/normal
    pair, so it can't align to expression on a single sample barcode. Like mutation,
    it is keyed by case_id (one tumor per case; duplicates keep first) and later joined
    onto tumor rows only. Returns columns [case_id, cna_<gene>...]. Pick a single CNV
    caller (e.g. workflow ASCAT3) upstream so there is one file per case.
    """
    matrix = _assemble_gene_matrix(meta_path, files_dir, value_col, gene_key, "cna_", console)
    return _reduce_to_case(matrix, prefer_sample_type)


def assemble_mirna(
    meta_path: Path,
    files_dir: Path,
    value_col: str = "reads_per_million_miRNA_mapped",
    gene_key: str = "miRNA_ID",
    console: Console | None = None,
    prefer_sample_type: str | None = None,
) -> pd.DataFrame:
    """Aggregate per-sample miRNA quantification files into one case x miRNA matrix.

    Value defaults to RPM (reads_per_million_miRNA_mapped); pass value_col='read_count'
    for raw counts. Reduced to one row per case (prefer tumor). Columns [case_id, mirna_<id>...].
    """
    matrix = _assemble_gene_matrix(meta_path, files_dir, value_col, gene_key, "mirna_", console)
    return _reduce_to_case(matrix, prefer_sample_type)


def assemble_methylation(
    meta_path: Path,
    files_dir: Path,
    console: Console | None = None,
    top_variance: int | None = None,
    prefer_sample_type: str | None = None,
) -> pd.DataFrame:
    """Aggregate per-sample methylation beta-value files into one case x probe matrix.

    GDC level-3 beta files are headerless two-column TSVs (probe id, beta; 'NA' for
    missing). Reduced to one row per case (prefer tumor). Columns [case_id, methyl_<probe>...].
    Note: EPIC/450K arrays yield hundreds of thousands of probe columns — parquet output
    is strongly recommended. Pass `top_variance=N` to keep only the N highest-variance
    probes (feature selection for a manageable matrix).
    """
    matrix = _assemble_gene_matrix(
        meta_path,
        files_dir,
        value_col="beta_value",
        gene_key="probe_id",
        prefix="methyl_",
        console=console,
        column_names=["probe_id", "beta_value"],
        na_values=["NA"],
    )
    matrix = _reduce_to_case(matrix, prefer_sample_type)
    return _select_top_variance(matrix, "methyl_", top_variance)


def assemble_protein(
    meta_path: Path,
    files_dir: Path,
    value_col: str = "protein_expression",
    gene_key: str = "peptide_target",
    console: Console | None = None,
    prefer_sample_type: str | None = None,
) -> pd.DataFrame:
    """Aggregate per-sample RPPA protein files into one case x antibody matrix.

    Reduced to one row per case (prefer tumor). Columns [case_id, prot_<peptide_target>...].
    """
    matrix = _assemble_gene_matrix(meta_path, files_dir, value_col, gene_key, "prot_", console)
    return _reduce_to_case(matrix, prefer_sample_type)


def _reduce_to_case(matrix: pd.DataFrame, prefer_sample_type: str | None = None) -> pd.DataFrame:
    """Collapse a per-sample matrix to one row per case, preferring a tumor sample.

    Used for the case-keyed omics (CNV, miRNA, methylation, protein) that are later
    joined onto tumor rows. When a case has both tumor and normal samples the tumor
    one is kept; sample_barcode/sample_type are dropped so only [case_id, <values>...]
    remain. `prefer_sample_type` (e.g. "Primary Tumor") overrides the default tumor-first
    tie-break so an exact sample type wins when a case has several.
    """
    if "sample_type" in matrix.columns:
        stype = matrix["sample_type"].fillna("")
        if prefer_sample_type:
            rank = (~stype.str.lower().eq(prefer_sample_type.lower())).astype(int)
        else:
            rank = stype.str.lower().str.contains("normal").astype(int)
        matrix = matrix.assign(_rank=rank).sort_values("_rank", kind="stable").drop(columns="_rank")
    drop = [c for c in ("sample_barcode", "sample_type") if c in matrix.columns]
    return matrix.drop(columns=drop).drop_duplicates(subset="case_id", keep="first").reset_index(drop=True)


def _assemble_gene_matrix(
    meta_path: Path,
    files_dir: Path,
    value_col: str,
    gene_key: str,
    prefix: str,
    console: Console | None = None,
    column_names: list[str] | None = None,
    na_values: list[str] | None = None,
) -> pd.DataFrame:
    """Transpose per-sample gene-value TSV files (expression, gene-level CNV, ...)
    into one sample x gene matrix with the given column prefix.

    column_names: pass explicit names for headerless files (e.g. methylation beta
    files, which are two unlabeled columns of probe id + beta value)."""
    console = console or Console()
    meta = pd.read_csv(meta_path, sep=None, engine="python", dtype=str)
    fid_c = _col(meta, "file_id", "id")
    fname_c = _col(meta, "file_name")
    case_c = _col(meta, "cases.case_id", "case_id")
    samp_c = _col(meta, "cases.samples.submitter_id", "samples.submitter_id")
    stype_c = _col(meta, "cases.samples.sample_type", "samples.sample_type")
    if not fid_c or not fname_c:
        raise ValueError("Meta file must contain file_id and file_name columns.")

    series: dict[str, pd.Series] = {}
    rows_meta: list[dict[str, object]] = []
    label = prefix.rstrip("_") or "matrix"
    with _assembly_progress(console, f"[cyan]Assembling {label}", len(meta)) as progress:
        task = progress.tasks[0].id
        for _, row in meta.iterrows():
            progress.advance(task)
            path = _find_file(files_dir, row[fid_c], row[fname_c])
            if path is None:
                console.print(f"[yellow]Missing downloaded file, skipping: {row[fid_c]}[/yellow]")
                continue
            if column_names is not None:
                frame = pd.read_csv(
                    path, sep="\t", comment="#", header=None, names=column_names, na_values=na_values
                )
            else:
                frame = pd.read_csv(path, sep="\t", comment="#", na_values=na_values)
            if "gene_id" in frame.columns:
                frame = frame[~frame["gene_id"].isin(STAR_SUMMARY_ROWS)]
            if gene_key not in frame.columns or value_col not in frame.columns:
                console.print(f"[yellow]Unexpected columns in {path.name}, skipping.[/yellow]")
                continue
            values = pd.to_numeric(frame.set_index(gene_key)[value_col], errors="coerce")
            values = values[~values.index.isna()]
            values = values[~values.index.duplicated(keep="first")]
            series[str(row[fid_c])] = values
            rows_meta.append(
                {
                    "row_id": str(row[fid_c]),
                    "case_id": _val(row, case_c),
                    "sample_barcode": _val(row, samp_c),
                    "sample_type": _val(row, stype_c),
                }
            )

    if not series:
        raise ValueError("No gene-value files could be assembled.")

    matrix = pd.DataFrame(series).T
    matrix.columns = [f"{prefix}{name}" for name in matrix.columns]
    meta_frame = pd.DataFrame(rows_meta).set_index("row_id")
    combined = meta_frame.join(matrix).reset_index(drop=True)
    return combined


def assemble_mutation(
    meta_path: Path,
    files_dir: Path,
    mode: str = "count",
    variant_classes: list[str] | None = None,
    console: Console | None = None,
) -> pd.DataFrame:
    """Aggregate per-case masked-somatic-mutation MAF files into a case x gene matrix.

    Returns columns [case_id, mut_<gene>...]. `mode` is 'count' (mutations per gene)
    or 'binary' (0/1 mutated). Aggregation is per case (a masked MAF = the case's
    tumor mutation profile).
    """
    if mode not in {"count", "binary"}:
        raise ValueError("mode must be count or binary")
    console = console or Console()
    meta = pd.read_csv(meta_path, sep=None, engine="python", dtype=str)
    fid_c = _col(meta, "file_id", "id")
    fname_c = _col(meta, "file_name")
    case_c = _col(meta, "cases.case_id", "case_id")
    if not fid_c or not fname_c or not case_c:
        raise ValueError("Meta file must contain file_id, file_name, and case id columns.")

    need_cols = ["Hugo_Symbol"] + (["Variant_Classification"] if variant_classes else [])
    per_case: dict[str, Counter] = defaultdict(Counter)
    with _assembly_progress(console, "[cyan]Assembling mut", len(meta)) as progress:
        task = progress.tasks[0].id
        for _, row in meta.iterrows():
            progress.advance(task)
            path = _find_file(files_dir, row[fid_c], row[fname_c])
            if path is None:
                console.print(f"[yellow]Missing downloaded file, skipping: {row[fid_c]}[/yellow]")
                continue
            maf = pd.read_csv(
                path,
                sep="\t",
                comment="#",
                usecols=lambda c: c in need_cols,
                low_memory=False,
            )
            if "Hugo_Symbol" not in maf.columns:
                console.print(f"[yellow]No Hugo_Symbol column in {path.name}, skipping.[/yellow]")
                continue
            if variant_classes and "Variant_Classification" in maf.columns:
                maf = maf[maf["Variant_Classification"].isin(variant_classes)]
            genes = maf["Hugo_Symbol"].dropna()
            genes = genes[~genes.isin(MAF_NON_GENES)]
            per_case[str(row[case_c])].update(genes.tolist())

    if not per_case:
        raise ValueError("No mutation files could be assembled.")

    matrix = pd.DataFrame({case: dict(counter) for case, counter in per_case.items()}).T
    matrix = matrix.fillna(0)
    matrix = (matrix > 0).astype(int) if mode == "binary" else matrix.astype(int)
    matrix.columns = [f"mut_{name}" for name in matrix.columns]
    matrix.index.name = "case_id"
    return matrix.reset_index()


def build_ml_table(
    clinical_path: Path | None,
    expr_path: Path | None = None,
    mut_path: Path | None = None,
    cna_path: Path | None = None,
    mirna_path: Path | None = None,
    methyl_path: Path | None = None,
    protein_path: Path | None = None,
    expr_level: str = "sample",
    age_unit: str = "days",
    prefer_sample_type: str | None = None,
    console: Console | None = None,
) -> pd.DataFrame:
    """Merge clinical labels + omics matrices into one wide ML table.

    If expression is given, rows = expression samples (all samples kept) and the case-keyed
    omics (mutation/CNV/miRNA/methylation/protein) are joined by case_id but only kept on
    tumor rows (normal rows get NaN). If there is no expression, clinical is the per-case row
    base and the case-keyed omics are joined by case_id for every case.

    expr_level="case" collapses the expression base to one row per case (preferring a tumor
    sample) so the whole table is per-case; "sample" (default) keeps one row per sample.
    age_unit="years" converts day-unit age columns to years.
    """
    console = console or Console()

    def _read_clinical(path: Path) -> pd.DataFrame:
        frame = pd.read_csv(path, sep="\t")
        if "case_id" not in frame.columns and "id" in frame.columns:
            frame = frame.rename(columns={"id": "case_id"})
        elif "case_id" in frame.columns and "id" in frame.columns:
            # `id` duplicates case_id (both emitted by the cases search); drop it so the
            # final ML table has a single case identifier.
            frame = frame.drop(columns=["id"])
        if "case_id" not in frame.columns:
            raise ValueError("Clinical file must contain a case_id (or id) column.")
        return _convert_age_units(frame, age_unit)

    if expr_path:
        base = read_frame(expr_path)
        if expr_level == "case":
            base = _reduce_to_case(base, prefer_sample_type)
        if clinical_path:
            base = base.merge(_read_clinical(clinical_path), on="case_id", how="left", suffixes=("", "_clinical"))
    elif clinical_path:
        base = _read_clinical(clinical_path)
    else:
        raise ValueError("Provide expr (per-sample base) or clinical (per-case base).")

    case_keyed = (
        (mut_path, "mut_"),
        (cna_path, "cna_"),
        (mirna_path, "mirna_"),
        (methyl_path, "methyl_"),
        (protein_path, "prot_"),
    )
    for path, prefix in case_keyed:
        if not path:
            continue
        omics = read_frame(path)
        cols = [c for c in omics.columns if c.startswith(prefix)]
        base = base.merge(omics, on="case_id", how="left")
        if "sample_type" in base.columns:
            is_tumor = ~base["sample_type"].fillna("").str.lower().str.contains("normal")
            base.loc[~is_tumor, cols] = pd.NA

    return base
