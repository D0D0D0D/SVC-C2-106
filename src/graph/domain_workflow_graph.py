"""Inner, fixed domain workflow for engagement acceptance evidence."""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any, ClassVar

from langgraph.graph import END, START

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.evidence_gate import validate_provenance
from src.ingest_row_pipeline import IngestRowPipelineNode, RowPipelineHooks
from src.llm_draft_stage import DraftStageHooks, LlmDraftStageNode
from src.payload_store import PayloadStoreError, mint_reference
from src.schemas.state import EngagementWorkflowState
from src.services import (
    AuthorizedResourceResolver,
    AuthorizationError,
    DECISION_OUTCOMES,
    PERSPECTIVE_CODES,
    build_retrieval_text,
    evidence_map_for_response,
    make_source_registry,
    normalize_response_source_ids,
    project_response,
    response_contract,
    sanitize_decision_rationale,
)

_INNER_INPUT: ContextVar[str] = ContextVar("engagement_workflow_input", default="{}")


def _scope(state: dict, resolver: AuthorizedResourceResolver):
    return resolver.for_caller(InvocationContext.from_state(state).caller_id)


def _payload(state: dict, resolver: AuthorizedResourceResolver) -> dict[str, Any]:
    scope = _scope(state, resolver)
    ctx = InvocationContext.from_state(state)
    raw = scope.payloads.resolve(
        state.get("payload_ref"), scope={"customer_ref": scope.customer_ref}, session_id=ctx.thread_id
    )
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PayloadStoreError("payload_ref_unresolvable") from exc
    if isinstance(raw, dict) and isinstance(raw.get("records"), dict):
        raw = raw["records"]
    if not isinstance(raw, dict):
        raise PayloadStoreError("payload_ref_unresolvable")
    return raw


