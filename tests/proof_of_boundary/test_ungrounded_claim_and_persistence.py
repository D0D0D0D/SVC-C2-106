"""無根拠 claim の拒否と、失敗時に永続化を残さないことを固定する。

由来: 2026-08-13 の独立レビュー（Batch 3）。レビュアーの再現 probe をそのまま回帰テスト化。

H5: `project_response()` が `source_ids=[]` を許可し、S-3 の部分集合検査も空集合を通すため、
    **根拠ゼロの claim が `status=success` で出力され、feedback 用 snapshot にも保存される**。

Medium: `DeterministicCheckNode` が LLM・S-3 より前に `save_case()` するため、
    **最終結果が error でも case と evidence が永続化されたまま残る**。
"""
from __future__ import annotations

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import EngagementAcceptanceEvidenceGraph
from src.payload_store import PayloadStore, mint_reference
from src.services import (
    AuthorizedResourceResolver, InMemoryEngagementDb, InMemoryEngagementKb, ResourceScope,
)

_EDITIONS = {"policyalpha": {"profession_profile": "audit_firm",
                             "clauses": [{"clause_id": "clausea", "clause_ref": "A",
                                          "required_doc_type": "kyc"}]}}


def _run(llm) -> tuple[dict, ResourceScope, str]:
    scope = ResourceScope("customera", InMemoryEngagementDb(editions=_EDITIONS),
                          InMemoryEngagementKb(), PayloadStore(scope_keys=("customer_ref",)))
    graph = EngagementAcceptanceEvidenceGraph({
        "resource_resolver": AuthorizedResourceResolver({"callera": scope}), "llm": llm})
    graph.compile()
    session_id, case_ref = mint_reference(), mint_reference()
    payload_ref = scope.payloads.put({
        "customer_id": "customera", "policy_edition_ref": "policyalpha",
        "case_narrative": "distinctive engagement facts",
        "evidence_declarations": [{"source_id": "sourcea", "doc_type": "kyc",
                                   "doc_ref": "doc", "span_ref": "line1"}],
    }, scope={"customer_ref": scope.customer_ref}, session_id=session_id)
    result = graph.invoke(json.dumps({
        "mode": "invoke", "customer_id": scope.customer_ref, "customer_ref": scope.customer_ref,
        "case_ref": case_ref, "payload_ref": payload_ref, "policy_edition_ref": "policyalpha",
    }), ctx=InvocationContext(session_id=session_id, thread_id=session_id, caller_id="callera",
                              caller_trust_level=TrustLevel.VERIFIED_EXTERNAL))
    return result, scope, case_ref


class _EmptySourceLlm:
    """根拠ゼロの claim を返す。"""
    def complete(self, _messages):
        return {"content": json.dumps({
            "candidate_conflicts": [{"description": "Ungrounded conflict from prior tendency",
                                     "clause_id": "clausea", "source_ids": [],
                                     "severity": "candidate", "confidence": 0.5}],
            "gaps": [], "verification_questions": []})}


class _NestedRawLlm:
    """S-3 が拒否する nested 値を返す（永続化の後始末を見るため）。"""
    def complete(self, _messages):
        return {"content": json.dumps({
            "candidate_conflicts": [{"description": {"raw": "raw-document-sentinel"},
                                     "clause_id": "clausea", "source_ids": ["sourcea"],
                                     "severity": "candidate", "confidence": 0.5}],
            "gaps": [], "verification_questions": []})}


def test_a_claim_without_any_source_is_rejected():
    """根拠ゼロの claim が出力にも snapshot にも残らないこと。"""
    result, scope, _ = _run(_EmptySourceLlm())
    assert result.get("status") == "error", (
        f"source_ids=[] の無根拠 claim が success で通った: {result.get('status')}"
    )
    assert not scope.db.claim_snapshots, (
        f"無根拠 claim が snapshot に保存された: {scope.db.claim_snapshots}"
    )


def test_nothing_is_persisted_when_the_run_fails():
    """S-3 が拒否した実行で case / evidence が残らないこと。"""
    result, scope, case_ref = _run(_NestedRawLlm())
    assert result.get("status") == "error"
    assert case_ref not in scope.db.cases, f"失敗した実行の case が永続化された: {scope.db.cases}"
    assert case_ref not in scope.db.evidence, f"失敗した実行の evidence が永続化された: {scope.db.evidence}"
