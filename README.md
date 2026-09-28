# SVC-C2-106 — Professional Services Engagement Acceptance Evidence Agent

> **Category**: Cat 2 (domain workflow)
> **Industry**: SVC

## Overview

Before an audit firm, a law firm, or a consultancy takes on a new matter, someone has to work
through the acceptance pack — who the client is, what the engagement actually involves, which
parties are connected to it, the KYC material, the independence and conflict-check declarations —
and hold all of it against the firm's own approved acceptance policy.

This agent prepares that comparison. Given the acceptance pack and one approved policy edition, it
returns three things, each citing the clause and the source documents behind it:

- **candidate conflicts** — where the pack appears to run into a policy clause;
- **gaps** — where a clause asks for something the pack does not supply;
- **verification questions** — what to put back to the requesting partner before deciding.

What it will not do is decide. There is no acceptance verdict in the output, no legal advice, no
staffing recommendation, and no final determination of a conflict, an independence threat, or an
AML risk rating. This is not a matter of prompt instruction — **those fields do not exist in the
output schema**, and a response carrying one is rejected by the output gate rather than trimmed.
The `severity` field on a candidate conflict accepts exactly one value, `"candidate"`: the schema
has no way to express a settled finding. The decision, and the record of it, stay with the
responsible qualified professional.

The agent accumulates. Three modes share one graph:

| Mode | Purpose |
|---|---|
| `POST /ingest` | Store the firm's approved acceptance policy and sanitized perspectives from past reviews |
| `POST /invoke` | Draft the non-conclusive evidence brief for a new matter |
| `POST /feedback` | Record a perspective a human confirmed or edited, with explicit approval and an audit event, so it joins the accumulated set |
| `DELETE /cases/{case_ref}` | Erase a case — database rows, knowledge-base documents, and any staged payloads |

Two properties are structural, and they shape the whole design:

- **Every customer is a separate connection, not a filtered row.** A caller's identity resolves to
  a `ResourceScope` carrying that customer's own database, knowledge base, and payload store. The
  graph and its nodes hold only the customer-independent resolver; the handles are fetched per
  invocation. There is no tenant column to forget to filter on, and no cached connection that
  could be reused for the next caller.
- **The request body never enters graph state.** The HTTP adapter scrubs the incoming JSON, stages
  the raw body in the customer's payload store under a server-minted reference, and hands the
  graph a control envelope. The staged body is deleted in a `finally` block, on the success and
  failure path alike. A caller-supplied `payload_ref` is ignored.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to your
own policy and data, and run it inside your own AGENTIC STAR deployment.

## Integration

Unlike a template that ships defaults, this one **refuses to invent a customer**. `wire_services()`
requires the hosting layer to supply both of these before the first request, and raises at start-up
if either is absent:

| Required | Supplied as |
|---|---|
| `application.state.resource_scopes` | A dict keyed by verified caller id, each value a `ResourceScope` holding that customer's database, knowledge base, and payload store |
| `application.state.llm` | A `BaseLLM` implementation. No provider client is bundled and no model is named in this template |

Identity middleware in front of the entry adapter must stamp a verified trust level, a caller id,
and the `customer_ref` scope. **All three modes are write operations**, so all three require a
verified middleware principal — a shared deployment bearer token is not sufficient for any of
them (see *Behaviour without the platform*).

Declared secrets (`config/agent.yaml`): `LLM_API_KEY`, `ENGAGEMENT_DB_DSN`, `ENGAGEMENT_KB_DSN`.

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `profession_profile` (`audit_firm`) | `audit_firm` \| `law_firm` \| `consultancy` — set by a human; the agent never infers it |
| `top_k` (`8`) | Number of past review perspectives recalled |
| `retention_days` (`365`) | Retention of derived records |
| `payload_ttl_s` (`900`) | Lifetime of a staged request body in the payload store |
| `max_rationale_chars` (`1000`) | Cap on the model-visible projection of a past decision rationale |
| `max_retry` (`3`) / `timeout_s` (`60`) | Execution controls |
| `memory_enabled` (`false`) | No checkpointer is attached |
| `hitl.enabled` (`false`) | No interrupt/resume cycle — the brief is always non-conclusive, and the acceptance decision is made outside the graph. Human review arrives through `/feedback` instead |

Start-up is fail-closed throughout: a malformed `config/config.yaml`, a missing resolver, or a
missing LLM stops the process rather than producing a degraded deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run on AGENTIC STAR, and this agent does not fall back to a degraded
mode when it is absent. A bare `uvicorn src.api.server:app` **does not start**: `wire_services()`
raises `RuntimeError: wire_services requires customer-specific resource_scopes` during the
start-up event, and the process cannot even answer `GET /health`.

