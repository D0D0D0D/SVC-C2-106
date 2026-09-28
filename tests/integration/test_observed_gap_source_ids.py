"""Replay the two source-id variants observed from production-shaped LLM calls."""

from __future__ import annotations

import json
import importlib
from pathlib import Path

import pytest

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
import src.graph.domain_workflow_graph as workflow_module
from src.graph.domain_workflow_graph import EngagementWorkflowGraph
from src.graph.graph import EngagementAcceptanceEvidenceGraph


_FIXTURES = Path(__file__).parents[1] / "fixtures"
response_contract_module = importlib.import_module("src.services.response_contract")


class ReplayLlm:
    def __init__(self, fixture_name: str) -> None:
        self._content = (_FIXTURES / fixture_name).read_text(encoding="utf-8")

    def complete(self, _messages: list[dict]) -> dict[str, str]:
        return {"content": self._content}


def _invoke(agent_config: dict, case_payload: dict, fixture_name: str) -> dict:
    graph = EngagementAcceptanceEvidenceGraph(
        {**agent_config, "llm": ReplayLlm(fixture_name)}
    )
    graph.compile()
    return graph.invoke(
        json.dumps(case_payload),
        ctx=InvocationContext(
            session_id="live-1",
            thread_id="live-1",
            caller_id="live-check",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        ),
    )


def _invoke_inner(agent_config: dict, case_payload: dict, fixture_name: str) -> dict:
    graph = EngagementWorkflowGraph(
        {**agent_config, "llm": ReplayLlm(fixture_name)}
    )
    graph.compile()
    return graph.invoke(
        json.dumps(case_payload),
        ctx=InvocationContext(
            session_id="live-1",
            thread_id="live-1",
            caller_id="live-check",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        ),
    )


@pytest.mark.parametrize(
    "fixture_name",
    [
        "observed_gpt_5_5_case_narrative_gap.json",
        "observed_gpt_4_1_empty_gap.json",
    ],
)
def test_observed_evidence_absent_gap_reaches_gate_and_succeeds(
    agent_config: dict, case_payload: dict, fixture_name: str
) -> None:
    result = _invoke(agent_config, case_payload, fixture_name)

    assert result["status"] == "success"
    gaps = result["output"]["gaps"]
    assert any(
        gap["clause_id"] == "client-identity" and gap["source_ids"] == []
        for gap in gaps
    )


def test_unknown_only_candidate_is_dropped_and_requires_human_review(
    agent_config: dict, case_payload: dict
) -> None:
    result = _invoke_inner(
        agent_config, case_payload, "unknown_only_candidate_conflict.json"
    )

    assert result["status"] == "success"
    assert result["evidence_brief"]["candidate_conflicts"] == []
    assert result["requires_human_review"] is True
    assert result["degraded"] is True
    assert result["degradation_code"] == "finding_dropped_unknown_source"
    assert set(result["cited_source_ids"]) <= {
        "conflict-search-aug",
        "independence-declaration-aug",
    }


def test_mutation_without_source_normalization_reproduces_observed_gate_error(
    agent_config: dict, case_payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workflow_module,
        "normalize_response_source_ids",
        lambda response, _registered: (response, False),
    )

    result = _invoke(
        agent_config,
        case_payload,
        "observed_gpt_5_5_case_narrative_gap.json",
    )

    assert result["status"] == "error"
    assert result["error_code"] == "workflow_failed"


def test_mutation_with_old_all_findings_minimum_reproduces_empty_gap_error(
    agent_config: dict, case_payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        response_contract_module,
        "SECTIONS_REQUIRING_SOURCE_IDS",
        frozenset(response_contract_module.FINDING_FIELDS_BY_SECTION),
    )

    result = _invoke(
        agent_config,
        case_payload,
        "observed_gpt_4_1_empty_gap.json",
    )

    assert result["status"] == "error"
    assert result["error_code"] == "workflow_failed"
