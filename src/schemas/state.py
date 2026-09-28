"""Checkpoint-safe state contracts for SVC-C2-106."""

from __future__ import annotations

from typing import NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState, total=False):
    mode: str
    case_ref: str
    customer_ref: str
    validated_input: str
    payload_ref: str
    policy_edition_ref: str
    evidence_declarations: list[dict]
    docs_present: dict
    format_check: dict
    recall_refs: list[dict]
    retrieved_records: list[dict]
    source_registry: dict[str, dict]
    policy_clauses: list[dict]
    cited_source_ids: list[str]
    evidence_brief: dict
    evidence_map: list[dict]
    requires_human_review: bool
    degraded: bool
    degradation_code: str
    error_code: str
    error_type: str
    ingest_summary: dict
    feedback_summary: dict


class EngagementWorkflowState(AgentState, total=False):
    """Inner graph schema.  It deliberately contains only JSON-safe values."""

    mode: str
    case_ref: str
    customer_ref: str
    payload_ref: NotRequired[str]
    customer_id: str
    policy_edition_ref: NotRequired[str]
    evidence_declarations: NotRequired[list[dict]]
    records: NotRequired[list[dict]]
    feedback: NotRequired[dict]
    docs_present: NotRequired[dict]
    format_check: NotRequired[dict]
    recall_refs: NotRequired[list[dict]]
    retrieved_records: NotRequired[list[dict]]
    source_registry: NotRequired[dict[str, dict]]
    policy_clauses: NotRequired[list[dict]]
    evidence_brief: NotRequired[dict]
    evidence_map: NotRequired[list[dict]]
    cited_source_ids: NotRequired[list[str]]
    requires_human_review: NotRequired[bool]
    degraded: NotRequired[bool]
    degradation_code: NotRequired[str]
    error_code: NotRequired[str]
    error_type: NotRequired[str]
    ingest_summary: NotRequired[dict]
    feedback_summary: NotRequired[dict]
