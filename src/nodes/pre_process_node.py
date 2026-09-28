"""Outer S-2 intake validation for the engagement evidence workflow."""

from __future__ import annotations

import json
from typing import ClassVar

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.nodes.function_node import FunctionNode
from shared.utils.audit_logger import emit_trace_event
from src.services import AuthorizedResourceResolver, AuthorizationError


class EngagementIntakePreProcessNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    _allowed_by_mode: ClassVar[dict[str, frozenset[str]]] = {
        "invoke": frozenset({"mode", "customer_id", "customer_ref", "policy_edition_ref", "case_ref", "payload_ref"}),
        "ingest": frozenset({"mode", "customer_id", "customer_ref", "policy_edition_ref", "case_ref", "payload_ref"}),
        "feedback": frozenset({"mode", "customer_id", "customer_ref", "case_ref", "payload_ref"}),
    }

    def __init__(self, *, resource_resolver: AuthorizedResourceResolver | None = None) -> None:
        super().__init__()
        self._resource_resolver = resource_resolver

    def execute(self, state: dict) -> dict:
        emit_trace_event("engagement_intake_validated", {"caller_id": state.get("caller_id", "")}, state)
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        try:
            packet = json.loads(state.get("user_input", ""))
            if not isinstance(packet, dict):
                raise ValueError("packet_not_object")
            mode = packet.get("mode")
            allowed = self._allowed_by_mode.get(mode)
            if allowed is None or set(packet) - allowed:
                raise ValueError("packet_rejected")
            if set(packet) != allowed:
                raise ValueError("packet_missing_required_fields")
            if not all(isinstance(packet[key], str) and packet[key] for key in allowed):
                raise ValueError("packet_invalid_field")
            if self._resource_resolver is None:
                raise AuthorizationError("authorization_unresolved")
            ctx = InvocationContext.from_state(state)
            scope = self._resource_resolver.for_caller(ctx.caller_id)
            if packet["customer_id"] != scope.customer_ref or packet["customer_ref"] != scope.customer_ref:
                raise AuthorizationError("customer_mismatch")
        except (ValueError, json.JSONDecodeError, AuthorizationError) as exc:
            return {"status": AgentStatus.ERROR.value, "error_log": [str(exc)]}
        delta = {
            "mode": mode,
            "case_ref": packet["case_ref"],
            "payload_ref": packet["payload_ref"],
            "validated_input": json.dumps(packet, ensure_ascii=False, sort_keys=True),
            "status": AgentStatus.SUCCESS.value,
        }
        if mode in {"invoke", "ingest"}:
            delta["policy_edition_ref"] = packet["policy_edition_ref"]
        return delta

    def _extra_security_gate_input(self, state: dict) -> dict:
        # Validation is deliberately based on an explicit mode declaration,
        # never on whether a preceding node happened to write some key.
        return state


# Compatibility name retained for generated scaffold consumers.
PreProcessNode = EngagementIntakePreProcessNode
