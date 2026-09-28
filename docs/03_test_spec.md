# Test Specification

## Test Strategy

- Coverage target: TC / PB / AT contract coverage
- Test types: Unit / Integration / Proof-of-Boundary

## Framework Compliance Tests

| TC-ID | Test | Result |
|---|---|---|
| TC-01 | State contract: flat TypedDict, no Pydantic/dataclass | PASS |
| TC-02 | Invalid input/output contract raises `SecurityViolationError` | PASS |
| TC-03 | No credential-like State field | PASS |
| TC-04 | `InvocationContext` is derived only from `configurable` state | PASS |
| TC-05 | Domain `execute()` does not duplicate lifecycle events | PASS |
| TC-06 | S-2 final gate cannot be overridden | PASS |
| TC-07 | S-3 final gate cannot be overridden | PASS |
| TC-08 | All domain nodes declare and enforce `VERIFIED_EXTERNAL`。加えて **不足 trust では `execute()` に到達しないこと**を証明する | PASS — PB-6 `TestInvokeOrder::test_s1_denial_refuses_execution_before_execute` が `_PrivilegedTrustGateFixture` を不足 trust で `node(state)` 呼びし、`status=error` / `error_log[0]` の `S-1 trust gate denied` / `events == ["s1_denied"]` / **`execute()` 未呼び出し** を assert（正本 docs/03 の TC-08 要求） |
| TC-09 | Intake S-2 hook validates explicit mode, packet shape, and customer match | PASS |
| TC-10 | Post-process S-3 hook enforces nested closed schema and provenance | PASS |
| TC-11 | Each domain execute path emits a domain trace event | PASS |

## Proof-of-Boundary Tests

| PB-ID | Boundary | Result |
|---|---|---|
| PB-1 | BaseNode → EventEmitter | PASS |
| PB-2 | State serialization is primitive/JSON-safe | PASS |
| PB-3 | DB/KB/LLM/payload seams + real SQLite scoped retrieval composition | PASS |
| PB-4 | No Level 0 import | PASS |
| PB-5 | Checkpoint-safe State has no credential or Python object | PASS |
| PB-6 | S-1 → S-4 → S-2 → execute → S-3 → S-4 invoke order | PASS |
| PB-7 | HITL interrupt propagation | Auto-waived — non-HITL |

## Business Logic Acceptance Tests

| AT-ID | Test | Result |
|---|---|---|
| AT-1 | 同一 graph の caller A → B 連続実行で接続・payload・KB が横断しない | PASS |
| AT-2 | 捏造 source ID と整合して見える provenance を S-3 が拒否する | PASS |
| AT-3 | `recommendation` や provenance 追加キーを nested closed schema が拒否する | PASS |
| AT-4 | feedback 未宣言キーと enum 外 outcome を 400 とする | PASS |
| AT-4b | outcome/rationale を差替えても retrieval top-k の ID と順序が不変 | PASS |
| AT-4c | `history_ref` を本件 `source_ids` に指定すると拒否する | PASS |
| AT-4d | PII は safe 値から除外、命令文・長さ超過は拒否する | PASS |
| AT-4e | `reject + approved=true` は拒否、reject 単独は監査のみ | PASS |
| AT-5 | 別 edition の clause 引用を拒否する | PASS |
| AT-6 | 他 customer/session の payload ref 解決を fail-closed する | PASS |
| AT-7 | 本文を State に置かず checkpoint 経路を payload ref 化する | PASS |
| AT-8 | case 削除で DB/KB/payload を cascade 削除する | PASS |
| AT-9 | KB 空振りは success + `kb_empty`、backend 障害は error | PASS |
| AT-10 | exact schema と実在 source/clause の正常系を受理する | PASS |
| AT-11 | 検証定数から生成した prompt contract で応答を機械生成し、同じ型 projector が受理する | PASS |
| AT-12 | `description={"raw":...}` / 宣言外キー / 他 customer の case・claim ID を full composition で拒否する | PASS |
| AT-13 | 実 HTTP で invoke / ingest / feedback を直列実行し、各 operation policy と本番 composition を通す | PASS |
| AT-14 | standalone token の `/invoke` を拒否し、case/evidence/snapshot が 0 件のままである | PASS |
| AT-15 | ingest 2 行目の KB write 失敗で DB/KB 全行を rollback し、reason 付き reject を返す | PASS |
| AT-16 | shared SQLite の保存行 scope + policy で customer A の 1 hit のみ返す | PASS |
| AT-17 | retrieval query に案件 narrative を渡し、top_k=1 で意味的一致行を cold row より上位にする | PASS |
| AT-18 | LLM provider 例外文を出力せず `llm_call_failed` の安定メタデータだけを返す | PASS |
| AT-19 | feedback `source_ids` の非文字列要素を membership 判定前に拒否し、`feedback_reference_invalid` へ射影する | PASS |

