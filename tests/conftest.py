"""Fixtures consumed by ``work/scripts/mrb_live_llm_check.py``."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.graph.graph import EngagementAcceptanceEvidenceGraph
from src.payload_store import PayloadStore, mint_reference
from src.services import (
    AuthorizedResourceResolver,
    InMemoryEngagementDb,
    InMemoryEngagementKb,
    ResourceScope,
    build_retrieval_text,
)


_LIVE_CUSTOMER = "customer-live"
_LIVE_CALLER = "live-check"
_LIVE_SESSION_ID = "live-1"
_LIVE_EDITION = "acceptance-policy-2026"


def _case_body() -> dict:
    return {
        "customer_id": _LIVE_CUSTOMER,
        "policy_edition_ref": _LIVE_EDITION,
        "case_narrative": (
            "A subsidiary of the current audit client requests internal-control remediation "
            "with a success fee tied to financing. The team declaration reports no conflict, "
            "but the proposed work includes designing controls later reviewed by the audit team. "
            "Ultimate-beneficial-owner evidence has not been supplied."
        ),
        "evidence_declarations": [
            {
                "source_id": "conflict-search-aug",
                "doc_type": "conflict_check",
                "doc_ref": "conflict-register-2026",
                "span_ref": "entry-44",
            },
            {
                "source_id": "independence-declaration-aug",
                "doc_type": "independence_declaration",
                "doc_ref": "team-declaration-2026",
                "span_ref": "section-2",
            },
        ],
    }


@pytest.fixture
def _live_runtime(monkeypatch):
    db = InMemoryEngagementDb(
        editions={
            _LIVE_EDITION: {
                "profession_profile": "audit_firm",
                "clauses": [
                    {
                        "clause_id": "conflict-screening",
                        "clause_ref": "A-1",
                        "required_doc_type": "conflict_check",
                    },
                    {
                        "clause_id": "self-review-threat",
                        "clause_ref": "A-2",
                        "required_doc_type": "independence_declaration",
                    },
                    {
                        "clause_id": "client-identity",
                        "clause_ref": "A-3",
                        "required_doc_type": "kyc",
                    },
                ],
            }
        }
    )
    kb = InMemoryEngagementKb()
    kb.upsert(
        {
            "history_ref": "history-self-review",
            "case_ref": "case-prior",
            "policy_edition_ref": _LIVE_EDITION,
            "clause_id": "self-review-threat",
            "perspective_code": "independence",
            "consultation_summary": (
                "Audit-client subsidiary requested control design with a contingent fee."
            ),
            "decision_outcome": "declined",
            "decision_rationale_safe": (
                "Review whether control design creates a self-review or management threat."
            ),
            "retrieval_text": build_retrieval_text(
                "self-review-threat",
                "audit client subsidiary control design success fee",
                "other",
            ),
        }
    )
    payloads = PayloadStore(scope_keys=("customer_ref",), ttl_seconds=900)
    payload_ref = payloads.put(
        {"records": _case_body()},
        scope={"customer_ref": _LIVE_CUSTOMER},
        session_id=_LIVE_SESSION_ID,
    )
    scope = ResourceScope(_LIVE_CUSTOMER, db, kb, payloads)
    resolver = AuthorizedResourceResolver({_LIVE_CALLER: scope})
    case_ref = mint_reference()

    original_invoke = EngagementAcceptanceEvidenceGraph.invoke

    def invoke_with_server_thread(self, user_input, *args, **kwargs):
        # The HTTP adapter sets thread_id=session_id before storing payloads;
        # the generic checker supplies only session_id, so restore that binding.
        ctx = kwargs.get("ctx")
        if ctx is not None:
            kwargs["ctx"] = replace(
                ctx,
                thread_id=_LIVE_SESSION_ID,
                hitl_allowed=False,
            )
        return original_invoke(self, user_input, *args, **kwargs)

    monkeypatch.setattr(
        EngagementAcceptanceEvidenceGraph,
        "invoke",
        invoke_with_server_thread,
    )
    return {
        "resolver": resolver,
        "payload": {
            "mode": "invoke",
            "customer_id": _LIVE_CUSTOMER,
            "customer_ref": _LIVE_CUSTOMER,
            "policy_edition_ref": _LIVE_EDITION,
            "case_ref": case_ref,
            "payload_ref": payload_ref,
        },
    }


@pytest.fixture
def agent_config(_live_runtime) -> dict:
    """Customer resource resolver; the checker injects ``config['llm']``."""

    return {
        "resource_resolver": _live_runtime["resolver"],
        "top_k": 8,
        "profession_profile": "audit_firm",
        "max_rationale_chars": 1000,
        "memory_enabled": False,
        "hitl": {"enabled": False},
    }


@pytest.fixture
def case_payload(_live_runtime) -> dict:
    """Server-style payload-ref envelope for a substantive acceptance review."""

    return _live_runtime["payload"]
