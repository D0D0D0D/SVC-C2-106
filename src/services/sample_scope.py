"""Bundled customer partition for the AGENTIC STAR Marketplace deployment.

The Marketplace entry point has no hosting layer to hand it
``application.state.resource_scopes``, which is what ``src/api/server.py``
requires. Without a scope the adapter built an empty
``AuthorizedResourceResolver()``, so ``for_caller()`` raised
``authorization_unresolved`` for every request and the agent could not serve
anyone at all.

This module builds one fictional customer partition from
``config/engagement_scope.sample.json`` -- the same "bundle a sample so the
deployment has something to read" approach other agent templates use. The
data is clearly marked and is not a substitute for a real policy edition:
``docs/07_operation_guide.md`` records what to replace before production use.

Loading is fail-closed. A malformed or unmarked sample raises at startup rather
than yielding a silently empty partition, because an empty partition looks
exactly like "this customer has no history" to the graph.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.services.service import (
    InMemoryEngagementDb,
    InMemoryEngagementKb,
    ResourceScope,
    build_retrieval_text,
)

_SAMPLE_FILE = "engagement_scope.sample.json"
_SAMPLE_MARKER = "SAMPLE-ENGAGEMENT-SCOPE"
_CLAUSE_KEYS = {"clause_id", "clause_ref", "required_doc_type"}
_HISTORY_KEYS = {
    "history_ref",
    "case_ref",
    "clause_id",
    "perspective_code",
    "consultation_summary",
    "decision_outcome",
    "decision_rationale_safe",
}


def _config_dir(config_dir: Path | None = None) -> Path:
    return config_dir or Path(__file__).resolve().parents[2] / "config"


def load_sample_scope(*, config_dir: Path | None = None) -> dict[str, Any]:
    """Read and validate the bundled partition; never return a partial one."""

    path = _config_dir(config_dir) / _SAMPLE_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Marketplace sample scope is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Marketplace sample scope must be an object: {path}")
    if _SAMPLE_MARKER not in str(value.get("sample_notice", "")):
        raise RuntimeError(
            "Marketplace sample scope is missing its sample identity — refusing to "
            "present unmarked data as a customer partition"
        )
    for key in ("policy_edition_ref", "profession_profile"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise RuntimeError(f"Marketplace sample scope {key!r} must be a non-empty string")
    clauses = value.get("clauses")
    if not isinstance(clauses, list) or not clauses:
        raise RuntimeError("Marketplace sample scope must declare at least one clause")
    for clause in clauses:
        if not isinstance(clause, dict) or set(clause) != _CLAUSE_KEYS:
            raise RuntimeError(f"Marketplace sample clause must declare exactly {sorted(_CLAUSE_KEYS)}")
        if not all(isinstance(clause[k], str) and clause[k].strip() for k in _CLAUSE_KEYS):
            raise RuntimeError("Marketplace sample clause fields must be non-empty strings")
    history = value.get("history")
    if not isinstance(history, list):
        raise RuntimeError("Marketplace sample scope history must be a list")
    clause_ids = {clause["clause_id"] for clause in clauses}
    for entry in history:
        if not isinstance(entry, dict) or set(entry) != _HISTORY_KEYS:
            raise RuntimeError(f"Marketplace sample history must declare exactly {sorted(_HISTORY_KEYS)}")
        if not all(isinstance(entry[k], str) and entry[k].strip() for k in _HISTORY_KEYS):
            raise RuntimeError("Marketplace sample history fields must be non-empty strings")
        if entry["clause_id"] not in clause_ids:
            raise RuntimeError(
                f"Marketplace sample history references an unknown clause: {entry['clause_id']!r}"
            )
    return value


def sample_policy_edition_ref(*, config_dir: Path | None = None) -> str:
    """The edition a Marketplace caller has to name in its packet."""

    return str(load_sample_scope(config_dir=config_dir)["policy_edition_ref"])


def build_sample_scope(customer_ref: str, payloads: Any, *, config_dir: Path | None = None) -> ResourceScope:
    """Assemble the bundled partition for one customer.

    ``payloads`` is supplied rather than created here so the adapter can stage a
    request body into the very store the graph will resolve it from.
    """

    if not isinstance(customer_ref, str) or not customer_ref.strip():
        raise RuntimeError("Marketplace sample scope requires a customer_ref")
    sample = load_sample_scope(config_dir=config_dir)
    edition = sample["policy_edition_ref"]
    db = InMemoryEngagementDb(
        editions={
            edition: {
                "profession_profile": sample["profession_profile"],
                "clauses": [dict(clause) for clause in sample["clauses"]],
            }
        }
    )
    kb = InMemoryEngagementKb()
    for entry in sample["history"]:
        kb.upsert(
            {
                **entry,
                "policy_edition_ref": edition,
                "retrieval_text": build_retrieval_text(
                    entry["clause_id"], entry["consultation_summary"], entry["perspective_code"]
                ),
            }
        )
    return ResourceScope(customer_ref, db, kb, payloads)
