"""Customer-isolated persistence and sanitisation seams.

The in-memory implementations are intentionally small test/STG adapters.  A
deployment wires real DB/KB handles into :class:`AuthorizedResourceResolver`;
nodes never retain a resolved scope between calls.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Protocol

from shared.security.pii_detector import detect_pii


DECISION_OUTCOMES = frozenset({"accepted", "declined", "accepted_with_safeguards", "withdrawn"})
PERSPECTIVE_CODES = frozenset({"conflict", "independence", "kyc", "scope", "other"})
_INSTRUCTION = re.compile(r"ignore\s+previous|以後の指示|システムプロンプト")


class AuthorizationError(PermissionError):
    """Caller-to-customer mapping is absent or cannot be used."""


class BackendUnavailableError(RuntimeError):
    """A provisioned DB/KB handle cannot service the request."""


class EngagementDbClient(Protocol):
    def policy_clauses(self, policy_edition_ref: str) -> list[dict[str, Any]]: ...
    def policy_profile(self, policy_edition_ref: str) -> str | None: ...
    def save_case(self, case_ref: str, policy_edition_ref: str, evidence: list[dict[str, Any]]) -> None: ...
    def evidence_records(self, case_ref: str) -> list[dict[str, Any]]: ...
    def save_perspective(self, record: dict[str, Any]) -> str: ...
    def delete_perspective(self, perspective_id: str) -> None: ...
    def audit(self, event: dict[str, Any]) -> str: ...
    def save_claim_snapshot(self, snapshot: dict[str, Any]) -> None: ...
    def claim_snapshot(self, claim_id: str) -> dict[str, Any] | None: ...
    def delete_claim_snapshot(self, claim_id: str) -> None: ...
    def delete_case(self, case_ref: str) -> int: ...


class EngagementKbClient(Protocol):
    def search(self, retrieval_text: str, *, policy_edition_ref: str, top_k: int) -> list[dict[str, Any]]: ...
    def upsert(self, projection: dict[str, Any]) -> str: ...
    def delete_history(self, history_ref: str) -> None: ...
    def delete_case(self, case_ref: str) -> int: ...


@dataclass
class InMemoryEngagementDb:
    editions: dict[str, dict[str, Any]] = field(default_factory=dict)
    cases: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    perspectives: list[dict[str, Any]] = field(default_factory=list)
    audits: list[dict[str, Any]] = field(default_factory=list)
    claim_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)

    def policy_clauses(self, policy_edition_ref: str) -> list[dict[str, Any]]:
        edition = self.editions.get(policy_edition_ref)
        return list(edition.get("clauses", [])) if edition else []

    def policy_profile(self, policy_edition_ref: str) -> str | None:
        edition = self.editions.get(policy_edition_ref)
        return edition.get("profession_profile") if edition else None

    def save_case(self, case_ref: str, policy_edition_ref: str, evidence: list[dict[str, Any]]) -> None:
        self.cases[case_ref] = {"policy_edition_ref": policy_edition_ref}
        self.evidence[case_ref] = [dict(record) for record in evidence]

    def evidence_records(self, case_ref: str) -> list[dict[str, Any]]:
        return [dict(record) for record in self.evidence.get(case_ref, [])]

    def save_perspective(self, record: dict[str, Any]) -> str:
        perspective_id = f"perspective-{len(self.perspectives) + 1}"
        self.perspectives.append({**record, "perspective_id": perspective_id})
        return perspective_id

    def delete_perspective(self, perspective_id: str) -> None:
        self.perspectives[:] = [row for row in self.perspectives if row.get("perspective_id") != perspective_id]

    def audit(self, event: dict[str, Any]) -> str:
        event_id = f"audit-{len(self.audits) + 1}"
        self.audits.append({**event, "event_id": event_id})
        return event_id

    def save_claim_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.claim_snapshots[str(snapshot["claim_id"])] = dict(snapshot)

    def claim_snapshot(self, claim_id: str) -> dict[str, Any] | None:
        snapshot = self.claim_snapshots.get(claim_id)
        return dict(snapshot) if snapshot is not None else None

    def delete_claim_snapshot(self, claim_id: str) -> None:
        self.claim_snapshots.pop(claim_id, None)

    def delete_case(self, case_ref: str) -> int:
        count = int(self.cases.pop(case_ref, None) is not None)
        count += len(self.evidence.pop(case_ref, []))
        before = len(self.perspectives)
        self.perspectives[:] = [row for row in self.perspectives if row.get("case_ref") != case_ref]
        self.claim_snapshots = {
            claim_id: snapshot
            for claim_id, snapshot in self.claim_snapshots.items()
            if snapshot.get("case_ref") != case_ref
        }
        return count + before - len(self.perspectives)


@dataclass
class InMemoryEngagementKb:
    projections: list[dict[str, Any]] = field(default_factory=list)

    def search(self, retrieval_text: str, *, policy_edition_ref: str, top_k: int) -> list[dict[str, Any]]:
        # The only searchable representation is retrieval_text.  Outcome and
        # rationale are display payloads and cannot affect this ordering.
        query = normalize_query(retrieval_text)
        ranked = [
            row for row in self.projections
            if row.get("policy_edition_ref") == policy_edition_ref
        ]
        ranked.sort(key=lambda row: _overlap(query, normalize_query(row.get("retrieval_text", ""))), reverse=True)
        return [dict(row) for row in ranked[:top_k]]

    def upsert(self, projection: dict[str, Any]) -> str:
        self.projections.append(dict(projection))
        return str(projection["history_ref"])

    def delete_history(self, history_ref: str) -> None:
        self.projections[:] = [row for row in self.projections if row.get("history_ref") != history_ref]

    def delete_case(self, case_ref: str) -> int:
        before = len(self.projections)
        self.projections[:] = [row for row in self.projections if row.get("case_ref") != case_ref]
        return before - len(self.projections)


@dataclass(frozen=True)
class ResourceScope:
    customer_ref: str
    db: EngagementDbClient
    kb: EngagementKbClient
    payloads: Any


class AuthorizedResourceResolver:
    """Resolve only provisioned, customer-specific resources; no fallback."""

    def __init__(self, caller_scopes: dict[str, ResourceScope] | None = None) -> None:
        self._caller_scopes = dict(caller_scopes or {})

    def for_caller(self, caller_id: str) -> ResourceScope:
        scope = self._caller_scopes.get(caller_id)
        if scope is None:
            raise AuthorizationError("authorization_unresolved")
        return scope


def normalize_query(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def build_retrieval_text(clause_id: str, consultation_summary: str, perspective_code: str) -> str:
    return normalize_query(f"{clause_id} {consultation_summary} {perspective_code}")


def make_source_registry(case_ref: str, policy_edition_ref: str, records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    registry: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        source_id = str(record.get("source_id", ""))
        if not source_id:
            continue
        registry[source_id] = {
            "doc_ref": record.get("doc_ref", ""),
            "span_ref": record.get("span_ref", ""),
            "provenance": {
                "case_ref": case_ref,
                "record_id": record.get("record_id", source_id),
                "doc_ref": record.get("doc_ref", ""),
                "span_ref": record.get("span_ref", ""),
                "policy_edition_ref": policy_edition_ref,
            },
            "doc_type": record.get("doc_type", "unknown"),
            "_index": index,
        }
    return registry


def sanitize_decision_rationale(value: str, max_chars: int) -> tuple[str, str, str]:
    if not isinstance(value, str) or len(value) > max_chars:
        raise ValueError("decision_rationale_invalid_length")
    normalized = unicodedata.normalize("NFKC", value)
    if _INSTRUCTION.search(normalized.casefold()):
        raise ValueError("decision_rationale_instruction_detected")
    audit = normalized
    findings = detect_pii(normalized)
    for finding in reversed(findings):
        audit = audit[: finding["start"]] + "[MASKED]" + audit[finding["end"] :]
    # `_audit` preserves the complete PII-masked normalized record for audit.
    # `_safe` is an independently derived bounded model projection.  Most
    # accepted inputs are already within the bound, but NFKC expansion can make
    # this slice materially different without weakening the raw-input 400 rule.
    safe = audit[:max_chars]
    return normalized, audit, safe


def _overlap(left: str, right: str) -> int:
    return len(set(left.split()) & set(right.split()))
