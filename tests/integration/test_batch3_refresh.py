"""Batch 3 composition proofs for SVC-C2-106."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi.testclient import TestClient
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

import src.api.server as server
from src.entry_adapter import AuthContext, AuthSource, Operation
from src.graph.graph import EngagementAcceptanceEvidenceGraph
from src.payload_store import PayloadStore, mint_reference
from src.services import (
    AuthorizedResourceResolver,
    InMemoryEngagementDb,
    InMemoryEngagementKb,
    ResourceScope,
    normalize_query,
)


class ValidLlm:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        self.prompts.append(messages[0]["content"])
        return {
            "content": json.dumps(
                {
                    "candidate_conflicts": [
                        {
                            "description": "candidate evidence conflict",
                            "clause_id": "clausea",
                            "source_ids": ["sourcea"],
                            "severity": "candidate",
                            "confidence": 0.5,
                        }
                    ],
                    "gaps": [],
                    "verification_questions": [],
                }
            )
        }


class SecretExceptionLlm:
    def complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        raise RuntimeError("provider-secret-sentinel")


class NestedRawLlm:
    def complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "content": json.dumps(
                {
                    "candidate_conflicts": [
                        {
                            "description": {"raw": "raw-document-sentinel"},
                            "clause_id": "clausea",
                            "source_ids": ["sourcea"],
                            "severity": "candidate",
                            "confidence": 0.5,
                        }
                    ],
                    "gaps": [],
                    "verification_questions": [],
                }
            )
        }


def _scope(customer: str, *, kb: Any | None = None) -> ResourceScope:
    db = InMemoryEngagementDb(
        editions={
            "policyalpha": {
                "profession_profile": "audit_firm",
                "clauses": [
                    {
                        "clause_id": "clausea",
                        "clause_ref": "A",
                        "required_doc_type": "kyc",
                    }
                ],
            }
        }
    )
    return ResourceScope(
        customer,
        db,
        kb or InMemoryEngagementKb(),
        PayloadStore(scope_keys=("customer_ref",)),
    )


def _auth(customer: str = "customera", caller: str = "callera") -> AuthContext:
    return AuthContext(
        trust=TrustLevel.VERIFIED_EXTERNAL,
        scope={"customer_ref": customer},
        principal={"caller_id": caller},
        auth_source=AuthSource.MIDDLEWARE,
    )


def _invoke_body() -> dict[str, Any]:
    return {
        "customer_id": "customera",
        "policy_edition_ref": "policyalpha",
        "case_narrative": "distinctive engagement facts",
        "evidence_declarations": [
            {
                "source_id": "sourcea",
                "doc_type": "kyc",
                "doc_ref": "doc",
                "span_ref": "line1",
            }
        ],
    }


def _graph_invoke(graph: EngagementAcceptanceEvidenceGraph, scope: ResourceScope) -> dict:
    session_id = mint_reference()
    payload_ref = scope.payloads.put(
        _invoke_body(),
        scope={"customer_ref": scope.customer_ref},
        session_id=session_id,
    )
    packet = {
        "mode": "invoke",
        "customer_id": scope.customer_ref,
        "customer_ref": scope.customer_ref,
        "case_ref": mint_reference(),
        "payload_ref": payload_ref,
        "policy_edition_ref": "policyalpha",
    }
    return graph.invoke(
        json.dumps(packet),
        ctx=InvocationContext(
            session_id=session_id,
            thread_id=session_id,
            caller_id="callera",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        ),
    )


def _configure_http(monkeypatch, scope: ResourceScope, llm: Any) -> list[Operation]:
    operations: list[Operation] = []

    def authenticate(_request, *, operation: Operation) -> AuthContext:
        operations.append(operation)
        return _auth(scope.customer_ref, "callera")

    monkeypatch.setattr(server, "authenticate", authenticate)
    server.app.state.resource_scopes = {"callera": scope}
    server.app.state.llm = llm
    return operations


def test_real_http_composition_runs_invoke_ingest_and_feedback(monkeypatch):
    scope = _scope("customera")
    operations = _configure_http(monkeypatch, scope, ValidLlm())

    with TestClient(server.app) as client:
        invoked = client.post("/invoke", json=_invoke_body())
        assert invoked.status_code == 200
        claim = invoked.json()["output"]["candidate_conflicts"][0]
        case_ref = invoked.json()["case_ref"]

        ingested = client.post(
            "/ingest",
            json={
                "customer_id": "customera",
                "policy_edition_ref": "policyalpha",
                "records": [
                    {
                        "policy_edition_ref": "policyalpha",
                        "clause_id": "clausea",
                        "consultation_summary": "distinctive engagement facts",
                        "decision_outcome": "accepted",
                        "decision_rationale": "documented rationale",
                        "perspective_code": "conflict",
                    }
                ],
            },
        )
        assert ingested.status_code == 200
        assert ingested.json()["output"]["ingest_summary"]["written_ids"]

        feedback = client.post(
            "/feedback",
            json={
                "customer_id": "customera",
                "case_ref": case_ref,
                "claim_id": claim["claim_id"],
                "perspective_code": "conflict",
                "clause_id": "clausea",
                "policy_edition_ref": "policyalpha",
                "source_ids": ["sourcea"],
                "decision_outcome": "accepted",
                "decision_rationale": "reviewed rationale",
                "action": "approve",
                "approved": True,
            },
        )
        assert feedback.status_code == 200
        assert feedback.json()["output"]["feedback_summary"]["applied_count"] == 1

    assert operations == [
        Operation.WRITE_INGEST,
        Operation.WRITE_INGEST,
        Operation.WRITE_FEEDBACK,
    ]
    assert not scope.payloads._items, "raw HTTP bodies were not deleted in finally"


def test_invoke_is_write_classified_and_standalone_token_cannot_persist(monkeypatch):
    scope = _scope("customera")
    server.app.state.resource_scopes = {"invoke-token:standalone": scope}
    server.app.state.llm = ValidLlm()
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_CUSTOMER_REF", "customera")

    with TestClient(server.app) as client:
        response = client.post(
            "/invoke",
            json=_invoke_body(),
            headers={"authorization": "Bearer mock-entry-token"},
        )

    assert response.status_code == 403
    assert scope.db.cases == {}


def test_http_invalid_policy_edition_pre_put_leaves_no_payload_or_tracking(monkeypatch):
    scope = _scope("customera")
    _configure_http(monkeypatch, scope, ValidLlm())
    body = _invoke_body()
    body["policy_edition_ref"] = ""

    with TestClient(server.app) as client:
        response = client.post("/invoke", json=body)

    assert response.status_code == 400
    assert response.json()["detail"] == "policy_edition_invalid"
    assert scope.payloads._items == {}
    assert server.app.state.case_payload_refs == {}
    assert scope.db.cases == {}


def test_http_post_put_contract_failure_deletes_payload_and_leaves_no_tracking(monkeypatch):
    scope = _scope("customera")
    _configure_http(monkeypatch, scope, NestedRawLlm())

    with TestClient(server.app) as client:
        response = client.post("/invoke", json=_invoke_body())

    assert response.status_code == 400
    assert "raw-document-sentinel" not in response.text
    assert scope.payloads._items == {}
    assert server.app.state.case_payload_refs == {}
    assert scope.db.claim_snapshots == {}


def test_full_graph_rejects_nested_raw_llm_value_before_customer_output():
    scope = _scope("customera")
    graph = EngagementAcceptanceEvidenceGraph(
        {"resource_resolver": AuthorizedResourceResolver({"callera": scope}), "llm": NestedRawLlm()}
    )
    graph.compile()

    result = _graph_invoke(graph, scope)

    assert result["status"] == "error"
    assert "raw-document-sentinel" not in json.dumps(result)
    assert scope.db.claim_snapshots == {}


def test_full_graph_llm_exception_exposes_only_stable_error_metadata():
    scope = _scope("customera")
    graph = EngagementAcceptanceEvidenceGraph(
        {"resource_resolver": AuthorizedResourceResolver({"callera": scope}), "llm": SecretExceptionLlm()}
    )
    graph.compile()

    result = _graph_invoke(graph, scope)

    assert result["status"] == "error"
    assert result["error_code"] == "llm_call_failed"
    assert "provider-secret-sentinel" not in json.dumps(result)


def test_http_ingest_batch_failure_rolls_back_every_row(monkeypatch):
    class FailingSecondKb(InMemoryEngagementKb):
        def upsert(self, projection: dict[str, Any]) -> str:
            if len(self.projections) == 1:
                raise RuntimeError("backend detail must not escape")
            return super().upsert(projection)

    scope = _scope("customera", kb=FailingSecondKb())
    _configure_http(monkeypatch, scope, ValidLlm())
    records = [
        {
            "policy_edition_ref": "policyalpha",
            "clause_id": "clausea",
            "consultation_summary": f"summary {index}",
            "decision_outcome": "accepted",
            "decision_rationale": "documented rationale",
            "perspective_code": "conflict",
        }
        for index in range(2)
    ]

    with TestClient(server.app) as client:
        response = client.post(
            "/ingest",
            json={
                "customer_id": "customera",
                "policy_edition_ref": "policyalpha",
                "records": records,
            },
        )

    assert response.status_code == 200
    summary = response.json()["output"]["ingest_summary"]
    assert summary["written_ids"] == []
    assert summary["rejected_count"] == 2
    assert scope.db.perspectives == []
    assert scope.kb.projections == []


def test_http_feedback_cannot_reuse_another_customer_case_and_claim_ids(monkeypatch):
    scope_a = _scope("customera")
    scope_b = _scope("customerb")
    foreign_case = mint_reference()
    foreign_claim = mint_reference()
    scope_b.db.save_case(foreign_case, "policyalpha", [{"source_id": "sourcea"}])
    scope_b.db.save_claim_snapshot(
        {
            "claim_id": foreign_claim,
            "case_ref": foreign_case,
            "customer_ref": "customerb",
            "policy_edition_ref": "policyalpha",
            "clause_id": "clausea",
            "source_ids": ["sourcea"],
            "text": "foreign snapshot",
        }
    )
    monkeypatch.setattr(
        server,
        "authenticate",
        lambda _request, *, operation: _auth("customera", "callera"),
    )
    server.app.state.resource_scopes = {"callera": scope_a, "callerb": scope_b}
    server.app.state.llm = ValidLlm()

    with TestClient(server.app) as client:
        response = client.post(
            "/feedback",
            json={
                "customer_id": "customera",
                "case_ref": foreign_case,
                "claim_id": foreign_claim,
                "perspective_code": "conflict",
                "clause_id": "clausea",
                "policy_edition_ref": "policyalpha",
                "source_ids": ["sourcea"],
                "decision_outcome": "accepted",
                "decision_rationale": "reviewed rationale",
                "action": "approve",
                "approved": True,
            },
        )

    assert response.status_code == 400
    assert scope_a.db.perspectives == []
    assert scope_b.db.claim_snapshot(foreign_claim) is not None


class ScopedSqliteKb:
    """A customer-bound adapter over one shared, real SQLite table."""

    def __init__(self, connection: sqlite3.Connection, customer_ref: str) -> None:
        self.connection = connection
        self.customer_ref = customer_ref

    def search(self, retrieval_text: str, *, policy_edition_ref: str, top_k: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT history_ref, case_ref, policy_edition_ref, retrieval_text "
            "FROM perspectives WHERE customer_ref = ? AND policy_edition_ref = ?",
            (self.customer_ref, policy_edition_ref),
        ).fetchall()
        query_terms = set(normalize_query(retrieval_text).split())
        ranked = sorted(
            rows,
            key=lambda row: len(query_terms & set(normalize_query(row[3]).split())),
            reverse=True,
        )
        return [
            {
                "history_ref": row[0],
                "case_ref": row[1],
                "policy_edition_ref": row[2],
                "retrieval_text": row[3],
            }
            for row in ranked[:top_k]
        ]

    def upsert(self, projection: dict[str, Any]) -> str:
        self.connection.execute(
            "INSERT INTO perspectives VALUES (?, ?, ?, ?, ?)",
            (
                self.customer_ref,
                projection["history_ref"],
                projection["case_ref"],
                projection["policy_edition_ref"],
                projection["retrieval_text"],
            ),
        )
        self.connection.commit()
        return str(projection["history_ref"])

    def delete_history(self, history_ref: str) -> None:
        self.connection.execute(
            "DELETE FROM perspectives WHERE customer_ref = ? AND history_ref = ?",
            (self.customer_ref, history_ref),
        )
        self.connection.commit()

    def delete_case(self, case_ref: str) -> int:
        cursor = self.connection.execute(
            "DELETE FROM perspectives WHERE customer_ref = ? AND case_ref = ?",
            (self.customer_ref, case_ref),
        )
        self.connection.commit()
        return cursor.rowcount


def test_real_sqlite_scope_projection_hits_one_saved_record_and_uses_business_query():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE perspectives (customer_ref TEXT, history_ref TEXT, case_ref TEXT, "
        "policy_edition_ref TEXT, retrieval_text TEXT)"
    )
    connection.executemany(
        "INSERT INTO perspectives VALUES (?, ?, ?, ?, ?)",
        [
            ("customera", "hist-a-match", "case-a", "policyalpha", "clausea distinctive engagement facts other"),
            ("customera", "hist-a-cold", "case-cold", "policyalpha", "clausea unrelated matter other"),
            ("customerb", "hist-b-forbidden", "case-b", "policyalpha", "clausea distinctive engagement facts other"),
        ],
    )
    llm = ValidLlm()
    scope = _scope("customera", kb=ScopedSqliteKb(connection, "customera"))
    graph = EngagementAcceptanceEvidenceGraph(
        {
            "resource_resolver": AuthorizedResourceResolver({"callera": scope}),
            "llm": llm,
            "top_k": 1,
        }
    )
    graph.compile()

    result = _graph_invoke(graph, scope)

    assert result["status"] == "success"
    assert "hist-a-match" in llm.prompts[0]
    assert "hist-a-cold" not in llm.prompts[0]
    assert "hist-b-forbidden" not in llm.prompts[0]
