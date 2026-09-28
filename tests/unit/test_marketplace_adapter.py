"""Unit contracts for the AGENTIC STAR Marketplace entry adapter."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import cli
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret, NullProvider
from framework.secrets.context import bound_secrets
from src.graph.graph import EngagementAcceptanceEvidenceGraph
from src.services import AuthorizedResourceResolver


@pytest.fixture(autouse=True)
def marketplace_environment(monkeypatch):
    values = {
        "STG_DEFAULT_CALLER_ID": "marketplace-caller",
        "STG_DEFAULT_CUSTOMER_REF": "customer-a",
        "AZURE_OPENAI_API_KEY": "key-a",
        "AZURE_OPENAI_ENDPOINT": "https://azure.example.invalid",
        "AZURE_OPENAI_DEPLOYMENT": "deployment-a",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def _capture_parent_invoke(monkeypatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        del self
        captured.update(
            user_input=user_input,
            session_id=session_id,
            ctx=ctx,
            input_context=input_context,
        )
        return {"status": AgentStatus.SUCCESS.value}

    monkeypatch.setattr(EngagementAcceptanceEvidenceGraph, "invoke", fake_invoke)
    return captured


def test_invoke_discards_runner_context_and_rebuilds_declared_scope(monkeypatch):
    captured = _capture_parent_invoke(monkeypatch)
    original_ctx = InvocationContext(
        caller_id="runner-controlled-caller",
        caller_trust_level=TrustLevel.INTERNAL,
        hitl_allowed=True,
    )

    result = cli.MarketplaceGraph().invoke(
        '{"mode":"invoke"}',
        session_id="marketplace-session",
        ctx=original_ctx,
        input_context={
            "conversation_history": [{"role": "user", "content": "unscanned"}],
            "customer_ref": "attacker-customer",
            "future_runner_key": "must-not-enter-state",
        },
    )

    assert result == {"status": AgentStatus.SUCCESS.value}
    assert captured["input_context"] == {"customer_ref": "customer-a"}
    assert captured["ctx"] is not original_ctx
    assert captured["ctx"].caller_id == "marketplace-caller"
    assert captured["ctx"].caller_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert captured["ctx"].hitl_allowed is False


def test_scope_resolution_follows_entry_adapter_declarations(monkeypatch):
    monkeypatch.setattr(cli, "SCOPE_KEYS", ("account_id", "workspace_id"))
    monkeypatch.setattr(
        cli,
        "STG_DEFAULT_SCOPE_ENVS",
        {
            "account_id": "MARKETPLACE_ACCOUNT_ID",
            "workspace_id": "MARKETPLACE_WORKSPACE_ID",
        },
    )
    monkeypatch.setenv("MARKETPLACE_ACCOUNT_ID", "account-a")
    monkeypatch.setenv("MARKETPLACE_WORKSPACE_ID", "workspace-a")

    assert cli._scope_from_environment(cli._AllowlistedEnvironmentProvider()) == {
        "account_id": "account-a",
        "workspace_id": "workspace-a",
    }


@pytest.mark.parametrize(
    "invalid",
    [None, "", " ", "bad value", "leading ", "bad\nvalue", "bad\tvalue"],
)
def test_scope_unset_empty_or_whitespace_fails_closed_at_startup(monkeypatch, invalid):
    if invalid is None:
        monkeypatch.delenv("STG_DEFAULT_CUSTOMER_REF")
    else:
        monkeypatch.setenv("STG_DEFAULT_CUSTOMER_REF", invalid)

    with pytest.raises((MissingSecret, ValueError), match="STG_DEFAULT_CUSTOMER_REF"):
        cli.MarketplaceGraph()


def test_scope_declaration_mismatch_fails_at_startup(monkeypatch):
    monkeypatch.setattr(cli, "SCOPE_KEYS", ("customer_ref",))
    monkeypatch.setattr(cli, "STG_DEFAULT_SCOPE_ENVS", {})

    with pytest.raises(RuntimeError, match="exactly match"):
        cli.MarketplaceGraph()


@pytest.mark.parametrize(
    "missing_key",
    (*cli._AZURE_OPENAI_SECRET_KEYS, cli._CALLER_ID_ENV),
)
def test_provision_secrets_rejects_missing_value_at_startup(monkeypatch, missing_key):
    monkeypatch.delenv(missing_key)
    if missing_key == cli._CALLER_ID_ENV:
        with pytest.raises(MissingSecret, match=missing_key):
            cli.MarketplaceGraph()
        return

    agent = cli.MarketplaceGraph()
    agent.compile()
    with pytest.raises(MissingSecret, match=missing_key):
        agent.provision_secrets(NullProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    agent = cli.MarketplaceGraph()
    agent.compile()
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "gate-test-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gate-test-deployment")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        agent.provision_secrets(NullProvider())


def test_allowlisted_provider_hides_undeclared_environment(monkeypatch):
    monkeypatch.setenv("UNRELATED_PLATFORM_SECRET", "must-not-leak")

    assert cli._AllowlistedEnvironmentProvider().get("UNRELATED_PLATFORM_SECRET") is None


def test_azure_llm_resolves_fresh_values_and_client_for_every_call(monkeypatch):
    constructed: list[dict[str, str]] = []

    class RecordingAzureClient:
        def __init__(self, config):
            constructed.append(dict(config))
            self.config = config

        def complete(self, messages):
            return {"content": self.config["api_key"], "messages": messages}

    fake_module = ModuleType("shared.services.llm.azure_openai_client")
    fake_module.AzureOpenAIClient = RecordingAzureClient
    monkeypatch.setitem(sys.modules, fake_module.__name__, fake_module)

    llm = cli.MarketplaceAzureLLM()
    assert vars(llm) == {}
    with bound_secrets(cli._AllowlistedEnvironmentProvider()):
        first = llm.complete([{"role": "user", "content": "first"}])
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "key-b")
        second = llm.complete([{"role": "user", "content": "second"}])

    assert first["content"] == "key-a"
    assert second["content"] == "key-b"
    assert [item["api_key"] for item in constructed] == ["key-a", "key-b"]
    assert vars(llm) == {}


@pytest.mark.parametrize("mode", ["ingest", "feedback"])
def test_marketplace_does_not_expose_write_modes(monkeypatch, mode):
    _capture_parent_invoke(monkeypatch)

    with pytest.raises(ValueError, match="not available"):
        cli.MarketplaceGraph().invoke(json.dumps({"mode": mode}))


def test_awaiting_human_is_a_marketplace_failure(monkeypatch):
    def fake_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        del self, user_input, session_id, ctx, input_context
        return {"status": AgentStatus.AWAITING_HUMAN.value}

    monkeypatch.setattr(EngagementAcceptanceEvidenceGraph, "invoke", fake_invoke)

    with pytest.raises(RuntimeError, match="AWAITING_HUMAN"):
        cli.MarketplaceGraph().invoke("request")


def test_compile_uses_azure_seam_without_replacing_deterministic_nodes():
    resolver = AuthorizedResourceResolver()
    agent = cli.MarketplaceGraph({"resource_resolver": resolver})
    agent.compile()

    assert agent.config["resource_resolver"] is resolver
    assert isinstance(agent.config["llm"], cli.MarketplaceAzureLLM)
    assert agent.config["hitl"] == {"enabled": False}
    assert agent._nodes["pre_process"].__class__.__module__ == "src.nodes.pre_process_node"
    assert agent._nodes["main"].__class__.__module__ == "src.nodes.main_node"
    assert agent._nodes["post_process"].__class__.__module__ == "src.nodes.post_process_node"


def test_main_uses_marketplace_contract_and_repository_config(monkeypatch):
    called: dict[str, Any] = {}

    def fake_runner(agent_cls, **kwargs):
        called["agent_cls"] = agent_cls
        called.update(kwargs)

    monkeypatch.setattr(cli, "run_agent_marketplace", fake_runner)
    cli.main()

    assert called["agent_cls"] is cli.MarketplaceGraph
    assert called["agent_name"] == "SVC-C2-106"
    assert called["namespace"] == "svc"
    assert called["config"] == cli.load_agent_config(Path(__file__).resolve().parents[2])


class TestDeploymentPartitionIsActuallyBound:
    """The resolver must hold a real partition, not an empty one.

    Regression: cli.py built AuthorizedResourceResolver() with no scopes, so
    for_caller() raised authorization_unresolved on every Marketplace request --
    the agent could not serve anyone. ResourceScope needs live db/kb/payload
    handles, and Marketplace has no hosting layer to supply
    application.state.resource_scopes the way src/api/server.py does, so the
    adapter binds the bundled sample partition to its one declared caller.
    """

    def test_the_declared_caller_resolves_to_a_usable_scope(self, marketplace_environment):
        from src.services.sample_scope import sample_policy_edition_ref

        resolver = cli.MarketplaceGraph(config={}).config["resource_resolver"]
        scope = resolver.for_caller(marketplace_environment["STG_DEFAULT_CALLER_ID"])

        assert scope.customer_ref == marketplace_environment["STG_DEFAULT_CUSTOMER_REF"]
        assert scope.db.policy_clauses(sample_policy_edition_ref())
        assert scope.db.policy_profile(sample_policy_edition_ref())

    def test_any_other_caller_is_still_refused(self, marketplace_environment):
        """Binding a partition must not turn into an open door."""

        from src.services.service import AuthorizationError

        resolver = cli.MarketplaceGraph(config={}).config["resource_resolver"]
        with pytest.raises(AuthorizationError):
            resolver.for_caller("someone-else")

    def test_prior_consultations_are_searchable(self, marketplace_environment):
        from src.services.sample_scope import sample_policy_edition_ref
        from src.services.service import build_retrieval_text

        scope = cli.MarketplaceGraph(config={}).config["resource_resolver"].for_caller(
            marketplace_environment["STG_DEFAULT_CALLER_ID"]
        )
        hits = scope.kb.search(
            build_retrieval_text(
                "self-review-threat", "audit client subsidiary control design", "independence"
            ),
            policy_edition_ref=sample_policy_edition_ref(),
            top_k=5,
        )
        assert hits, "the bundled history must be reachable, or recall is silently empty"

    def test_an_injected_resolver_is_left_alone(self, marketplace_environment):
        """Hosting that does supply a partition keeps ownership of it."""

        injected = AuthorizedResourceResolver({"other": object()})
        graph = cli.MarketplaceGraph(config={"resource_resolver": injected})
        assert graph.config["resource_resolver"] is injected


class TestInlineRecordsAreStaged:
    """PreProcessNode needs the full six-key packet, payload_ref included."""

    def _graph(self):
        return cli.MarketplaceGraph(config={})

    def test_inline_body_is_staged_in_the_shape_the_workflow_unwraps(self, marketplace_environment):
        """A list here resolves fine and is then silently dropped downstream.

        domain_workflow_graph unwraps payload["records"] only when it is a
        mapping, then reads evidence_declarations / case_narrative from it. The
        first version of this adapter stored a list, so the caller's evidence
        never reached the workflow while everything still looked healthy.
        """

        graph = self._graph()
        customer = marketplace_environment["STG_DEFAULT_CUSTOMER_REF"]
        packet = json.loads(
            cli._marketplace_user_input(
                json.dumps(
                    {
                        "case_narrative": "acceptance review",
                        "evidence_declarations": [{"source_id": "a", "doc_type": "kyc"}],
                    }
                ),
                {"customer_ref": customer},
                graph._payloads,
                "thread-a",
            )
        )
        assert set(packet) == {
            "mode", "customer_id", "customer_ref", "policy_edition_ref", "case_ref", "payload_ref",
        }
        resolved = graph._payloads.resolve(
            packet["payload_ref"], scope={"customer_ref": customer}, session_id="thread-a", consume=False
        )
        records = resolved["records"]
        assert isinstance(records, dict), "a list is dropped by the workflow's unwrap"
        assert records["case_narrative"] == "acceptance review"
        assert records["evidence_declarations"] == [{"source_id": "a", "doc_type": "kyc"}]

    def test_case_ref_is_server_minted_not_taken_from_the_caller(self, marketplace_environment):
        """post_process persists against case_ref; a caller-chosen one overwrites."""

        from src.payload_store import is_reference

        graph = self._graph()
        packet = json.loads(
            cli._marketplace_user_input(
                json.dumps({"case_ref": "victims-case", "case_narrative": "x"}),
                {"customer_ref": marketplace_environment["STG_DEFAULT_CUSTOMER_REF"]},
                graph._payloads,
                "thread-a",
            )
        )
        assert packet["case_ref"] != "victims-case"
        assert is_reference(packet["case_ref"])

    def test_an_oversized_body_is_refused_before_it_is_stored(self, marketplace_environment):
        graph = self._graph()
        with pytest.raises(ValueError):
            cli._marketplace_user_input(
                json.dumps({"case_narrative": "x" * (cli._MAX_BODY_BYTES + 1)}),
                {"customer_ref": marketplace_environment["STG_DEFAULT_CUSTOMER_REF"]},
                graph._payloads,
                "thread-a",
            )

    def test_a_staging_store_exists_even_when_a_resolver_is_injected(self, marketplace_environment):
        """Creating it only on the bundled-partition branch crashed on first use."""

        injected = AuthorizedResourceResolver({"other": object()})
        assert cli.MarketplaceGraph(config={"resource_resolver": injected})._payloads is not None

    def test_partition_identifiers_come_from_the_deployment_not_the_caller(
        self, marketplace_environment
    ):
        """A caller-supplied customer/edition would be a claim about what to read."""

        graph = self._graph()
        packet = json.loads(
            cli._marketplace_user_input(
                json.dumps(
                    {
                        "case_ref": "case-1",
                        "records": [],
                        "customer_id": "someone-elses-customer",
                        "customer_ref": "someone-elses-customer",
                        "policy_edition_ref": "someone-elses-edition",
                    }
                ),
                {"customer_ref": marketplace_environment["STG_DEFAULT_CUSTOMER_REF"]},
                graph._payloads,
                "thread-a",
            )
        )
        assert packet["customer_id"] == marketplace_environment["STG_DEFAULT_CUSTOMER_REF"]
        assert packet["customer_ref"] == marketplace_environment["STG_DEFAULT_CUSTOMER_REF"]
        assert packet["policy_edition_ref"] != "someone-elses-edition"

    @pytest.mark.parametrize("mode", sorted(cli._MARKETPLACE_WRITE_MODES))
    def test_write_modes_are_refused_before_anything_is_staged(self, marketplace_environment, mode):
        graph = self._graph()
        with pytest.raises(ValueError):
            cli._marketplace_user_input(
                json.dumps({"mode": mode, "records": []}),
                {"customer_ref": marketplace_environment["STG_DEFAULT_CUSTOMER_REF"]},
                graph._payloads,
                "thread-a",
            )


class TestStagedBodyDoesNotOutliveTheRequest:
    """TTL expiry only runs on the next store operation.

    Without an explicit delete the last request of a process keeps its raw body
    in memory until the process ends -- exactly the material the payload store
    exists to keep out of state.
    """

    def _run(self, monkeypatch, raise_in_graph):
        graph = cli.MarketplaceGraph(config={})
        seen: dict = {}

        def fake_invoke(self, user_input, session_id="", ctx=None, input_context=None):
            seen["ref"] = json.loads(user_input)["payload_ref"]
            seen["thread"] = ctx.thread_id
            if raise_in_graph:
                raise RuntimeError("graph failure")
            return {"status": "success", "output": {}}

        monkeypatch.setattr(
            cli.EngagementAcceptanceEvidenceGraph, "invoke", fake_invoke, raising=False
        )
        return graph, seen

    def _assert_gone(self, graph, seen, customer):
        from src.payload_store import PayloadStoreError

        with pytest.raises(PayloadStoreError):
            graph._payloads.resolve(
                seen["ref"], scope={"customer_ref": customer}, session_id=seen["thread"], consume=False
            )

    def test_deleted_after_a_successful_request(self, monkeypatch, marketplace_environment):
        graph, seen = self._run(monkeypatch, raise_in_graph=False)
        graph.invoke(json.dumps({"case_narrative": "x"}), session_id="s-1")
        self._assert_gone(graph, seen, marketplace_environment["STG_DEFAULT_CUSTOMER_REF"])

    def test_deleted_even_when_the_graph_raises(self, monkeypatch, marketplace_environment):
        graph, seen = self._run(monkeypatch, raise_in_graph=True)
        with pytest.raises(RuntimeError):
            graph.invoke(json.dumps({"case_narrative": "x"}), session_id="s-1")
        self._assert_gone(graph, seen, marketplace_environment["STG_DEFAULT_CUSTOMER_REF"])
