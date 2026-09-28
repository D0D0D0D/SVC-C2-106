"""HTTP adapter: authentication, payload isolation, and graph invocation only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from fastapi import FastAPI, HTTPException, Request

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from shared.utils.audit_logger import emit_trace_event
from src.api import stg_smoke
from src.entry_adapter import AuthContext, Operation, authenticate
from src.graph.graph import EngagementAcceptanceEvidenceGraph
from src.ingest_row_pipeline import CheckpointScrubPolicy, scrub_checkpoint_envelope
from src.payload_store import PayloadStoreError, is_reference, mint_reference
from src.services import AuthorizedResourceResolver


app = FastAPI(title="Professional Services Engagement Acceptance Evidence Agent")

_RUNTIME_CONFIG_PATH = Path(__file__).parents[2] / "config" / "config.yaml"

_CHECKPOINT_POLICY = CheckpointScrubPolicy(
    safe_keys=frozenset({"mode", "customer_id", "case_ref", "policy_edition_ref"}),
    scope_keys=frozenset({"customer_ref"}),
    enum_value_keys={"mode": frozenset({"invoke", "ingest", "feedback"})},
    safe_scalar_list_keys=frozenset(),
)


def _operation_for_mode(mode: str) -> Operation:
    """Classify the real side effect, not the route name.

    ``invoke`` persists the case/evidence registry and S-3-gated claim
    snapshots for later feedback, so it is a write operation even though its
    response is a brief.  ``WRITE_INGEST`` is the entry-adapter's generic
    record-persistence policy bucket; it intentionally keeps standalone tokens
    out of this path.
    """

    return {
        "invoke": Operation.WRITE_INGEST,
        "ingest": Operation.WRITE_INGEST,
        "feedback": Operation.WRITE_FEEDBACK,
    }[mode]


def load_runtime_config(path: Path = _RUNTIME_CONFIG_PATH) -> dict[str, Any]:
    """Load the repository runtime defaults with strict, fail-closed types."""
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError("runtime_config_unavailable") from exc
    required = {
        "profession_profile": str,
        "top_k": int,
        "retention_days": int,
        "payload_ttl_s": int,
        "max_rationale_chars": int,
        "memory_enabled": bool,
    }
    if not isinstance(data, dict) or any(not isinstance(data.get(key), value_type) for key, value_type in required.items()):
        raise RuntimeError("runtime_config_invalid")
    if data["top_k"] < 1 or data["payload_ttl_s"] < 1 or data["max_rationale_chars"] < 1:
        raise RuntimeError("runtime_config_invalid")
    return data


def wire_services(application: FastAPI = app) -> EngagementAcceptanceEvidenceGraph:
    """Wire customer-local service handles before the first invocation.

    Hosting supplies ``application.state.resource_scopes`` keyed by verified
    caller id and an injected ``application.state.llm``. No default/fallback
    scope or LLM client is constructed by the template.
    """
    scopes = getattr(application.state, "resource_scopes", None)
    if not isinstance(scopes, dict) or not scopes:
        raise RuntimeError("wire_services requires customer-specific resource_scopes")
    llm = getattr(application.state, "llm", None)
    if llm is None:
        raise RuntimeError("wire_services requires an injected llm")
    runtime = load_runtime_config()
    resolver = AuthorizedResourceResolver(scopes)
    config = {"resource_resolver": resolver, "llm": llm,
              "top_k": getattr(application.state, "top_k", runtime["top_k"]),
              "profession_profile": getattr(application.state, "profession_profile", runtime["profession_profile"]),
              "max_rationale_chars": getattr(application.state, "max_rationale_chars", runtime["max_rationale_chars"]),
              "memory_enabled": False, "hitl": {"enabled": False}}
    graph = EngagementAcceptanceEvidenceGraph(config)
    graph.compile()
    graph.provision_secrets(secrets_factory(namespace="svc-c2-106", agent_name="engagement-acceptance-evidence"))
    application.state.agent = graph
    application.state.resource_resolver = resolver
    application.state.case_payload_refs = {}
    return graph


@app.on_event("startup")
async def startup() -> None:
    # No-op outside the ephemeral deploy-stg smoke job (src/api/stg_smoke.py).
    stg_smoke.provision(app)
    wire_services(app)


def _agent() -> EngagementAcceptanceEvidenceGraph:
    graph = getattr(app.state, "agent", None)
    if graph is None:
        graph = wire_services(app)
    return graph


def _caller(request: Request, customer_ref: str, *, allow_stg_token: bool) -> str:
    value = getattr(request.state, "caller_id", "")
    if isinstance(value, str) and value:
        if value.startswith("stg:"):
            raise HTTPException(403, "reserved_caller_id_prefix")
        return value
    if allow_stg_token:
        return f"stg:{customer_ref}"
    raise HTTPException(403, "middleware_caller_id_required")


def _error_detail(result: dict[str, Any]) -> str:
    """Return an allowlisted 400 detail, never an inner exception string."""
    error_code = result.get("error_code")
    if error_code in {
        "packet_rejected",
        "policy_edition_invalid",
        "payload_unresolvable",
        "llm_call_failed",
        "llm_output_parse_failed",
        "llm_not_configured",
        "workflow_failed",
    }:
        return error_code
    return "packet_rejected"


async def _invoke_mode(request: Request, body: dict[str, Any], mode: str) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(400, "packet_rejected")
    allowed = {
        "invoke": {"customer_id", "policy_edition_ref", "case_narrative", "evidence_declarations", "kyc_documents"},
        "ingest": {"customer_id", "policy_edition_ref", "records"},
        "feedback": {"customer_id", "case_ref", "claim_id", "perspective_code", "clause_id", "policy_edition_ref", "source_ids", "decision_outcome", "decision_rationale", "action", "approved"},
    }[mode]
    if set(body) - allowed or not {"customer_id"} <= set(body):
        raise HTTPException(400, "packet_rejected")
    auth: AuthContext = authenticate(request, operation=_operation_for_mode(mode))
    customer_ref = auth.scope["customer_ref"]
    caller_id = auth.principal["caller_id"]
    if body.get("customer_id") != customer_ref:
        emit_trace_event("customer_mismatch", {}, {})
        raise HTTPException(403, "customer_mismatch")
    resolver = app.state.resource_resolver
    try:
        scope = resolver.for_caller(caller_id)
    except PermissionError as exc:
        raise HTTPException(403, "authorization_unresolved") from exc
    if scope.customer_ref != customer_ref:
        raise HTTPException(403, "authorization_unresolved")
    case_ref = body.get("case_ref") if mode == "feedback" else mint_reference()
    if not isinstance(case_ref, str) or not is_reference(case_ref):
        raise HTTPException(400, "case_ref_invalid")
    session_id = mint_reference()
    ctx = InvocationContext(
        session_id=session_id,
        thread_id=session_id,
        caller_id=caller_id,
        caller_trust_level=auth.trust,
    )
    edition: str | None = None
    if mode in {"invoke", "ingest"}:
        candidate_edition = body.get("policy_edition_ref")
        if not isinstance(candidate_edition, str) or not candidate_edition:
            raise HTTPException(400, "policy_edition_invalid")
        edition = candidate_edition
    # Client payload_ref is intentionally ignored. The checkpoint scrubber
    # always mints a new scope/session-bound handle and returns only a control
    # envelope; raw request fields never enter graph state.
    payload_body = ({key: value for key, value in body.items() if key not in {"customer_id", "case_ref"}}
                    if mode == "feedback" else body)
    payload_records: object = payload_body.get("records", []) if mode == "ingest" else payload_body
    raw_envelope = {
        "mode": mode,
        "customer_id": customer_ref,
        "customer_ref": customer_ref,
        "case_ref": case_ref,
        "payload": {"records": payload_records},
    }
    if edition is not None:
        raw_envelope["policy_edition_ref"] = edition

    payload_ref: str | None = None
    try:
        scrubbed = json.loads(
            scrub_checkpoint_envelope(
                json.dumps(raw_envelope, ensure_ascii=False),
                scope.payloads,
                session_id=ctx.thread_id,
                trusted_scope=auth.scope,
                policy=_CHECKPOINT_POLICY,
            )
        )
        scrubbed.pop("payload", None)
        payload_ref = scrubbed["payload_ref"]
        if edition is not None:
            scrubbed["policy_edition_ref"] = edition
        with bound_secrets(_agent()._secrets_provider):
            result = _agent().invoke(json.dumps(scrubbed, ensure_ascii=False), ctx=ctx)
    except Exception as exc:  # unexpected graph/runtime failures remain fail closed
        emit_trace_event("backend_unavailable", {"error": type(exc).__name__}, {})
        raise HTTPException(500, "backend_unavailable") from exc
    finally:
        # Raw request bodies are needed only for this synchronous graph run.
        # Keep the opaque tracking entry for case deletion/audit, but destroy
        # the stored value on every success and failure path.
        if payload_ref is not None:
            scope.payloads.delete(
                payload_ref,
                scope={"customer_ref": customer_ref},
                session_id=ctx.thread_id,
            )
    if result.get("status") == "error":
        raise HTTPException(400, _error_detail(result))
    # Track successful cases only. Failed/cold paths neither retain raw data
    # nor leave a case deletion entry for a response that was never issued.
    app.state.case_payload_refs.setdefault(customer_ref, {}).setdefault(case_ref, []).append(
        (payload_ref, ctx.thread_id)
    )
    return {"status": result["status"], "output": result.get("output"), "correlation_id": result.get("correlation_id", ""), "case_ref": case_ref}


@app.post("/invoke")
async def invoke(request: Request, body: dict[str, Any]) -> dict:
    return await _invoke_mode(request, body, "invoke")


@app.post("/ingest")
async def ingest(request: Request, body: dict[str, Any]) -> dict:
    return await _invoke_mode(request, body, "ingest")


@app.post("/feedback")
async def feedback(request: Request, body: dict[str, Any]) -> dict:
    return await _invoke_mode(request, body, "feedback")


@app.delete("/cases/{case_ref}")
async def delete_case(case_ref: str, request: Request) -> dict:
    auth = authenticate(request, operation=Operation.WRITE_INGEST)
    customer_ref = auth.scope["customer_ref"]
    scope = app.state.resource_resolver.for_caller(auth.principal["caller_id"])
    if scope.customer_ref != customer_ref:
        raise HTTPException(403, "authorization_unresolved")
    customer_refs = app.state.case_payload_refs.get(customer_ref, {})
    payload_refs = customer_refs.get(case_ref)
    if payload_refs is None:
        raise HTTPException(404, "case_not_found")
    payloads = 0
    for payload_ref, session_id in payload_refs:
        try:
            scope.payloads.delete(payload_ref, scope={"customer_ref": customer_ref}, session_id=session_id)
            payloads += 1
        except PayloadStoreError as exc:
            safe_code = str(exc)
            if safe_code not in {"payload_ref_scope_missing", "payload_ref_scope_mismatch"}:
                safe_code = "payload_unresolvable"
            raise HTTPException(400, safe_code) from exc
    customer_refs.pop(case_ref, None)
    deleted = {"db_rows": scope.db.delete_case(case_ref), "kb_docs": scope.kb.delete_case(case_ref), "payloads": payloads}
    emit_trace_event("case_deleted", {"case_ref": case_ref}, {})
    return {"status": "success", "deleted": deleted}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "agent": "SVC-C2-106"}
