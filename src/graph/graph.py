"""Outer fixed backbone for SVC-C2-106."""

from __future__ import annotations

from typing import Any

from framework.graph.agent_base_graph import AgentBaseGraph
from src.nodes.main_node import EngagementWorkflowGraphNode
from src.nodes.post_process_node import EvidenceBriefPostProcessNode
from src.nodes.pre_process_node import EngagementIntakePreProcessNode
from src.schemas.state import State
from src.services import AuthorizedResourceResolver


class EngagementAcceptanceEvidenceGraph(AgentBaseGraph):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        defaults = {"top_k": 8, "profession_profile": "audit_firm", "max_rationale_chars": 1000,
                    "memory_enabled": False, "hitl": {"enabled": False}}
        defaults.update(config or {})
        super().__init__(config=defaults)

    @property
    def name(self) -> str: return "engagement_acceptance_evidence"

    @property
    def state_schema(self) -> type: return State

    def _validate_config(self) -> None:
        super()._validate_config()
        if not isinstance(self.config.get("resource_resolver"), AuthorizedResourceResolver):
            from framework.errors import ConfigError
            raise ConfigError("resource_resolver is required")
        if self.config.get("llm") is None:
            from framework.errors import ConfigError
            raise ConfigError("llm is required")

    def register_nodes(self) -> None:
        super().register_nodes()
        self._nodes["pre_process"] = EngagementIntakePreProcessNode(resource_resolver=self.config["resource_resolver"])
        self._nodes["main"] = EngagementWorkflowGraphNode(config=self.config)
        self._nodes["post_process"] = EvidenceBriefPostProcessNode(
            resource_resolver=self.config["resource_resolver"]
        )

    def _enrich_output(self, output: dict, result: dict) -> None:
        if output.get("status") != "error":
            return
        raw = (result.get("error_log") or [""])[0]
        error_codes = {
            "policy_edition_invalid": "policy_edition_invalid",
            "payload_ref_unknown": "payload_unresolvable",
            "payload_ref_expired": "payload_unresolvable",
            "payload_ref_scope_mismatch": "payload_unresolvable",
            "packet_rejected": "packet_rejected",
            "packet_missing_required_fields": "packet_rejected",
            "llm_call_failed": "llm_call_failed",
            "llm_output_parse_failed": "llm_output_parse_failed",
            "llm_not_configured": "llm_not_configured",
            "workflow_failed": "workflow_failed",
        }
        output["error_code"] = error_codes.get(raw, "packet_rejected")


# Registry-compatible alias.
Graph = EngagementAcceptanceEvidenceGraph
