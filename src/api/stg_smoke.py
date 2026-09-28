"""Stage 5 smoke provisioning — CI-job-bound, inert everywhere else.

`wire_services()` deliberately refuses to invent a customer scope or an LLM: a
deployment must supply real, customer-specific handles.  The `deploy-stg` job,
however, boots `uvicorn src.api.server:app` with nothing injected, so the agent
cannot even answer `GET /health` there.

This module supplies one placeholder caller/customer plus in-memory stand-ins,
and only inside the ephemeral provisional smoke job.  Outside that job every
entry point returns without touching anything, so the shipped fail-closed
posture is unchanged for real deployments.

Bearer-token presence is NOT a staging discriminator — a standalone production
deployment sets `INVOKE_AUTH_TOKEN` too.  The guard is CI provenance instead.
"""

from __future__ import annotations

import json
import os
from typing import Any

from src.entry_adapter.auth import _valid_scope_value
from src.payload_store import PayloadStore
from src.services import (
    InMemoryEngagementDb,
    InMemoryEngagementKb,
    ResourceScope,
)


# The smoke edition and case the canonical payload refers to.  Both are reserved
# values that must never name a real policy edition or engagement.
STG_SMOKE_EDITION = "stgsmokeedition"


# The synthetic source the canonical smoke packet declares. Citing it makes the
# S-3 gate do real work: an empty brief would satisfy every per-row check
# vacuously, so a PASS would prove only that the envelope had no rows to reject.
STG_SMOKE_SOURCE_ID = "stgsmokesource"
STG_SMOKE_CLAUSE_ID = "stgsmokeclause"


class StgSmokeLlm:
    """Deterministic stand-in for the injected `BaseLLM` seam.

    The design makes the LLM mandatory at startup (`LLM_API_KEY` is a declared
    secret), so the smoke job must inject *something* rather than lean on the
    runtime degradation path.

    The returned draft cites the declared synthetic source so the envelope
    carries a populated `evidence_sources` row and a claim referencing it —
    exercising the provenance registry match, the current-case/edition scope
    check and the claim -> source existence check for real.  `claim_id` is
    omitted on purpose: the workflow mints it, and supplying one here would test
    the wrong contract.
    """

    def complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        draft = {
            "candidate_conflicts": [],
            "gaps": [
                {
                    "description": "STG smoke gap — reserved synthetic finding, never a real assessment.",
                    "clause_id": STG_SMOKE_CLAUSE_ID,
                    "source_ids": [STG_SMOKE_SOURCE_ID],
                }
            ],
            "verification_questions": [],
        }
        return {
            "content": json.dumps(draft, ensure_ascii=False),
            "model": "stg-smoke-deterministic",
            "usage": {},
        }


def is_provisional_stg_smoke_job() -> bool:
    """True only inside the ephemeral `deploy-stg` provisional smoke job.

    Requires the provenance that only that CI job carries: a GitLab runner
    (`CI` and `GITLAB_CI`), the job named exactly `deploy-stg`, a runner-issued
    per-job token and job id, and the job-scoped `STG_MOCK_MODE=true` that marks
    the in-job ephemeral deploy.

    Known limit — this is process environment, not a cryptographic attestation.
    A deployment that sets every one of these by hand reproduces it, so the
    guard's job is to make that unmistakably deliberate rather than an accident
    of a leaked `STG_DEFAULT_*`.  A stronger binding (a CI-only entrypoint, or
    verifying a GitLab OIDC token) is not available to a template: the
    shared CI configuration hard-codes `uvicorn src.api.server:app` and
    declares no `id_tokens`, so neither the process nor a JWT is ours to choose.
    What keeps this bounded is the blast radius, not the guard: everything it
    binds is in-memory and freshly constructed, so a mistaken activation yields
    an agent serving a reserved placeholder customer with no path to real data.
    """
    return (
        os.environ.get("CI") == "true"
        and os.environ.get("GITLAB_CI") == "true"
        and os.environ.get("CI_JOB_NAME") == "deploy-stg"
        and os.environ.get("STG_MOCK_MODE") == "true"
        # Runner-issued, single-job values: present in a real job, absent from a
        # hand-written deployment environment unless copied deliberately.
        and bool(os.environ.get("CI_JOB_TOKEN"))
        and bool(os.environ.get("CI_JOB_ID"))
    )


def provision(application: Any) -> None:
    """Bind one placeholder scope + LLM onto `application.state`, smoke job only.

    Guards (all required):
      * the process is the provisional `deploy-stg` smoke job;
      * `INVOKE_AUTH_TOKEN` is set, so the smoke request is still authenticated;
      * `STG_DEFAULT_CUSTOMER_REF` is a clean value — same contract the entry
        adapter applies to a verified scope, so a padded or blank setting is a
        deployment error that keeps failing closed at startup rather than
        booting into a 403 further down;
      * `resource_scopes` and `llm` are both absent. A wired — or partially
        wired — deployment is never overwritten.
    """
    if not is_provisional_stg_smoke_job():
        return
    if not os.environ.get("INVOKE_AUTH_TOKEN"):
        return
    customer = os.environ.get("STG_DEFAULT_CUSTOMER_REF", "")
    # Reuse the entry adapter's own predicate rather than restating it: a value
    # this provisioning accepts but `authenticate_scope` later rejects would boot
    # into a guaranteed 403 instead of failing at startup.
    if not _valid_scope_value(customer):
        return
    # The token-auth entry path has no middleware-supplied caller id, so
    # `_caller()` synthesises `stg:<customer_ref>` and rejects that prefix from
    # any other source. Key the smoke scope by exactly that value — inventing a
    # separate STG_DEFAULT_CALLER_ID would never be looked up.
    caller = f"stg:{customer}"
    # Only provision onto a blank slate. A partially wired app is a deployment
    # error and must keep failing closed rather than be rescued here.
    if getattr(application.state, "resource_scopes", None) is not None:
        return
    if getattr(application.state, "llm", None) is not None:
        return

    db = InMemoryEngagementDb()
    db.editions[STG_SMOKE_EDITION] = {
        "clauses": [
            {
                "clause_id": STG_SMOKE_CLAUSE_ID,
                "text": "STG smoke clause — reserved, never a real acceptance policy provision.",
                "policy_edition_ref": STG_SMOKE_EDITION,
            }
        ],
        "profession_profile": "audit_firm",
    }
    application.state.resource_scopes = {
        caller: ResourceScope(
            customer_ref=customer,
            db=db,
            kb=InMemoryEngagementKb(),
            payloads=PayloadStore(scope_keys=("customer_ref",)),
        )
    }
    application.state.llm = StgSmokeLlm()
