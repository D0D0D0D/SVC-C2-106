"""The prompt and S-3 projector share one executable response contract."""

from __future__ import annotations

import json

import pytest

from framework.errors import SecurityViolationError
from src.graph.domain_workflow_graph import _build_prompt
from src.services import project_response, response_contract


def _value_from_contract(name: str, contract: dict) -> object:
    if "enum" in contract:
        return contract["enum"][0]
    if contract["type"] == "string":
        return {
            "description": "candidate evidence gap",
            "question": "Which evidence resolves this gap?",
            "clause_id": "clausea",
        }[name]
    if contract["type"] == "number":
        return 0.5
    if contract["type"] == "array":
        return ["sourcea"]
    raise AssertionError(f"unsupported generated contract type: {contract['type']}")


def _response_from_contract(contract: dict) -> dict:
    response = {}
    for section, section_contract in contract["properties"].items():
        properties = section_contract["items"]["properties"]
        response[section] = [
            {name: _value_from_contract(name, field) for name, field in properties.items()}
        ]
    return response


def test_prompt_contract_mechanically_builds_a_response_accepted_by_projector():
    contract = response_contract()
    generated = _response_from_contract(contract)

    projected = project_response(generated)
    prompt = _build_prompt(
        {
            "policy_clauses": [],
            "retrieved_records": [
                {"source_id": "sourceb"},
                {"source_id": "sourcea"},
            ],
            "recall_refs": [],
        },
        {"case_narrative": "ordinary case"},
    )

    assert projected["candidate_conflicts"][0]["severity"] == "candidate"
    assert json.dumps(contract, ensure_ascii=False, sort_keys=True) in prompt
    assert 'Permitted source_ids values: ["sourcea", "sourceb"]' in prompt
    assert "Never put case_narrative or any other string in source_ids" in prompt
    assert "source_ids: [] and a registered clause_id" in prompt


def test_only_candidate_conflicts_require_a_non_empty_source_list():
    contract = response_contract()
    properties = contract["properties"]

    assert properties["candidate_conflicts"]["items"]["properties"]["source_ids"]["minItems"] == 1
    assert "minItems" not in properties["gaps"]["items"]["properties"]["source_ids"]
    assert "minItems" not in properties["verification_questions"]["items"]["properties"]["source_ids"]

    generated = _response_from_contract(contract)
    generated["gaps"][0]["source_ids"] = []
    generated["verification_questions"][0]["source_ids"] = []
    assert project_response(generated)["gaps"][0]["source_ids"] == []

    generated["candidate_conflicts"][0]["source_ids"] = []
    with pytest.raises(SecurityViolationError, match="requires at least one source_id"):
        project_response(generated)


def test_typed_projection_rejects_nested_raw_text_in_declared_string_field():
    response = _response_from_contract(response_contract())
    sensitive = "raw-document-sentinel"
    response["candidate_conflicts"][0]["description"] = {"raw": sensitive}

    with pytest.raises(SecurityViolationError) as exc_info:
        project_response(response)

    assert sensitive not in str(exc_info.value)


def test_typed_projection_rejects_undeclared_key_without_echoing_it():
    response = _response_from_contract(response_contract())
    sensitive_key = "raw-document-sentinel"
    response["gaps"][0][sensitive_key] = "private"

    with pytest.raises(SecurityViolationError) as exc_info:
        project_response(response)

    assert sensitive_key not in str(exc_info.value)
