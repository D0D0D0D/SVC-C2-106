"""Single source of truth for the LLM response contract.

The prompt schema and the fail-closed projector are generated from the same
field declarations.  LLM-authored dictionaries are never retained wholesale.
"""

from __future__ import annotations

from typing import Any

from framework.errors import SecurityViolationError
from src.evidence_gate import FindingField, project_typed_finding


MAX_FINDING_TEXT_CHARS = 2_000

CANDIDATE_CONFLICT_FIELDS = (
    FindingField("description", str, required=True),
    FindingField("clause_id", str, required=True),
    FindingField("source_ids", list, required=True, item_type=str),
    FindingField("severity", str, required=True, allowed_values=("candidate",)),
    FindingField("confidence", (int, float), required=True),
)
GAP_FIELDS = (
    FindingField("description", str, required=True),
    FindingField("clause_id", str, required=True),
    FindingField("source_ids", list, required=True, item_type=str),
)
VERIFICATION_QUESTION_FIELDS = (
    FindingField("question", str, required=True),
    FindingField("clause_id", str, required=True),
    FindingField("source_ids", list, required=True, item_type=str),
)

FINDING_FIELDS_BY_SECTION: dict[str, tuple[FindingField, ...]] = {
    "candidate_conflicts": CANDIDATE_CONFLICT_FIELDS,
    "gaps": GAP_FIELDS,
    "verification_questions": VERIFICATION_QUESTION_FIELDS,
}
FINDING_TEXT_KEYS = {
    "candidate_conflicts": "description",
    "gaps": "description",
    "verification_questions": "question",
}
FINDING_TEXT_LIMITS = {
    "description": MAX_FINDING_TEXT_CHARS,
    "question": MAX_FINDING_TEXT_CHARS,
}
SECTIONS_REQUIRING_SOURCE_IDS = frozenset({"candidate_conflicts"})


def response_contract() -> dict[str, Any]:
    """Return the JSON response contract generated from validation constants."""

    return {
        "type": "object",
        "required": list(FINDING_FIELDS_BY_SECTION),
        "additionalProperties": False,
        "properties": {
            section: {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [field.name for field in fields if field.required],
                    "additionalProperties": False,
                    "properties": {
                        field.name: _field_contract(
                            field,
                            require_source=section in SECTIONS_REQUIRING_SOURCE_IDS,
                        )
                        for field in fields
                    },
                },
            }
            for section, fields in FINDING_FIELDS_BY_SECTION.items()
        },
    }


def project_response(value: Any) -> dict[str, list[dict[str, Any]]]:
    """Validate and copy only declared response fields.

    Unknown top-level or finding keys fail closed without being echoed.  The
    typed projector rejects containers where a scalar was declared, so nested
    raw text cannot hide under fields such as ``description``.
    """

    if not isinstance(value, dict):
        raise SecurityViolationError("S-3 LLM response must be an object")
    if set(value) != set(FINDING_FIELDS_BY_SECTION):
        raise SecurityViolationError("S-3 LLM response contains undeclared or missing sections")

    projected: dict[str, list[dict[str, Any]]] = {}
    for section, fields in FINDING_FIELDS_BY_SECTION.items():
        raw_findings = value[section]
        if not isinstance(raw_findings, list):
            raise SecurityViolationError("S-3 LLM response section must be a list")
        canonical: list[dict[str, Any]] = []
        for raw_finding in raw_findings:
            finding = project_typed_finding(raw_finding, fields)
            text_key = FINDING_TEXT_KEYS[section]
            text = finding[text_key]
            if not text or len(text) > FINDING_TEXT_LIMITS[text_key]:
                raise SecurityViolationError("S-3 LLM finding text length is outside its contract")
            if not finding["clause_id"]:
                raise SecurityViolationError("S-3 LLM finding clause_id is empty")
            if section in SECTIONS_REQUIRING_SOURCE_IDS and not finding["source_ids"]:
                raise SecurityViolationError("S-3 LLM finding requires at least one source_id")
            if section == "candidate_conflicts":
                confidence = float(finding["confidence"])
                if not 0.0 <= confidence <= 1.0:
                    raise SecurityViolationError("S-3 LLM finding confidence is outside its contract")
                finding["confidence"] = confidence
            canonical.append(finding)
        projected[section] = canonical
    return projected


def normalize_response_source_ids(
    response: dict[str, list[dict[str, Any]]],
    registered_source_ids: set[str] | frozenset[str],
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """Remove unregistered citations before evidence-gate validation.

    Evidence-absent gaps and questions remain valid with an empty source list.
    A candidate conflict without any registered source is removed because it
    would otherwise become an ungrounded substantive claim.
    """

    registered = frozenset(registered_source_ids)
    normalized: dict[str, list[dict[str, Any]]] = {}
    dropped_candidate = False
    for section, findings in response.items():
        normalized[section] = []
        for finding in findings:
            source_ids = [
                source_id
                for source_id in finding["source_ids"]
                if source_id in registered
            ]
            if section == "candidate_conflicts" and not source_ids:
                dropped_candidate = True
                continue
            normalized[section].append({**finding, "source_ids": source_ids})
    return normalized, dropped_candidate


def evidence_map_for_response(
    response: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build the evidence-gate contract from a projected response."""

    evidence_map: list[dict[str, Any]] = []
    cited: set[str] = set()
    for section, findings in response.items():
        text_key = FINDING_TEXT_KEYS[section]
        for finding in findings:
            source_ids = list(finding["source_ids"])
            claim = {
                "claim_text": finding[text_key],
                "source_ids": source_ids,
            }
            if "claim_id" in finding:
                claim["claim_id"] = finding["claim_id"]
            evidence_map.append(claim)
            cited.update(source_ids)
    return evidence_map, sorted(cited)


def output_keys_for_section(section: str) -> frozenset[str]:
    """Return post-validation keys after the framework adds ``claim_id``."""

    return frozenset({"claim_id", *(field.name for field in FINDING_FIELDS_BY_SECTION[section])})


def _field_contract(
    field: FindingField, *, require_source: bool = False
) -> dict[str, Any]:
    declared = field.expected_type if isinstance(field.expected_type, tuple) else (field.expected_type,)
    if list in declared:
        contract: dict[str, Any] = {"type": "array"}
        if field.item_type is str:
            contract["items"] = {"type": "string"}
    elif str in declared:
        contract = {"type": "string"}
    elif any(item in (int, float) for item in declared):
        contract = {"type": "number"}
    else:
        raise TypeError("unsupported response contract field type")
    if field.allowed_values:
        contract["enum"] = list(field.allowed_values)
    if field.name == "source_ids" and require_source:
        contract["minItems"] = 1
    if field.name in FINDING_TEXT_LIMITS:
        contract["minLength"] = 1
        contract["maxLength"] = FINDING_TEXT_LIMITS[field.name]
    if field.name == "confidence":
        contract["minimum"] = 0.0
        contract["maximum"] = 1.0
    return contract
