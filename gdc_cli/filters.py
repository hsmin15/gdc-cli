from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

DEFAULT_ALIAS_PATH = Path(__file__).with_name("aliases.yaml")
OPERATORS = [
    "excludeifany",
    "not in",
    "exclude",
    "!=",
    "<=",
    ">=",
    "in",
    "is",
    "=",
    "<",
    ">",
]
OPERATOR_PATTERN = "|".join(re.escape(op).replace("\\ ", r"\s+") for op in OPERATORS)


class FilterError(ValueError):
    pass


# `<field> is missing`, `<field> not missing`, `<field> is not missing` — GDC's
# null-presence operators, which take no value after the operator.
MISSING_PATTERN = re.compile(
    r"^\s*(?P<field>\S+)\s+(?P<op>is\s+not|is|not)\s+missing\s*$",
    flags=re.IGNORECASE,
)


def load_aliases(path: Path | None = None) -> dict[str, Any]:
    alias_path = path or DEFAULT_ALIAS_PATH
    with alias_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    data.setdefault("fields", {})
    data.setdefault("filters", {})
    return data


def expand_field_list(fields: str | list[str] | None, aliases: dict[str, Any]) -> list[str]:
    if not fields:
        return []
    names = fields if isinstance(fields, list) else [part.strip() for part in fields.split(",")]
    expanded: list[str] = []
    for name in names:
        if not name:
            continue
        expanded.extend(aliases.get("fields", {}).get(name, [name]))
    return list(dict.fromkeys(expanded))


def parse_filter_expression(expression: str) -> dict[str, Any]:
    missing = MISSING_PATTERN.match(expression)
    if missing:
        # GDC: "is MISSING" => field is null; "not MISSING" => field is present.
        op = re.sub(r"\s+", " ", missing.group("op").lower())
        gdc_op = "is" if op == "is" else "not"
        return {"op": gdc_op, "content": {"field": missing.group("field"), "value": "MISSING"}}

    match = re.match(
        rf"^\s*(?P<field>\S+)\s+(?P<op>{OPERATOR_PATTERN})\s+(?P<value>.+?)\s*$",
        expression,
        flags=re.IGNORECASE,
    )
    if not match:
        raise FilterError(f"Invalid filter expression: {expression}")
    op = re.sub(r"\s+", " ", match.group("op").lower())
    value = _parse_value(match.group("value"))
    return {"op": op, "content": {"field": match.group("field"), "value": value}}


def parse_filter_json(raw: str) -> dict[str, Any]:
    """Parse a raw GDC filter object (the JSON accepted by --filter-json).

    Lets callers express nested AND/OR that the flat `field op value` expression
    grammar can't reach, e.g. {"op":"or","content":[{...},{...}]}.
    """
    try:
        clause = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FilterError(f"Invalid --filter-json (not valid JSON): {exc}") from exc
    if not isinstance(clause, dict) or "op" not in clause or "content" not in clause:
        raise FilterError('--filter-json must be a GDC filter object with "op" and "content" keys.')
    return clause


def alias_filter(name: str, aliases: dict[str, Any]) -> dict[str, Any]:
    spec = aliases.get("filters", {}).get(name)
    if not spec:
        raise FilterError(f"Unknown filter alias: {name}")
    value = spec.get("value")
    if not isinstance(value, list):
        value = [value]
    return {
        "op": str(spec.get("op", "=")).lower(),
        "content": {"field": spec["field"], "value": value},
    }


def build_filter(
    clauses: list[dict[str, Any]],
    combine_op: str = "and",
) -> dict[str, Any] | None:
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    if combine_op not in {"and", "or"}:
        raise FilterError(f"Unsupported combine operator: {combine_op}")
    return {"op": combine_op, "content": clauses}


def build_clauses(
    filter_expressions: list[str] | None,
    alias_names: list[str] | None,
    aliases: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    clauses: list[dict[str, Any]] = []
    warnings: list[str] = []

    for alias_name in alias_names or []:
        clause = alias_filter(alias_name, aliases)
        if _is_homo_sapiens_clause(clause):
            warnings.append("Ignored homo sapiens species filter; GDC cohorts are human.")
            continue
        clauses.append(clause)

    for expression in filter_expressions or []:
        clause = parse_filter_expression(expression)
        if _is_homo_sapiens_clause(clause):
            warnings.append("Ignored homo sapiens species filter; GDC cohorts are human.")
            continue
        clauses.append(clause)

    return clauses, warnings


def filter_fields(filter_json: dict[str, Any] | None) -> list[str]:
    if not filter_json:
        return []
    content = filter_json.get("content")
    if isinstance(content, list):
        fields: list[str] = []
        for clause in content:
            fields.extend(filter_fields(clause))
        return fields
    if isinstance(content, dict) and "field" in content:
        return [content["field"]]
    return []


def _parse_value(raw_value: str) -> list[Any]:
    parts = _split_csv(raw_value.strip())
    return [_coerce_value(part.strip()) for part in parts if part.strip() != ""]


def _split_csv(raw: str) -> list[str]:
    """Split a comma-separated value list, honouring single/double quotes.

    Quoting lets a single value contain a comma, e.g.
    ``diagnoses.primary_diagnosis in "Adenocarcinoma, NOS","Squamous cell carcinoma, NOS"``
    yields two values rather than four. Quote characters are consumed.
    """
    parts: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    for char in raw:
        if quote is not None:
            if char == quote:
                quote = None
            else:
                buffer.append(char)
        elif char in "\"'":
            quote = char
        elif char == ",":
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    parts.append("".join(buffer))
    return parts


def _coerce_value(value: str) -> Any:
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def _is_homo_sapiens_clause(clause: dict[str, Any]) -> bool:
    content = clause.get("content", {})
    values = content.get("value", [])
    if not isinstance(values, list):
        values = [values]
    return any(str(value).lower() == "homo sapiens" for value in values)
