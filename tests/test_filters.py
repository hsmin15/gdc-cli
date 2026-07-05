"""Offline unit tests for the pure filter/alias logic (no network)."""
from __future__ import annotations

import pytest

from gdc_cli.filters import (
    FilterError,
    build_filter,
    expand_field_list,
    filter_fields,
    load_aliases,
    parse_filter_expression,
    parse_filter_json,
)


def test_simple_equality_expression():
    clause = parse_filter_expression("project.project_id = TCGA-LUAD")
    assert clause == {"op": "=", "content": {"field": "project.project_id", "value": ["TCGA-LUAD"]}}


def test_in_operator_splits_on_comma():
    clause = parse_filter_expression("cases.project.project_id in TCGA-LUAD,TCGA-LUSC")
    assert clause["op"] == "in"
    assert clause["content"]["value"] == ["TCGA-LUAD", "TCGA-LUSC"]


def test_quoted_value_with_internal_comma_stays_one_value():
    clause = parse_filter_expression(
        'diagnoses.primary_diagnosis in "Adenocarcinoma, NOS","Squamous cell carcinoma, NOS"'
    )
    assert clause["content"]["value"] == [
        "Adenocarcinoma, NOS",
        "Squamous cell carcinoma, NOS",
    ]


def test_is_missing_and_not_missing():
    assert parse_filter_expression("stage is missing") == {
        "op": "is",
        "content": {"field": "stage", "value": "MISSING"},
    }
    assert parse_filter_expression("stage not missing") == {
        "op": "not",
        "content": {"field": "stage", "value": "MISSING"},
    }


def test_numeric_coercion():
    clause = parse_filter_expression("diagnoses.age_at_diagnosis >= 18250")
    assert clause["content"]["value"] == [18250]


def test_invalid_expression_raises():
    with pytest.raises(FilterError):
        parse_filter_expression("this has no operator")


def test_build_filter_combines_with_and():
    a = parse_filter_expression("a = 1")
    b = parse_filter_expression("b = 2")
    combined = build_filter([a, b], combine_op="and")
    assert combined["op"] == "and"
    assert len(combined["content"]) == 2
    assert build_filter([a]) == a
    assert build_filter([]) is None


def test_filter_json_roundtrip_and_validation():
    raw = '{"op":"or","content":[{"op":"=","content":{"field":"a","value":["1"]}}]}'
    assert parse_filter_json(raw)["op"] == "or"
    with pytest.raises(FilterError):
        parse_filter_json("{not valid json")
    with pytest.raises(FilterError):
        parse_filter_json('{"op":"or"}')  # missing "content"


def test_filter_fields_extracts_nested_field_paths():
    nested = {
        "op": "and",
        "content": [
            {"op": "=", "content": {"field": "a", "value": ["1"]}},
            {"op": "in", "content": {"field": "b.c", "value": ["2"]}},
        ],
    }
    assert sorted(filter_fields(nested)) == ["a", "b.c"]


def test_alias_field_expansion():
    aliases = load_aliases()
    assert expand_field_list("case", aliases) == ["case_id"]
    # OS alias expands to the three survival columns
    expanded = expand_field_list("OS", aliases)
    assert "demographic.vital_status" in expanded
    # unknown names pass through unchanged; duplicates are de-duped
    assert expand_field_list("case,case,unknown_field", aliases) == ["case_id", "unknown_field"]