That is deliberate. The alternative — booting with an empty resolver, or an invented default
customer — would produce a server that accepts requests and reads from a partition nobody
authorised. Refusing to start is the honest outcome.

Even with a running deployment, a standalone `INVOKE_AUTH_TOKEN` bearer token reaches nothing:
`/invoke`, `/ingest` and `/feedback` all classify as writes and all answer **403 — verified
middleware identity is required**. `/invoke` counts as a write because it persists the case and
evidence registry and the gated claim snapshots that later feedback refers to; the route name is
not the side effect.

The test suite runs without a platform connection, using injected in-memory stands-in.

## Input

Every mode takes a plain JSON object — not a JSON string — and each mode has a **closed** key set.
An unexpected key rejects the request rather than being ignored:

| Mode | Accepted keys |
|---|---|
| `/invoke` | `customer_id`, `policy_edition_ref`, `case_narrative`, `evidence_declarations`, `kyc_documents` |
| `/ingest` | `customer_id`, `policy_edition_ref`, `records` |
| `/feedback` | `customer_id`, `case_ref`, `claim_id`, `perspective_code`, `clause_id`, `policy_edition_ref`, `source_ids`, `decision_outcome`, `decision_rationale`, `action`, `approved` |

`customer_id` is required on every request and must equal the `customer_ref` the verified caller
resolved to — a mismatch is **403**, never a silent read of the caller's own partition. `case_ref`
is minted by the server for `/invoke` and `/ingest`; only `/feedback` supplies one, to name the
case it is amending.

```bash
curl -X POST https://<your-deployment>/invoke \
  -H 'Content-Type: application/json' \
  -d '{
    "customer_id": "<the customer_ref your middleware stamped>",
    "policy_edition_ref": "acceptance-policy-2026",
    "case_narrative": "Statutory audit for a listed manufacturer; prior-year advisory by an affiliate.",
    "evidence_declarations": [
      {"source_id": "conflictcheckA", "doc_type": "conflict_check"}
    ],
    "kyc_documents": [
      {"source_id": "kycpackA", "doc_type": "client_identity"}
    ]
  }'
```

Authentication headers are omitted above because there is no token that works: the request must
arrive through identity middleware that stamps the verified principal.

### Errors

A rejected request comes back as **400** with one of an allowlisted set of codes — never an inner
exception string:

| Code | Meaning |
|---|---|
| `packet_rejected` | The body failed the closed key set, or the graph rejected the envelope |
| `policy_edition_invalid` | `policy_edition_ref` missing, empty, or not a string |
| `payload_unresolvable` | The staged body reference could not be resolved in this scope and session |
| `case_ref_invalid` | The supplied `case_ref` is not a well-formed reference |
| `llm_not_configured` / `llm_call_failed` / `llm_output_parse_failed` | The model stage could not produce a usable draft |
| `workflow_failed` | The domain workflow failed for a reason not covered above |

Authorization failures are **403** — `customer_mismatch`, `authorization_unresolved`,
`middleware_caller_id_required`, `reserved_caller_id_prefix`. An unexpected runtime failure is
**500 `backend_unavailable`**; the exception type is recorded in the audit event, and its message
is not.

## Output

```json
{
  "status": "success",
  "correlation_id": "...",
  "case_ref": "<the server-minted reference for this case>",
  "output": {
    "mode": "invoke",
    "gate_kind": "engagement_acceptance_evidence",
    "meta": {"degraded": false, "degradation_code": null},
    "policy_clauses": [
      {"clause_id": "conflict-screening", "policy_edition_ref": "acceptance-policy-2026", "clause_ref": "A-1"}
    ],
    "evidence_sources": [
      {"source_id": "conflictcheckA", "doc_type": "conflict_check", "quote_span": "...", "provenance": {}}
    ],
    "candidate_conflicts": [
      {"claim_id": "...", "description": "...", "clause_id": "conflict-screening",
       "source_ids": ["conflictcheckA"], "severity": "candidate", "confidence": 0.4}
    ],
    "gaps": [
      {"claim_id": "...", "description": "...", "clause_id": "self-review-threat", "source_ids": ["kycpackA"]}
    ],
    "verification_questions": [
      {"claim_id": "...", "question": "...", "clause_id": "client-identity", "source_ids": ["kycpackA"]}
    ]
  }
}
```

Values above are illustrative; real output depends on the pack and the policy edition supplied.

The top-level key set is closed per mode and the finding objects are rebuilt field by field from a
single declared contract — the same declarations that generate the schema the model is asked to
follow. A model-authored dictionary is never retained wholesale, and a container where a scalar
was declared is rejected rather than flattened, so free text cannot hide under `description`.

