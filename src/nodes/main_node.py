"""Cat 2 main-slot adapter for the inner engagement workflow."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.errors import SubgraphError
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.cat2_graph_skeleton import DomainWorkflowGraphNode, ModeSpec
from src.graph.domain_workflow_graph import EngagementWorkflowGraph


class EngagementWorkflowGraphNode(DomainWorkflowGraphNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "handle"
    propagate_hitl: ClassVar[bool] = False
    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "invoke": ModeSpec("engagement_acceptance_evidence", {
            "evidence_brief": "evidence_brief", "source_registry": "source_registry", "policy_clauses": "policy_clauses",
            "cited_source_ids": "cited_source_ids", "retrieved_records": "retrieved_records",
            "requires_human_review": "requires_human_review", "degraded": "degraded", "degradation_code": "degradation_code",
        }),
        "ingest": ModeSpec("engagement_acceptance_evidence", {"ingest_summary": "ingest_summary"}),
        "feedback": ModeSpec("engagement_acceptance_evidence", {"feedback_summary": "feedback_summary"}),
    }

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        adapted = dict(config or {})
        adapted["memory_enabled"] = False
        adapted["hitl"] = {"enabled": False}
        super().__init__(config=adapted, subgraph_factory=lambda inner_config: EngagementWorkflowGraph(inner_config))

    def execute(self, state: AgentState) -> dict:
        emit_trace_event("engagement_workflow_started", {"mode": state.get("mode", "")}, state)
        # A malformed direct node call must terminate cleanly before GraphNode
        # attempts to construct an InvocationContext.  Normal graph calls have
        # these fields from initialize/pre_process.
        if not state.get("mode") or not all(state.get(key) for key in ("correlation_id", "session_id", "trace_id", "thread_id")):
            return {"status": "error", "error_log": ["workflow_input_missing"]}
        return super().execute(state)

    def on_subgraph_error(self, state: AgentState, error: Exception) -> dict:
        """Project inner failures to stable codes without exception text."""

        safe_codes = (
            "llm_call_failed",
            "llm_output_parse_failed",
            "llm_not_configured",
            "policy_edition_invalid",
            "payload_ref_unknown",
            "payload_ref_expired",
            "payload_ref_scope_mismatch",
            "payload_ref_replayed",
        )
        messages = error.error_log if isinstance(error, SubgraphError) else []
        error_code = next(
            (code for code in safe_codes if any(code in message for message in messages)),
            "workflow_failed",
        )
        emit_trace_event("engagement_workflow_failed", {"error_code": error_code}, state)
        return {
            "status": AgentStatus.ERROR.value,
            "error_code": error_code,
            "error_log": [error_code],
        }


# Compatibility name retained for generated scaffold consumers.
MainNode = EngagementWorkflowGraphNode