## Common Parts Regression Tests

| Part | Test item | Result |
|---|---|---|
| `cat2-graph-skeleton` 0.1.6 | G-4: inner `SubgraphError` の raw error_log marker が外側 `error_log` / `error_code` に投影されない | PASS |
| `evidence-gate` 0.3.1 | `strict.py` を `-W error::SyntaxWarning -W error::DeprecationWarning` で compile して invalid escape を検出する | PASS |

既知の残余リスクは、過去結論を読んだ LLM が本件の正しい source ID を添えて結論的な自由文を作ることを、自由文の構造だけでは止められない点である。これは設計どおり業務プロセス側の人間レビューが担う。

## Test Execution Summary

- Execution date: 2026-08-14
- Total tests: `pytest tests/ -q` collected 334 tests.
- Result in this restricted sandbox: 332 passed / 2 skipped. The 2 skips are PB-7 auto-waivers because `config/config.yaml` has `hitl.enabled: false`.
- Coverage: line coverage は未計測。上記 TC/PB/AT を実装済み。

## 実 LLM 1 回転検証（live LLM check）

`config/agent.yaml` の `generation_mode: "llm"` 宣言に対する実挙動の裏取り。既存テストの LLM double は
常に整形済みの応答を返すため、「プロンプトが出力契約を実際に導けるか」は実 LLM を通さないと分からない。

| 項目 | 値 |
|---|---|
| 実施日 | 2026-08-20 |
| 使用 LLM | ローカル CLI（Azure 上のモデル）を `BaseLLM` 実装として注入 |
| SDK | 実施時点の CI の `AGENTCORE_WHEEL_SPEC`（当時 `agenticstar-agentcore[anthropic]==1.0.1`。現行は 1.0.3） |
| 注入シーム | `config["llm"]` → `EngagementWorkflowGraph.register_nodes()` → `LlmDraftStageNode`（`llm.complete([{role, content}])`） |
| LLM 呼び出し回数 | 1 |
| 結果 | ✅ `status: success`・`degradation_reason` なし（縮退していない） |
| 非空だった出力キー | `policy_clauses`, `evidence_sources`, `candidate_conflicts`, `gaps`, `verification_questions`, `mode`, `gate_kind` |

`src/` の変更は不要だった。注入シームは canonical な `BaseLLM.complete(messages: list) -> dict` を
そのまま受けており、実 LLM は 1 回の呼び出しで closed contract に適合した JSON を返した。

### 実 LLM が実際に出した業務内容（縮退していないことの中身）

- `candidate_conflicts`: 監査クライアント子会社の統制設計を後に監査チームがレビューする構図を
  **self-review threat の候補**として検出（`clause_id: self-review-threat`、チーム申告は「利益相反なし」だったにもかかわらず）
- `gaps`: 実質的支配者（UBO）を特定する KYC 証跡の不足を検出（`clause_id: client-identity`）
- `verification_questions`: 3 条項それぞれに対し、提出済み証跡を名指しした確認質問を生成

`claim_id` は AG 側が Python で採番しており、LLM には決定論的な計算・ID 生成を委譲していない。

### 再現手順

```bash
export AZURE_OPENAI_API_KEY=<key>
python3 work/scripts/mrb_live_llm_check.py <repo>
```

`tests/conftest.py` の `agent_config` / `case_payload` fixture が、承認方針 3 条項・過去相談 1 件・
証跡宣言 2 件（KYC 欠落）を含む受嘱審査ケースを payload_store 経由で seed する。

## llm-draft-stage 0.3.0 の回帰テスト（LLM 出力の複数 JSON）

AGENTIC STAR の登録モデルが draft JSON を改行区切りで 2 回連結して返す、または JSON の後ろに `{` `}` を含む散文を付ける応答で、
Stage の parse が `Extra data` になり fail-closed していた。部品 `llm-draft-stage` を 0.3.0 に更新し、複数候補を走査して
一意な非空 object だけを受理する（相異なる複数は `AmbiguousJsonOutput` で fail-closed・`{}` / NaN / 配列は不受理）。
対応テスト: `tests/parts/llm-draft-stage/test_draft_node.py`（実物 fixture `fixtures/gpt54mini_duplicated_decision.txt` を含む）。
