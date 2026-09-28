"""Closed S-3 output contract and mode-specific evidence envelopes."""

from __future__ import annotations

import re
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.output_envelope import OutputEnvelopeNode
from src.services import AuthorizedResourceResolver, output_keys_for_section


INVOKE_TOP_LEVEL_KEYS = frozenset({"mode", "gate_kind", "meta", "policy_clauses", "evidence_sources", "candidate_conflicts", "gaps", "verification_questions"})
INGEST_TOP_LEVEL_KEYS = frozenset({"mode", "gate_kind", "meta", "ingest_summary"})
FEEDBACK_TOP_LEVEL_KEYS = frozenset({"mode", "gate_kind", "meta", "feedback_summary"})
_SAFE_ID = re.compile(r"^[0-9A-Za-z_-]+$")


class EvidenceBriefPostProcessNode(OutputEnvelopeNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    gate_kind_by_mode: ClassVar[dict[str, str]] = {
        "invoke": "engagement_acceptance_evidence", "ingest": "engagement_acceptance_evidence", "feedback": "engagement_acceptance_evidence",
    }
    required_fields_by_mode: ClassVar[dict[str, tuple[str, ...]]] = {
        "invoke": tuple(INVOKE_TOP_LEVEL_KEYS), "ingest": tuple(INGEST_TOP_LEVEL_KEYS), "feedback": tuple(FEEDBACK_TOP_LEVEL_KEYS),
    }

    def __init__(self, *, resource_resolver: AuthorizedResourceResolver | None = None) -> None:
        super().__init__()
        self._resource_resolver = resource_resolver

    def execute(self, state: dict) -> dict:
        emit_trace_event("evidence_brief_formatted", {"mode": state.get("mode", "")}, state)
        if not state.get("mode"):
            return super().execute({**state, "status": AgentStatus.ERROR.value, "error_log": ["mode_missing"]})
        return super().execute(state)

    def format_invoke(self, state: dict) -> dict[str, Any]:
        brief = state.get("evidence_brief", {})
        if not isinstance(brief, dict):
            brief = {}
        registry = state.get("source_registry", {})
        evidence_sources = []
        for source_id in state.get("cited_source_ids", []):
            row = registry.get(source_id)
            if isinstance(row, dict):
                evidence_sources.append({"source_id": source_id, "doc_type": row.get("doc_type", "unknown"), "quote_span": row.get("span_ref", ""), "provenance": row.get("provenance", {})})
        return {
            "meta": {"degraded": bool(state.get("degraded", False)), "degradation_code": state.get("degradation_code")},
            "policy_clauses": [
                {"clause_id": row.get("clause_id", ""), "policy_edition_ref": state.get("policy_edition_ref", ""), "clause_ref": row.get("clause_ref", "")}
                for row in state.get("policy_clauses", []) if isinstance(row, dict)
            ],
            "evidence_sources": evidence_sources,
            "candidate_conflicts": brief.get("candidate_conflicts", []), "gaps": brief.get("gaps", []),
            "verification_questions": brief.get("verification_questions", []),
        }

    def format_ingest(self, state: dict) -> dict[str, Any]:
        return {"meta": {"degraded": False, "degradation_code": None}, "ingest_summary": state.get("ingest_summary", {})}

    def format_feedback(self, state: dict) -> dict[str, Any]:
        return {"meta": {"degraded": False, "degradation_code": None}, "feedback_summary": state.get("feedback_summary", {})}

    def _extra_security_gate_output(self, result: dict) -> dict:
        hook_state = result.get("_output_envelope_hook_state") or {}
        result = super()._extra_security_gate_output(result)
        if result.get("status") == AgentStatus.ERROR.value:
            return result
        formatted = result["formatted_output"]
        _validate_envelope(formatted, hook_state)
        self._persist_gated_invoke(formatted, hook_state)
        return result

    def _persist_gated_invoke(self, envelope: dict[str, Any], state: dict[str, Any]) -> None:
        if envelope.get("mode") != "invoke" or self._resource_resolver is None:
            return
        scope = self._resource_resolver.for_caller(InvocationContext.from_state(state).caller_id)
        text_keys = {
            "candidate_conflicts": "description",
            "gaps": "description",
            "verification_questions": "question",
        }
        try:
            # Case/evidence and claim snapshots share the same commit point:
            # only the output that has passed the domain S-3 envelope gate is
            # allowed to become durable business state.
            scope.db.save_case(
                state["case_ref"],
                state["policy_edition_ref"],
                list(state.get("retrieved_records", [])),
            )
            for claim_type, text_key in text_keys.items():
                for claim in envelope[claim_type]:
                    scope.db.save_claim_snapshot({
                        "claim_id": claim["claim_id"],
                        "case_ref": state["case_ref"],
                        "customer_ref": scope.customer_ref,
                        "policy_edition_ref": state["policy_edition_ref"],
                        "clause_id": claim["clause_id"],
                        "source_ids": list(claim["source_ids"]),
                        "text": claim[text_key],
                        "claim_type": claim_type,
                    })
        except Exception as exc:
            scope.db.delete_case(state["case_ref"])
            raise SecurityViolationError("S-3 gated persistence failed") from exc


def _validate_envelope(envelope: dict[str, Any], state: dict[str, Any]) -> None:
    mode = envelope.get("mode")
    expected = {"invoke": INVOKE_TOP_LEVEL_KEYS, "ingest": INGEST_TOP_LEVEL_KEYS, "feedback": FEEDBACK_TOP_LEVEL_KEYS}.get(mode)
    if expected is None or frozenset(envelope) != expected:
        raise SecurityViolationError("S-3 closed envelope schema violation")
    if envelope.get("gate_kind") != "engagement_acceptance_evidence":
        raise SecurityViolationError("S-3 gate kind violation")
    meta = envelope.get("meta")
    if not isinstance(meta, dict) or frozenset(meta) != frozenset({"degraded", "degradation_code"}) or not isinstance(meta["degraded"], bool) or meta["degradation_code"] not in {None, "kb_empty", "llm_unavailable", "finding_dropped_unknown_source"}:
        raise SecurityViolationError("S-3 meta schema violation")
    if mode != "invoke":
        _validate_summary(mode, envelope)
        return
    _exact_lists(envelope)
    registry = state.get("source_registry", {})
    edition = state.get("policy_edition_ref", "")
    clause_ids = {row.get("clause_id") for row in state.get("policy_clauses", []) if isinstance(row, dict)}
    cited: set[str] = set()
    for source in envelope["evidence_sources"]:
        source_id = source["source_id"]
        actual = registry.get(source_id)
        provenance = source["provenance"]
        if (
            not isinstance(source_id, str)
            or not _SAFE_ID.fullmatch(source_id)
            or not isinstance(actual, dict)
            or provenance["case_ref"] != state.get("case_ref")
            or provenance["policy_edition_ref"] != state.get("policy_edition_ref")
            or source["doc_type"] != actual.get("doc_type")
            or source["quote_span"] != actual.get("span_ref")
            or provenance != actual.get("provenance")
            or not source["quote_span"]
        ):
            raise SecurityViolationError("S-3 provenance violation")
    for claim_key, text_key in (("candidate_conflicts", "description"), ("gaps", "description"), ("verification_questions", "question")):
        for claim in envelope[claim_key]:
            source_ids = claim["source_ids"]
            if (
                claim["clause_id"] not in clause_ids
                or not isinstance(claim[text_key], str)
                or not isinstance(source_ids, list)
                or not all(isinstance(source_id, str) and _SAFE_ID.fullmatch(source_id) for source_id in source_ids)
                or not set(source_ids) <= set(registry)
                or (claim_key == "candidate_conflicts" and not source_ids)
            ):
                raise SecurityViolationError("S-3 claim source or clause violation")
            cited.update(source_ids)
            if claim_key == "candidate_conflicts" and (claim["severity"] != "candidate" or not isinstance(claim["confidence"], float) or not 0.0 <= claim["confidence"] <= 1.0):
                raise SecurityViolationError("S-3 candidate granularity violation")
    for clause in envelope["policy_clauses"]:
        if clause["policy_edition_ref"] != edition or clause["clause_id"] not in clause_ids:
            raise SecurityViolationError("S-3 policy clause violation")
    # Recompute, never trust a model declaration.
    state["cited_source_ids"] = sorted(cited)


def _exact_lists(envelope: dict[str, Any]) -> None:
    shapes = {
        "policy_clauses": frozenset({"clause_id", "policy_edition_ref", "clause_ref"}),
        "evidence_sources": frozenset({"source_id", "doc_type", "quote_span", "provenance"}),
        "candidate_conflicts": output_keys_for_section("candidate_conflicts"),
        "gaps": output_keys_for_section("gaps"),
        "verification_questions": output_keys_for_section("verification_questions"),
    }
    for name, keys in shapes.items():
        if not isinstance(envelope[name], list):
            raise SecurityViolationError("S-3 list type violation")
        for row in envelope[name]:
            if not isinstance(row, dict) or frozenset(row) != keys:
                raise SecurityViolationError("S-3 nested schema violation")
            if name == "evidence_sources" and (not isinstance(row["provenance"], dict) or frozenset(row["provenance"]) != frozenset({"case_ref", "record_id", "doc_ref", "span_ref", "policy_edition_ref"})):
                raise SecurityViolationError("S-3 provenance schema violation")


def _validate_summary(mode: str, envelope: dict[str, Any]) -> None:
    field = "ingest_summary" if mode == "ingest" else "feedback_summary"
    if not isinstance(envelope.get(field), dict):
        raise SecurityViolationError("S-3 summary schema violation")


# Compatibility name retained for generated scaffold consumers.
PostProcessNode = EvidenceBriefPostProcessNode
