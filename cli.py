"""AGENTIC STAR Marketplace entry adapter for SVC-C2-106."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider for pre-1.0.2 wheels."""

        def __init__(self, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.entry_adapter import SCOPE_KEYS, STG_DEFAULT_SCOPE_ENVS
from src.graph.graph import EngagementAcceptanceEvidenceGraph
from src.services import AuthorizedResourceResolver
from src.payload_store import PayloadStore, mint_reference
from src.services.sample_scope import build_sample_scope, sample_policy_edition_ref

_NAMESPACE = "svc"
_AGENT_NAME = "SVC-C2-106"
_PROVIDER_AGENT_NAME = "svc_c2_106"
_CALLER_ID_ENV = "STG_DEFAULT_CALLER_ID"
# Request-scoped: the staged body must not outlive the conversation.
_PAYLOAD_TTL_SECONDS = 900
# The invoke body the HTTP path accepts (src/api/server.py::_invoke_mode).
# customer_id / policy_edition_ref / case_ref are deployment- or server-owned
# and are deliberately absent here.
_CALLER_BODY_KEYS = frozenset({"case_narrative", "evidence_declarations", "kyc_documents"})
_MAX_BODY_BYTES = 256 * 1024

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_MARKETPLACE_WRITE_MODES = frozenset({"ingest", "feedback"})


def _validate_scope_declarations() -> None:
    """Reject malformed reusable-part adaptation before graph construction."""

    if not isinstance(SCOPE_KEYS, tuple):
        raise RuntimeError("Marketplace SCOPE_KEYS must be a tuple")
    if len(set(SCOPE_KEYS)) != len(SCOPE_KEYS):
        raise RuntimeError("Marketplace SCOPE_KEYS must not contain duplicates")
    if any(not isinstance(key, str) or not key or key != key.strip() or not key.isidentifier() for key in SCOPE_KEYS):
        raise RuntimeError("Marketplace SCOPE_KEYS must contain clean identifier strings")
    if not isinstance(STG_DEFAULT_SCOPE_ENVS, Mapping) or set(STG_DEFAULT_SCOPE_ENVS) != set(SCOPE_KEYS):
        raise RuntimeError("Marketplace STG_DEFAULT_SCOPE_ENVS must exactly match SCOPE_KEYS")
    if any(
        not isinstance(env_name, str) or not env_name or any(character.isspace() for character in env_name)
        for env_name in STG_DEFAULT_SCOPE_ENVS.values()
    ):
        raise RuntimeError("Marketplace scope environment names must be clean non-empty strings")


def _allowed_environment_keys() -> frozenset[str]:
    """Return the dynamic allowlist, including reusable-part scope declarations."""

    return frozenset(
        (
            _CALLER_ID_ENV,
            *_AZURE_OPENAI_SECRET_KEYS,
            *(STG_DEFAULT_SCOPE_ENVS[key] for key in SCOPE_KEYS),
        )
    )


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only this single-agent container's declared environment values."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _allowed_environment_keys():
            return default
        value = super().get(key)
        return value if value is not None else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve one required deployment value without repairing it."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


def _scope_from_environment(provider: SecretProvider) -> dict[str, str]:
    """Build exactly the scope declared by ``src.entry_adapter``."""

    _validate_scope_declarations()
    return {scope_key: _require_clean(provider, STG_DEFAULT_SCOPE_ENVS[scope_key]) for scope_key in SCOPE_KEYS}


class _AzureOpenAIClientCompat(BaseLLM):
    """Small Azure chat-completion client for AgentCore before 1.0.2."""

    def __init__(self, config: dict[str, str]) -> None:
        self._config = dict(config)

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        endpoint = self._config["azure_endpoint"].rstrip("/")
        deployment = quote(self._config["azure_deployment"], safe="")
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions" "?api-version=2024-10-21"
        request = Request(
            url,
            data=json.dumps({"messages": messages}).encode("utf-8"),
            headers={
                "api-key": self._config["api_key"],
                "content-type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - operator-owned endpoint
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        message = choices[0].get("message", {}) if choices else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        model = payload.get("model", self._config["azure_deployment"])
        return {
            "content": content,
            "tool_calls": message.get("tool_calls", []) if isinstance(message, dict) else [],
            "model": model,
            "usage": payload.get("usage", {}),
        }

    def stream(self, messages: list[Any]) -> Iterator[str]:
        result = self.complete(messages)
        content = result.get("content", "")
        if content:
            yield str(content)

    def bind_tools(self, tools: list[Any]) -> "_AzureOpenAIClientCompat":
        if tools:
            raise NotImplementedError("Azure compatibility client does not support tool binding")
        return self


class MarketplaceAzureLLM(BaseLLM):
    """Resolve Azure values and create a fresh client for every operation."""

    @staticmethod
    def _new_client() -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean(provider, key) for key in _AZURE_OPENAI_SECRET_KEYS}

        # Keep Azure as the sole invocation-time import. Framework, security,
        # graph, and state imports stay eager for the runner's to_thread path.
        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            AzureOpenAIClient = _AzureOpenAIClientCompat  # type: ignore[misc,assignment]

        return cast(
            BaseLLM,
            AzureOpenAIClient(
                {
                    "api_key": values["AZURE_OPENAI_API_KEY"],
                    "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
                    "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
                }
            ),
        )

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._new_client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._new_client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        return self._new_client().bind_tools(tools)


def _marketplace_user_input(
    user_input: str, scope: dict[str, str], payloads: Any, session_id: str
) -> str:
    """Reject write modes, apply the deployment scope, and stage the case body.

    PreProcessNode requires the full six-key packet including ``payload_ref``,
    and refs are issued by the store against scope + session — a Marketplace
    caller cannot mint one. ``src/api/server.py`` stages the body for the HTTP
    path; this is the same step here. The caller sends its case records inline
    under ``records``.

    ``customer_id`` / ``customer_ref`` / ``policy_edition_ref`` come from the
    deployment, never from the caller: a caller-supplied value would be a claim
    about which partition to read.
    """

    if not isinstance(user_input, str):
        raise TypeError("Marketplace input must be one plain string")
    try:
        payload = json.loads(user_input)
    except json.JSONDecodeError:
        return user_input
    if not isinstance(payload, dict):
        return user_input
    mode = payload.get("mode")
    if mode in _MARKETPLACE_WRITE_MODES:
        raise ValueError(f"Marketplace mode {mode!r} is not available")

    customer_ref = scope["customer_ref"]
    edition = sample_policy_edition_ref()
    # Keep only the fields the HTTP path accepts for invoke. Anything else the
    # caller sends is dropped rather than forwarded.
    body = {key: value for key, value in payload.items() if key in _CALLER_BODY_KEYS}
    if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > _MAX_BODY_BYTES:
        raise ValueError("Marketplace request body is too large")
    # Store the body as a dict under "records": the workflow unwraps
    # payload["records"] only when it is a mapping, then reads
    # evidence_declarations / case_narrative from it. Storing a list here would
    # resolve fine and then silently drop the caller's evidence.
    payload_body = {"customer_id": customer_ref, "policy_edition_ref": edition, **body}
    packet = {
        "mode": "invoke",
        "customer_id": customer_ref,
        "customer_ref": customer_ref,
        "policy_edition_ref": edition,
        # Server-minted, like src/api/server.py. A caller-supplied case_ref
        # would let one caller overwrite another case in the same partition.
        "case_ref": mint_reference(),
        "payload_ref": payloads.put(
            {"records": payload_body},
            scope={"customer_ref": customer_ref},
            session_id=session_id,
        ),
    }
    return json.dumps(packet, ensure_ascii=False, sort_keys=True)


class MarketplaceGraph(EngagementAcceptanceEvidenceGraph):
    """Marketplace-only scope, identity, mode, and Azure boundary."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        provider = _AllowlistedEnvironmentProvider()
        _scope_from_environment(provider)
        _require_clean(provider, _CALLER_ID_ENV)

        marketplace_config = dict(config or {})
        # The runner compiles before provision_secrets(). Supply only the
        # existing injection seams here; provisioning still validates secrets.
        #
        # An empty AuthorizedResourceResolver() made for_caller() raise
        # authorization_unresolved on every request: ResourceScope needs real
        # db/kb/payload handles, and Marketplace has no hosting layer to supply
        # application.state.resource_scopes the way src/api/server.py does.
        # Bind the bundled sample partition to this deployment's caller instead,
        # so the agent serves exactly one declared customer and no other.
        # Always present: invoke() stages through it whichever resolver is in
        # use. Creating it only on the bundled-partition branch left an injected
        # resolver crashing with AttributeError on the first request.
        self._payloads = PayloadStore(scope_keys=SCOPE_KEYS, ttl_seconds=_PAYLOAD_TTL_SECONDS)
        if marketplace_config.get("resource_resolver") is None:
            marketplace_config["resource_resolver"] = AuthorizedResourceResolver(
                {
                    _require_clean(provider, _CALLER_ID_ENV): build_sample_scope(
                        _scope_from_environment(provider)["customer_ref"], self._payloads
                    )
                }
            )
        marketplace_config["llm"] = MarketplaceAzureLLM()
        marketplace_config["memory_enabled"] = False
        marketplace_config["hitl"] = {"enabled": False}
        super().__init__(config=marketplace_config)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Runner conversation_history is untrusted free text outside S-2.
        # Rebuild context exclusively from adapter-owned deployment data.
        del input_context
        provider = _AllowlistedEnvironmentProvider()
        scope = _scope_from_environment(provider)
        caller_id = _require_clean(provider, _CALLER_ID_ENV)

        base_ctx = ctx if ctx is not None else InvocationContext(session_id=session_id or "")
        marketplace_ctx = replace(
            base_ctx,
            caller_id=caller_id,
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            hitl_allowed=False,
        )
        # The graph resolves the ref with the thread the context carries, so
        # issue it against that thread.
        staged = _marketplace_user_input(user_input, scope, self._payloads, marketplace_ctx.thread_id)
        staged_ref = json.loads(staged).get("payload_ref") if staged.lstrip().startswith("{") else None
        try:
            result = cast(
                dict[str, Any],
                super().invoke(
                    staged,
                    session_id=session_id,
                    ctx=marketplace_ctx,
                    input_context=scope,
                ),
            )
        finally:
            # The staged body is request-scoped. TTL expiry only runs on the
            # next store operation, so the last request of a process would
            # otherwise keep its raw body until the process ends.
            if staged_ref:
                self._payloads.delete(
                    staged_ref, scope={"customer_ref": scope["customer_ref"]},
                    session_id=marketplace_ctx.thread_id,
                )
        if result.get("status") == AgentStatus.AWAITING_HUMAN.value:
            raise RuntimeError("AWAITING_HUMAN is not supported by AGENTIC STAR Marketplace")
        return result

    def provision_secrets(self, provider: SecretProvider) -> None:
        # Ignore a runner provider that may include dotenv fallback or values
        # belonging to another agent in a shared environment.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in (*_AZURE_OPENAI_SECRET_KEYS, _CALLER_ID_ENV):
            _require_clean(environment_provider, key)
        _scope_from_environment(environment_provider)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(Path(__file__).resolve().parent),
    )


if __name__ == "__main__":
    main()