`/ingest` returns `{"mode": "ingest", "gate_kind": ..., "meta": ..., "ingest_summary": {...}}`,
and `/feedback` returns the same shape with `feedback_summary`.

## Security and Limitations

The five-layer AgentCore model applies, with these template-specific boundaries:

- **Trust and partition.** Every node requires verified-external trust. The customer's data
  handles are resolved per invocation from the verified caller id; the resolver refuses a caller
  it does not know, and a resolved scope whose `customer_ref` disagrees with the request is
  rejected. Cross-customer reuse is prevented by there being no shared connection to reuse.
- **Ingress.** Raw request content is staged outside graph state under a scope- and
  session-bound reference and deleted after the run. Checkpointing is off, so there is no
  checkpoint for raw content to reach.
- **Egress.** The response is projected against the closed contract described above. Acceptance
  verdicts, risk ratings, and independence determinations are absent from that contract and
  therefore unreachable.
- **Provenance.** Every finding must cite a `clause_id` from the supplied policy edition and
  `source_id`s from the documents actually declared for this case. A citation to something that
  was not supplied does not pass the gate.
- **Personal data.** Input is scanned by the framework's own detector; this template does not
  hand-roll a replacement. A deployment handling Japanese free text that may carry personal names
  should account for the detector's known limits in its own data handling.
- **Erasure.** `DELETE /cases/{case_ref}` removes the case's database rows, knowledge-base
  documents, and staged payloads, and emits an audit event. It reports what it deleted.

Known limitations, stated rather than hidden:

- **The gate stops citation and conclusion, not tone.** The provenance registry stops a direct
  quotation of a past matter and stops the generation of a conclusion field for the current one.
  It does **not** stop the model's prose from being coloured by the pattern of past decisions it
  was shown. The agent does not claim to be free of that influence; it claims only that it emits
  no verdict field and quotes no prior matter.
- A citation proves a finding was drawn from a document declared for this case. It does not prove
  the finding is a correct reading of that document, or that the clause it names is the right one.
- Recall is only as good as what was ingested. An empty policy edition or an empty history
  produces a thin brief and does not flag that as unusual.
- Ingest accepts lightweight structured text. Documents that live in PDF or spreadsheet form must
  be converted before they can be accumulated.
- `confidence` is a model-supplied number on candidate conflicts. It is a sorting aid, not a
  calibrated probability.

Output is machine-generated and assistive. It does not substitute for professional judgement, and
the acceptance decision, the independence conclusion, and the record of both remain the
responsibility of the qualified professionals in the adopting firm.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), accumulation-based document-generation pattern
- Generation mode: `llm`
- HITL: not enabled — human review arrives asynchronously through `/feedback`
- Persistence: no checkpointer; the database, knowledge base, and payload store are deployment-injected
- External API clients: deployment-injected; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, injecting in-memory stands-in for the customer's stores
and a deterministic stand-in for the language model. Running the agent itself does not: as
described above, `uvicorn src.api.server:app` with nothing injected raises during start-up. To
exercise it locally, wire `app.state.resource_scopes` and `app.state.llm` yourself before calling
`wire_services()`, or use the Marketplace entry point below.

`cli.py` is the AGENTIC STAR Marketplace entry point, and it serves `/invoke` only — the write
modes are refused there, because a Marketplace caller cannot present the verified middleware
principal that writing to the accumulated record requires. On that path:

- the caller sends only `case_narrative`, `evidence_declarations` and `kyc_documents`;
- `customer_id`, `customer_ref` and `policy_edition_ref` come from the deployment, never from the
  caller — a caller-supplied value would be a claim about which partition to read;
- `case_ref` and `payload_ref` are minted by the server, so one caller cannot overwrite another
  caller's case in the same partition;
- the request body is size-capped, staged, and deleted after the run.

It reads its scope and its Azure OpenAI credentials (`AZURE_OPENAI_API_KEY`,
`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, plus `STG_DEFAULT_CUSTOMER_REF` and
`STG_DEFAULT_CALLER_ID`) from an explicit allowlist of environment variables and rejects anything
else, and it requires the endpoint to be a bare Azure resource endpoint rather than an arbitrary
URL.

Because the Marketplace path has no hosting layer to supply `resource_scopes`, it binds one
**fictional sample partition** from `config/engagement_scope.sample.json` so the deployment has a
policy edition and a history to read. The file is explicitly marked as a sample and loading is
fail-closed — an unmarked or malformed file raises at start-up rather than yielding a silently
empty partition. **Replace it with the adopting firm's real approved policy edition before any
production use**; `docs/07_operation_guide.md` records what to change.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the customer-isolation contract, the
response contract, and the security design. `docs/03_test_spec.md` records the test specification
and the verification results, including a live-model run.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