class _Stage(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, resolver: AuthorizedResourceResolver | None = None, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._resolver = resolver
        self._config = dict(config or {})

    def _resolver_or_error(self) -> AuthorizedResourceResolver:
        if self._resolver is None:
            raise AuthorizationError("authorization_unresolved")
        return self._resolver


class DeterministicCheckNode(_Stage):
    def execute(self, state: EngagementWorkflowState) -> dict:
        emit_trace_event("deterministic_check_completed", {"case_ref": state.get("case_ref", "")}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        try:
            resolver = self._resolver_or_error()
            scope = _scope(state, resolver)
            body = _payload(state, resolver)
            edition = str(state.get("policy_edition_ref", ""))
            clauses = scope.db.policy_clauses(edition)
            if not clauses or scope.db.policy_profile(edition) != self._config.get("profession_profile"):
                raise ValueError("policy_edition_invalid")
            declarations = body.get("evidence_declarations", [])
            if not isinstance(declarations, list):
                raise ValueError("evidence_declarations_invalid")
            records = []
            for item in declarations:
                if not isinstance(item, dict) or not item.get("source_id"):
                    raise ValueError("evidence_declarations_invalid")
                records.append({
                    "source_id": str(item["source_id"]), "record_id": str(item.get("record_id", item["source_id"])),
                    "doc_type": str(item.get("doc_type", "unknown")), "doc_ref": str(item.get("doc_ref", "")),
                    "span_ref": str(item.get("span_ref", "")),
                })
            registry = make_source_registry(state["case_ref"], edition, records)
            required = {str(row.get("required_doc_type", "")) for row in clauses if row.get("required_doc_type")}
            present = {row["doc_type"] for row in records}
            gaps = [
                {
                    "description": f"Required evidence is missing: {row['required_doc_type']}",
                    "clause_id": str(row["clause_id"]),
                    "source_ids": [],
                }
                for row in clauses if row.get("required_doc_type") and row["required_doc_type"] not in present
            ]
            return {"docs_present": {key: key in present for key in required}, "format_check": {"valid": True},
                    "source_registry": registry, "retrieved_records": records, "policy_clauses": clauses,
                    "gaps": gaps, "status": AgentStatus.SUCCESS.value}
        except (AuthorizationError, PayloadStoreError, ValueError) as exc:
            return {"status": AgentStatus.ERROR.value, "error_log": [str(exc)]}


class RecallPerspectivesNode(_Stage):
    def execute(self, state: EngagementWorkflowState) -> dict:
        emit_trace_event("perspectives_recalled", {"case_ref": state.get("case_ref", "")}, state)
        try:
            resolver = self._resolver_or_error()
            scope = _scope(state, resolver)
            body = _payload(state, resolver)
            query = build_retrieval_text(" ".join(row.get("clause_id", "") for row in state.get("policy_clauses", [])),
                                         str(body.get("case_narrative", "")), "other")
            refs = scope.kb.search(query, policy_edition_ref=state.get("policy_edition_ref", ""), top_k=int(self._config.get("top_k", 8)))
            if not refs:
                return {"recall_refs": [], "degraded": True, "degradation_code": "kb_empty", "status": AgentStatus.SUCCESS.value}
            return {"recall_refs": refs, "degraded": False, "status": AgentStatus.SUCCESS.value}
        except (AuthorizationError, PayloadStoreError) as exc:
            return {"status": AgentStatus.ERROR.value, "error_log": [str(exc)]}
        except RuntimeError:
            return {"status": AgentStatus.ERROR.value, "error_log": ["retrieval_backend_unavailable"]}


class ApplicabilityDraftNode(_Stage):
    def execute(self, state: EngagementWorkflowState) -> dict:
        emit_trace_event("applicability_draft_requested", {"case_ref": state.get("case_ref", "")}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        try:
            body = _payload(state, self._resolver_or_error())
        except (AuthorizationError, PayloadStoreError) as exc:
            return {"status": AgentStatus.ERROR.value, "error_log": [str(exc)]}
        hooks = DraftStageHooks(
            build_prompt=lambda current_state: _build_prompt(current_state, body),
            build_fallback=lambda current_state: _fallback_brief(current_state),
            build_evidence=_normalized_evidence,
        )
        stage = LlmDraftStageNode(config={"llm": self._config.get("llm")}, hooks=hooks)
        stage_result = stage(state)
        if stage_result.get("status") == AgentStatus.ERROR.value:
            return {
                key: stage_result[key]
                for key in ("status", "error_code", "error_type", "error_log")
                if key in stage_result
            }
        brief, dropped_candidate = normalize_response_source_ids(
            project_response(stage_result.get("draft")),
            set(state.get("source_registry", {})),
        )
        evidence_map, cited = evidence_map_for_response(brief)
        degraded = bool(
            state.get("degraded", False)
            or stage_result.get("degradation_reason")
            or dropped_candidate
        )
        degradation_code = (
            "finding_dropped_unknown_source"
            if dropped_candidate
            else "llm_unavailable"
            if stage_result.get("degradation_reason")
            else state.get("degradation_code")
        )
        return {
            "evidence_brief": brief,
            "evidence_map": evidence_map,
            "cited_source_ids": cited,
            "requires_human_review": bool(
                state.get("requires_human_review", False) or dropped_candidate
            ),
            "degraded": degraded,
            "degradation_code": degradation_code,
            "status": AgentStatus.SUCCESS.value,
        }


class EvidenceValidationNode(_Stage):
    def execute(self, state: EngagementWorkflowState) -> dict:
        emit_trace_event("evidence_validation_completed", {"case_ref": state.get("case_ref", "")}, state)
        brief = project_response(state.get("evidence_brief"))
        clause_ids = {
            str(row.get("clause_id"))
            for row in state.get("policy_clauses", [])
            if isinstance(row, dict) and row.get("clause_id")
        }
        with_ids: dict[str, list[dict[str, Any]]] = {}
        for section, claims in brief.items():
            with_ids[section] = []
            for claim in claims:
                if claim["clause_id"] not in clause_ids:
                    raise ValueError("llm_clause_id_invalid")
                with_ids[section].append({**claim, "claim_id": mint_reference()})
        # Stage 1 owns deterministic missing-evidence gaps.  They are not LLM
        # claims and may legitimately have no source because the evidence is
        # absent; merge them after provider-authored findings have passed the
        # section-specific response projector above.
        with_ids["gaps"] = [
            *[
                {**item, "claim_id": mint_reference()}
                for item in state.get("gaps", [])
                if isinstance(item, dict)
            ],
            *with_ids["gaps"],
        ]
        evidence_map, cited = evidence_map_for_response(with_ids)
        validate_provenance(
            {
                "gate_kind": "draft",
                "status": AgentStatus.SUCCESS.value,
                "cited_source_ids": cited,
                "retrieved_records": state.get("retrieved_records", []),
                "evidence_map": evidence_map,
            }
        )
        return {
            "evidence_brief": with_ids,
            "evidence_map": evidence_map,
            "cited_source_ids": cited,
            "requires_human_review": bool(state.get("requires_human_review", False)),
            "status": AgentStatus.SUCCESS.value,
        }


class EngagementIngestPipelineNode(IngestRowPipelineNode):
    """Domain declarations layered over the vendored batch pipeline."""

    required_fields = (
        "policy_edition_ref",
        "clause_id",
        "consultation_summary",
        "decision_outcome",
        "decision_rationale",
        "perspective_code",
    )
    required_any_fields: ClassVar[tuple[tuple[str, ...], ...]] = ()


class AccumulateIngestNode(_Stage):
    def execute(self, state: EngagementWorkflowState) -> dict:
        emit_trace_event("ingest_accumulated", {"case_ref": state.get("case_ref", "")}, state)
        try:
            resolver = self._resolver_or_error()
            scope = _scope(state, resolver)
            ctx = InvocationContext.from_state(state)
            expected_keys = frozenset(EngagementIngestPipelineNode.required_fields)

            def sanitize(_state: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
                if frozenset(row) != expected_keys:
                    raise ValueError("record_shape_invalid")
                if row["decision_outcome"] not in DECISION_OUTCOMES or row["perspective_code"] not in PERSPECTIVE_CODES:
                    raise ValueError("record_enum_invalid")
                if not all(
                    isinstance(row[key], str) and bool(row[key])
                    for key in expected_keys
                ):
                    raise ValueError("record_type_invalid")
                raw, audit, safe = sanitize_decision_rationale(
                    row["decision_rationale"],
                    int(self._config.get("max_rationale_chars", 1000)),
                )
                return {
                    **row,
                    "decision_rationale_raw": raw,
                    "decision_rationale_audit": audit,
                    "decision_rationale_safe": safe,
                }

            def normalize(current_state: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
                return {
                    **row,
                    "history_ref": "hist" + mint_reference(),
                    "case_ref": current_state["case_ref"],
                    "retrieval_text": build_retrieval_text(
                        row["clause_id"], row["consultation_summary"], row["perspective_code"]
                    ),
                }

            def write_batch(_state: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
                db_ids: list[str] = []
                history_ids: list[str] = []
                try:
                    for row in rows:
                        projection = {
                            key: row[key]
                            for key in (
                                "history_ref", "case_ref", "policy_edition_ref", "clause_id",
                                "perspective_code", "consultation_summary", "decision_outcome",
                                "decision_rationale_safe", "retrieval_text",
                            )
                        }
                        db_ids.append(
                            scope.db.save_perspective(
                                {
                                    **projection,
                                    "decision_rationale_raw": row["decision_rationale_raw"],
                                    "decision_rationale_audit": row["decision_rationale_audit"],
                                }
                            )
                        )
                        history_ids.append(scope.kb.upsert(projection))
                except Exception as exc:
                    for history_ref in reversed(history_ids):
                        scope.kb.delete_history(history_ref)
                    for perspective_id in reversed(db_ids):
                        scope.db.delete_perspective(perspective_id)
                    raise RuntimeError("atomic_ingest_failed") from exc
                return history_ids

            pipeline = EngagementIngestPipelineNode(
                payload_store=scope.payloads,
                trusted_scope={"customer_ref": scope.customer_ref},
                session_id=ctx.thread_id,
                hooks=RowPipelineHooks(
                    sanitize=sanitize,
                    normalize=normalize,
                    write_batch=write_batch,
                ),
            )
            result = pipeline(state)
            return {
                key: result[key]
                for key in ("ingest_summary", "status", "error_log")
                if key in result
            }
        except (AuthorizationError, PayloadStoreError, ValueError) as exc:
            return {"status": AgentStatus.ERROR.value, "error_log": [str(exc)]}


class AccumulateFeedbackNode(_Stage):
    def execute(self, state: EngagementWorkflowState) -> dict:
        emit_trace_event("feedback_accumulated", {"case_ref": state.get("case_ref", "")}, state)
        try:
            resolver = self._resolver_or_error()
            scope = _scope(state, resolver)
            feedback = _payload(state, resolver)
            required = {"claim_id", "perspective_code", "clause_id", "policy_edition_ref", "source_ids", "decision_outcome", "decision_rationale", "action", "approved"}
            if set(feedback) - required or set(feedback) != required:
                raise ValueError("feedback_schema_invalid")
            if feedback["decision_outcome"] not in DECISION_OUTCOMES or feedback["perspective_code"] not in PERSPECTIVE_CODES:
                raise ValueError("feedback_enum_invalid")
            if feedback["action"] not in {"approve", "edit", "reject"} or not isinstance(feedback["approved"], bool):
                raise ValueError("feedback_action_invalid")
            if feedback["action"] == "reject" and feedback["approved"]:
                raise ValueError("feedback_action_approval_conflict")
            clauses = {row.get("clause_id") for row in scope.db.policy_clauses(feedback["policy_edition_ref"])}
            evidence_ids = {row.get("source_id") for row in scope.db.evidence_records(state["case_ref"])}
            source_ids = feedback["source_ids"]
            if (
                feedback["clause_id"] not in clauses
                or not isinstance(source_ids, list)
                or not all(isinstance(source_id, str) and source_id for source_id in source_ids)
                or not set(source_ids) <= evidence_ids
            ):
                raise ValueError("feedback_reference_invalid")
            snapshot = scope.db.claim_snapshot(feedback["claim_id"])
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("case_ref") != state["case_ref"]
                or snapshot.get("customer_ref") != scope.customer_ref
                or snapshot.get("policy_edition_ref") != feedback["policy_edition_ref"]
                or snapshot.get("clause_id") != feedback["clause_id"]
                or snapshot.get("source_ids") != feedback["source_ids"]
            ):
                raise ValueError("claim_snapshot_invalid")
            raw, audit, safe = sanitize_decision_rationale(
                feedback["decision_rationale"], int(self._config.get("max_rationale_chars", 1000))
            )
            applied = 0
            if feedback["approved"] and feedback["action"] != "reject":
                history_ref = "hist" + mint_reference()
                projection = {"history_ref": history_ref, "case_ref": state["case_ref"], "policy_edition_ref": feedback["policy_edition_ref"],
                              "clause_id": feedback["clause_id"], "perspective_code": feedback["perspective_code"], "consultation_summary": snapshot["text"],
                              "decision_outcome": feedback["decision_outcome"], "decision_rationale_safe": safe,
                              "retrieval_text": build_retrieval_text(feedback["clause_id"], snapshot["text"], feedback["perspective_code"])}
                perspective_id = ""
                kb_written = False
                try:
                    perspective_id = scope.db.save_perspective(
                        {**projection, "decision_rationale_raw": raw, "decision_rationale_audit": audit}
                    )
                    scope.kb.upsert(projection)
                    kb_written = True
                    event_id = scope.db.audit(
                        {"case_ref": state["case_ref"], "action": feedback["action"], "approved": feedback["approved"]}
                    )
                except Exception:
                    if kb_written:
                        scope.kb.delete_history(history_ref)
                    if perspective_id:
                        scope.db.delete_perspective(perspective_id)
                    raise
                applied = 1
            else:
                event_id = scope.db.audit(
                    {"case_ref": state["case_ref"], "action": feedback["action"], "approved": feedback["approved"]}
                )
            return {"feedback_summary": {"applied_count": applied, "audit_event_id": event_id}, "status": AgentStatus.SUCCESS.value}
        except (AuthorizationError, PayloadStoreError, ValueError) as exc:
            return {"status": AgentStatus.ERROR.value, "error_log": [str(exc)]}
        except Exception:
            return {"status": AgentStatus.ERROR.value, "error_log": ["feedback_persist_failed"]}


class EngagementWorkflowGraph(BaseGraph):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config)

    @property
    def name(self) -> str:
        return "engagement_workflow"

    @property
    def state_schema(self) -> type:
        return EngagementWorkflowState

    def _validate_config(self) -> None:
        if not isinstance(self.config.get("resource_resolver"), AuthorizedResourceResolver):
            raise ConfigError("resource_resolver is required")
        if not isinstance(self.config.get("top_k", 8), int):
            raise ConfigError("top_k must be an integer")

    def _extra_initial_state(self) -> dict:
        try:
            packet = json.loads(_INNER_INPUT.get())
        except json.JSONDecodeError:
            packet = {}
        return packet if isinstance(packet, dict) else {}

    def invoke(self, user_input: str, *args, **kwargs):
        token = _INNER_INPUT.set(user_input)
        try:
            return super().invoke(user_input, *args, **kwargs)
        finally:
            _INNER_INPUT.reset(token)

    def register_nodes(self) -> None:
        resolver = self.config["resource_resolver"]
        self._nodes = {"deterministic_check": DeterministicCheckNode(resolver=resolver, config=self.config),
                       "recall_perspectives": RecallPerspectivesNode(resolver=resolver, config=self.config),
                       "applicability_draft": ApplicabilityDraftNode(resolver=resolver, config=self.config),
                       "evidence_validation": EvidenceValidationNode(resolver=resolver, config=self.config),
                       "accumulate_ingest": AccumulateIngestNode(resolver=resolver, config=self.config),
                       "accumulate_feedback": AccumulateFeedbackNode(resolver=resolver, config=self.config)}

    def add_edges(self) -> None:
        self._sg.add_conditional_edges(START, self.route)
        self._sg.add_edge("deterministic_check", "recall_perspectives")
        self._sg.add_edge("recall_perspectives", "applicability_draft")
        self._sg.add_edge("applicability_draft", "evidence_validation")
        self._sg.add_edge("evidence_validation", END)
        self._sg.add_edge("accumulate_ingest", END)
        self._sg.add_edge("accumulate_feedback", END)

    def route(self, state: EngagementWorkflowState) -> str:
        return {"invoke": "deterministic_check", "ingest": "accumulate_ingest", "feedback": "accumulate_feedback"}.get(state.get("mode"), END)

    def get_output(self, state: EngagementWorkflowState) -> dict:
        keys = {"invoke": ("evidence_brief", "source_registry", "policy_clauses", "cited_source_ids", "retrieved_records", "requires_human_review", "degraded", "degradation_code"),
                "ingest": ("ingest_summary",), "feedback": ("feedback_summary",)}.get(state.get("mode"), ())
        return {
            "mode": state.get("mode"),
            "status": state.get("status"),
            **{key: state[key] for key in keys if key in state},
            **{key: state[key] for key in ("error_code", "error_type") if key in state},
            "error_log": state.get("error_log", []),
        }


def _fallback_brief(state: dict) -> dict:
    return {
        "candidate_conflicts": [],
        # Deterministic Stage 1 gaps are merged after this draft passes the
        # same projector used for provider output.
        "gaps": [],
        "verification_questions": [],
    }


def _normalized_evidence(
    state: dict[str, Any], draft: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    response, _ = normalize_response_source_ids(
        project_response(draft),
        set(state.get("source_registry", {})),
    )
    return evidence_map_for_response(response)


def _build_prompt(state: dict, body: dict) -> str:
    current = {"policy_clauses": state.get("policy_clauses", []), "case_narrative": body.get("case_narrative", ""), "evidence": state.get("retrieved_records", [])}
    history = state.get("recall_refs", [])
    permitted_source_ids = sorted(
        {
            str(record["source_id"])
            for record in state.get("retrieved_records", [])
            if isinstance(record, dict) and record.get("source_id")
        }
    )
    return (
        "Block 1 (permitted evidence): use only this case's clauses and evidence as claim support.\n"
        + "Permitted source_ids values: "
        + json.dumps(permitted_source_ids, ensure_ascii=False)
        + ". Never put case_narrative or any other string in source_ids. "
        + "For an evidence-absent gap or verification question, use source_ids: [] and a registered clause_id.\n"
        + json.dumps(current, ensure_ascii=False, default=str)
        + "\nBlock 2 (reference only): prior cases may inform questions but must never be cited as evidence for this case.\n"
        + json.dumps(history, ensure_ascii=False, default=str)
        + "\nReturn one JSON object matching this response contract exactly. Do not add keys.\n"
        + json.dumps(response_contract(), ensure_ascii=False, sort_keys=True)
    )
