# Template Design Specification — SVC-C2-106 Professional Services Engagement Acceptance Evidence Agent

> **設計確定日**: 2026-07-23
> 本設計書は decision.md §12 の design-entry blocker (a)〜(e) を schema / API / テストに落とすことを目的とする。
> SDK 契約の実機確認結果は `work/05_sdk_spike.md`（AgentCore SDK 1.0.0 実体・2026-07-27 確認）に準拠する。

## Position in AgentCore Architecture

- **Agent Class**: `EngagementAcceptanceEvidenceGraph`（`src/graph/graph.py`）
- **L1 Base**: `AgentBaseGraph`（L1 直継承）
  - `AutonomousBaseGraph` は採らない。本テンプレは自律ループではなく、モードごとに終端が決まる固定パイプラインである。
- **Three-Layer Separation**:
  - **State**: flat TypedDict の合成（`AgentState` を継承・Pydantic 不可＝msgpack 非互換）
  - **Node**: L1 継承（Template Method — `execute(self, state) -> dict` のみ override）
  - **Graph**: 合成（`register_nodes()` で 3 スロットを埋める。`add_edges()` / `route()` は override しない）

## Architecture Overview

### 3 モード

| モード | 目的 | 主な作用 |
|---|---|---|
| `ingest` | 顧客承認済みの受任方針（1 版）・過去受任レビュー観点を構造化して蓄積する | 顧客専用 DB へ原記録を append、KB へ retrieval-safe projection を書込 |
| `invoke` | 新規受任案件の証跡・案件記述を承認済み受任方針に照合し、証拠ブリーフをドラフトする | Stage 1〜4 を実行し、非結論の 5 区分出力を返す |
| `feedback` | 受任受付担当が確定・編集した論点を、明示承認 + 監査イベント付きで蓄積する | `approved is True` のときのみ KB へ反映し監査イベントを記録 |

### Node Configuration（outer graph — 固定 5 ノード backbone）

| Node | 責務 | Input State | Output State | Inherits/Overrides |
|------|------|-------------|--------------|-------------------|
| `initialize` | framework 既定 | — | — | `InitializeNode`（既定・注入） |
| `pre_process` | mode allowlist 検証・顧客解決 fail-closed・パケット形状検証・allowlist 射影 | `user_input`, `caller_id` | `mode`, `case_ref`, `validated_input`, `payload_ref` | `FunctionNode` → `EngagementIntakePreProcessNode` |
| `main` | 内側 domain workflow subgraph への委譲 | 上記 4 キー | モードスコープの結果キー | `GraphNode` → `EngagementWorkflowGraphNode` |
| `post_process` | S-3 出力安全契約の検査 + `formatted_output` envelope の整形 | main の結果キー | `formatted_output` | `FunctionNode` → `EvidenceBriefPostProcessNode` |
| `finalize` | framework 既定 | — | — | `FinalizeNode`（既定・注入） |

> `add_edges()` / `route()` は **override しない**。framework 既定の
> `START → initialize → pre_process → main → {route} → post_process → finalize → END` をそのまま使う。

### Node Configuration（inner `EngagementWorkflowGraph` — 独自トポロジ）

| Stage | Node | 責務 | LLM |
|---|---|---|---|
| Stage 1 | `deterministic_check` | 必須証跡の有無・書式・証跡種別の定型要件を決定論的に検証し `gaps` を出す。衝突・充足の意味的結論は出さない | 不要 |
| Stage 2 | `recall_perspectives` | 顧客専用 KB から過去受任レビューの **3 点セット（相談内容・結論・判定理由）** を想起する。**検索軸には結論を使わない**（§過去事例の 3 点セット） | 不要 |
| Stage 3 | `applicability_draft` | 案件叙述・証跡と承認済み受任方針を突き合わせ、候補的衝突・不足・矛盾・確認質問をドラフトする | **必要（本テンプレ唯一の LLM 核）** |
| Stage 4 | `evidence_validation` | claim→source 対応・source ID 実在・現在案件スコープを機械検証する。不整合は fail-closed または `requires_human_review` | 不要 |
| （ingest） | `accumulate_ingest` | 品質ゲート → サニタイズ → 正規化 → 書込 → `ingest_summary` | 不要 |
| （feedback） | `accumulate_feedback` | `approved is True` 検証 → KB 反映 → 監査イベント → `feedback_summary` | 不要 |

> **LLM は Stage 3 のみ。** Stage 1/2/4 は非 LLM のため単体テスト可能であり、
> 「LLM が必要な理由」を Stage 3 の当てはめ判断に限定できる（レビュー観点: ドメイン精度）。
> Stage 3 の `candidate_conflicts` は `source_ids` を 1 件以上必須とし、prompt の JSON schema にも
> `minItems: 1` を宣言する。証拠不在を示す `gaps` / `verification_questions` は登録済み `clause_id` を必須とした上で `source_ids: []` を許す。

### inner の `BaseGraph` ABC 実装

| メソッド | 実装方針 |
|---|---|
| `name` | `"engagement_workflow"` |
| `state_schema` | `EngagementWorkflowState`（inner 専用 flat TypedDict） |
| `_validate_config` | **顧客非依存**の注入物のみ検証する: `resource_resolver`（後述）/ `payload_store` / `top_k` / `llm`。**`policy_edition_ref` は検証しない**（版は呼出ごとの入力であり config ではない）。**`db_client` / `kb_client` も検証しない**（顧客専用接続は呼出ごとに resolver から取得する） |
| `register_nodes` | 上記 6 ノードを登録 |
| `add_edges` | mode 分岐 + Stage 1 → 2 → 3 → 4 → END の直列 |
| `route` | `{"invoke": "deterministic_check", "ingest": "accumulate_ingest", "feedback": "accumulate_feedback"}.get(mode, END)` の **辞書 allowlist**（未知 mode は END へ落ちる。denylist ではない） |
| `get_output` | 実行されたモードのフィールドだけを返す（None 埋めしない） |

> 🔴 **顧客専用の接続を graph / node にキャッシュしない（最重要のセキュリティ契約）。**
> `GraphNode` の `self._subgraph` キャッシュに `db_client` / `kb_client` を握らせると、
> **次の呼出（別顧客）でも同じ接続が再利用され得る**。node の instance 変数は
> グラフ実行間で共有されるため、フレームワークの禁止事項（可変状態を node instance 変数に置かない）にも抵触する。
>
> **正しい契約:** graph / node が保持してよいのは**顧客非依存の `AuthorizedResourceResolver` だけ**。
> 各 Stage は `execute()` 内で `resolver.for_caller(ctx.caller_id)` を呼び、
> **その呼出限りの**顧客専用接続（`db` / `kb` / `payload_store`）を取得してローカル変数で使う。
> `self._subgraph` にキャッシュしてよいのは**トポロジ（ノード構成）だけ**であり、接続や顧客文脈は含めない。

**`AuthorizedResourceResolver` の契約（実装フェーズの対象）:**

```python
class AuthorizedResourceResolver:                       # 顧客非依存・graph に注入してよい唯一の resource seam
    def for_caller(self, caller_id: str) -> "ResourceScope": ...
        # 1. caller_id → 認可された customer を解決（マッピング不在なら AuthorizationError で fail-closed）
        # 2. その customer に provision 済みの専用 DB / KB / payload_store ハンドルを返す
        # 3. default / fallback の接続は返さない（存在しない）

class ResourceScope:                                    # 呼出限りのローカル変数。State にも self にも置かない
    customer_ref: str                                   # 認可済み顧客（入力の customer_id は照合のみに使う）
    db:  "EngagementDbClient"
    kb:  "EngagementKbClient"
    payloads: "PayloadStore"
```

**受入テスト（必須）:** caller A で 1 回実行 → 同一プロセスで caller B を実行したとき、
B が A の接続・payload・KB レコードに一切到達できないことを統合テストで固定する
（同一 graph インスタンスの連続実行で検証する。ここを再生成前提のテストにすると意味がない）。

> ⚠️ inner の `route()` / `get_output()` の `state` 引数は **`EngagementWorkflowState` で注釈する**。
> `AgentState` で注釈すると射影でカスタム欄が消え誤 route する（Business Travel Hotel & Plan Recommendation Agent 実例）。

### Node 別 `required_trust_level`（S-1）

| Node | `required_trust_level` | 理由 |
|---|---|---|
| `EngagementIntakePreProcessNode` | `VERIFIED_EXTERNAL` | 顧客識別の解決を行う。匿名呼出は受け付けない |
| `EngagementWorkflowGraphNode` | `VERIFIED_EXTERNAL` | GraphNode 既定は `ANONYMOUS` のため**明示宣言必須**（既定のままだと内側 S-1 と不整合になる実バグ前例あり） |
| `EvidenceBriefPostProcessNode` | `VERIFIED_EXTERNAL` | 顧客の機微情報を含む出力を扱う |
| inner の全 Stage ノード | `VERIFIED_EXTERNAL` | KB/DB 読書と LLM 呼出を伴う |

有効値は `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL` の 3 つのみ（`TrustLevel` enum の全値）。

### Data Flow

```
[受任受付担当者]
   │  POST /ingest | /invoke | /feedback   (Bearer 認証)
   ▼
src/api/server.py  ── 認証(caller_id) → 認可顧客の解決 → 顧客専用 DB/KB 接続の取得
                   └─ 機微本文を payload_store に put → State には payload_ref のみ
   ▼
START → initialize → pre_process → main(GraphNode) → post_process → finalize → END
                         │              │                  │
                         │              ▼                  ▼
                         │      EngagementWorkflowGraph   S-3 closed allowlist
                         │      ├ invoke:   Stage1→2→3→4    + claim→source 実在
                         │      ├ ingest:   accumulate_ingest + severity=candidate 固定
                         │      └ feedback: accumulate_feedback → formatted_output envelope
                         ▼
                  mode allowlist / 顧客照合 fail-closed
```

### State Definition

`src/schemas/state.py` — `class State(AgentState, total=False)` + `NotRequired[...]`

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | `ingest` / `invoke` / `feedback` の 3 値 | ✅ |
| `case_ref` | `str` | 受任案件の参照 ID（**英字のみ**。理由は下記注記） | ✅ |
| `customer_ref` | `str` | entry-adapter が検証した payload/KB scope。client 値は scrubber が破棄し trusted 値を注入 | ✅ |
| `validated_input` | `str` | pre_process が allowlist 射影したパケットの JSON 文字列（inner `extract_input` 用） | ✅ |
| `payload_ref` | `str` | 機微本文（案件叙述・KYC 資料本文）の opaque handle。**server が発行**しクライアント供給値は破棄 | — |
| `policy_edition_ref` | `str` | 適用する承認済み受任方針の版参照 | invoke 時 |
| `evidence_declarations` | `list[dict]` | 提出された証跡の宣言（種別・source_id・提出日） | invoke 時 |
| `docs_present` | `dict` | Stage 1 の必須証跡有無の判定結果 | — |
| `format_check` | `dict` | Stage 1 の書式検証結果 | — |
| `recall_refs` | `list[dict]` | Stage 2 が想起した過去事例の 3 点セット。各要素は **`history_ref`**（`source_id` と名前空間が重ならない識別子。例 `HIST-<英字>`）/ `clause_id` / `policy_edition_ref` / `consultation_summary` / `decision_outcome` / `decision_rationale_safe`。**`source_registry` には入れない**（＝本件の証拠として引用できない） | — |
| `retrieved_records` | `list[dict]` | evidence-gate 契約キー（引用 ID 実在照合の突合先） | — |
| `source_registry` | `dict[str, dict]` | source_id → `{doc_ref, span_ref, provenance}` の**決定論テーブル**。Stage 1 が `evidence_records` から生成し、以降 **immutable**。**S-3 の照合基準**であり、LLM はここから**選択するだけ**（採番させない） | invoke 時 |
| `cited_source_ids` | `list[str]` | evidence-gate 契約キー。**検証を通過した claim の `source_ids` から post_process が再計算**する（LLM の自己申告値は使わない） | — |
| `evidence_brief` | `dict` | Stage 3 のドラフト（5 区分） | — |
| `evidence_map` | `list[dict]` | evidence-gate が検証する claim → source の対応マップ | — |
| `requires_human_review` | `bool` | Stage 4 が不整合を検出したときに立てる | — |
| `degraded` | `bool` | 縮退したか（envelope の `meta.degraded` に載る） | — |
| `degradation_code` | `str` | 縮退理由の**有限 enum**（`kb_empty` / `llm_unavailable` / `finding_dropped_unknown_source`）。`status` は `success` のまま | — |
| `ingest_summary` | `dict` | `written_ids` / `rejected[]` | ingest 時 |
| `feedback_summary` | `dict` | 反映件数・監査イベント ID | feedback 時 |
| `error_code` / `error_type` | `str` | LLM/provider 等の失敗を自由文なしの安定メタデータで返す | error 時 |

**State Constraints（必須）:**
- flat TypedDict のみ（primitive + JSON 直列化可能な型）
- JWT / API key / credential を State に置かない（checkpoint DB に平文で残る）
- `InvocationContext` は `config["configurable"]` 経由のみ（State に入れない）
- Pydantic / dataclass / 任意 Python オブジェクトを置かない（msgpack 非互換）
- `hitl_count` はテンプレから書かない（framework 専用）
- `node_history` / `error_log` は reducer（`operator.add`）— `execute()` は**変更したキーだけ返す**（full state 返却は履歴を二重化する）

> 🔴 **参照 ID は英字のみで発行する。** framework の S-2 ゲート（`FunctionNode._security_gate_input`・`@final`）は
> `_PII_SCAN_FIELDS` を `detect_pii` で走査し、検出範囲を **`[MASKED]` に破壊的置換**する。
> `uuid4()` のように数字を含む ref は電話番号等として誤検知され破壊される既知欠陥がある
> （他テンプレートでの実測 0.58% の本番失敗）。`case_ref` / `payload_ref` は英字のみの符号化とする
> （parts `payload-store` v0.4.0 と同方針）。

### 出力契約（`formatted_output` envelope）

```yaml
formatted_output:
  mode: "invoke"                                 # 必須
  gate_kind: "engagement_acceptance_evidence"    # 必須（evidence-gate の宣言）
  meta:                  { degraded, degradation_code }        # 閉じた 2 キー
  policy_clauses:        [ { clause_id, policy_edition_ref, clause_ref } ]        # 案件 evidence の source_id は持たない
  evidence_sources:      [ { source_id, doc_type, quote_span, provenance } ]
  candidate_conflicts:   [ { claim_id, description, clause_id, source_ids, severity: "candidate", confidence } ]
  gaps:                  [ { claim_id, description, clause_id, source_ids } ]
  verification_questions:[ { claim_id, question, clause_id, source_ids } ]
```

> `get_output()` は `formatted_output` → `result` の順で `output` を拾う（SDK 1.0.0 実体で確認）。
> したがって最終 envelope は **`formatted_output`** に載せる。
>
> 縮退（KB 空振り、LLM injection seam 自体が未設定、または未知 ID だけを引用した candidate finding の除去）は `meta.degraded` / `meta.degradation_code` で伝える。
> 注入済み LLM の provider/auth/config/timeout/runtime 失敗は縮退させず
> `status="error"` + `error_code="llm_call_failed"` で fail-closed にする。
> **top-level に `degradation_reason` のような追加キーを生やさない**（S-3 の closed allowlist が reject するため）。
> 各ネストオブジェクトの exact-key 定義は §S-3 出力安全契約を正とする。
>
> `invoke` の case / evidence registry と claim snapshot は Stage 1 では永続化しない。
> post_process の closed schema・provenance 検査がすべて通った後に同じ commit point で保存し、
> 保存途中の失敗は `case_ref` 単位で rollback する。S-3 拒否時には case / evidence / snapshot を残さない。

**出力スキーマに持たないフィールド（責任境界の構造排除）:**
`acceptance_verdict` / `conflict_final` / `legal_advice` / `independence_impairment_verdict` /
`aml_risk_rating` / `staffing_decision`

## Framework Utilization

### Shared Components Used
- [x] `InvocationContext`（`correlation_id`, `session_id`, `caller_id`, `caller_trust_level`, `secrets`, `hitl_allowed`）
      — **`tenant_id` / `org_id` は存在しない**（SDK 1.0.0 実機確認）。顧客識別は `caller_id` からの認可解決による
- [x] S-2: `_extra_security_gate_input()` — mode allowlist・顧客照合 fail-closed・パケット形状検証
- [x] S-3: `_extra_security_gate_output()` — 出力の closed allowlist・claim→source 実在・禁止キー再帰排除
- [x] S-4: `emit_trace_event()` — 各 `execute()` 内で最低 1 回（`node_start` / `node_complete` / `node_error` は
      framework が `__call__()` で出すので**重複させない**）
- [x] `framework.security.detect_pii` / `detect_credentials` — `FunctionNode` が自動実行する。
      **自前の PII/credential 正規表現は書かない**（フレームワークの検知器を再利用する）
- [x] `shared.services.llm`（`BaseLLM`）— LLM は `config["llm"]` の注入シームのみ。
      モデル ID / 具体クライアントを `src/` に書かない

> **S-2/S-3 のノード種別ごとの挙動（ADR-017）:**
> - `FunctionNode` サブクラス → framework の `@final` ゲートが必ず走る。拡張は `_extra_*` フックのみ
> - `GraphNode` → 意図的な no-op（上流/子側のゲートが適用済み）
> - `_security_gate_input/output` を直接定義すると **import 時 `TypeError`**

> **非結論性は「構造」で担保し、自由文の denylist に依存しない。**
> 禁止語・禁止フレーズの正規表現は言い換えで原理的にすり抜けるため**持たない**（Logistics Dangerous-Goods Document Consistency Agent / Financial Regulatory Consultation Response Evidence Brief Agent 判例）。
> 詳細は §S-3 出力安全契約。

### Composition Pattern

- **Pattern**: `GraphNode`（in-process subgraph）
- **Composition target**: `EngagementWorkflowGraph`（`src/graph/domain_workflow_graph.py`）
- **`error_strategy`**: `"propagate"` — 内側でエラーが出たら部分結果を返さず全体を error にする
  （証拠ブリーフは部分的に正しい出力の方が危険なため）
- **`propagate_hitl`**: `False`（HITL 非採用のため）
- **abstract 3 メソッド**: `get_subgraph()`（lazy + キャッシュ）/ `extract_input()`（`validated_input` を渡す）/
  `merge_output()`（**実行されたモードのフィールドだけ返す**・`node_history` は返さない）

> `get_subgraph()` は `self._subgraph is None` のときだけ生成してキャッシュする
> （毎 execute で新規生成すると resume が収束しない）。

## HITL 方針

**HITL は採用しない（`hitl.enabled: false`）。** 根拠:

1. SVC は HITL Gate の必須対象業種（HCR/INS/FIN/GOV）ではない
2. 出力は常に非結論（`severity` は `candidate` 固定）であり、受任可否は業務プロセス上必ず人間が別途判断する。
   グラフ内で人間を待たせる `interrupt()` は業務価値を足さない
3. 人間のレビュー結果は **`/feedback` モード（非同期）** で明示承認 + 監査イベント付きに蓄積する。
   受任受入レビューのターン間隔は数時間〜数日であり、checkpoint に吊り下げる方式は不適
   （PCRepairIntakeTriageAgent 判例と同型）

**帰結:** PB-7 の assertion は **auto-waived — non-HITL**（PB-7 は HITL 有効時のみ必須）。
`memory_enabled` も `false` でよい。

## AGENTIC STAR Marketplace entry adapter

リポジトリ直下の `cli.py` は `EngagementAcceptanceEvidenceGraph` を継承する
`MarketplaceGraph` を公開する。Marketplace runner が渡す `input_context` は利用せず、
`conversation_history` を含めて破棄した上で、`src.entry_adapter` の `SCOPE_KEYS` と
`STG_DEFAULT_SCOPE_ENVS` が宣言した値だけを process environment から再構築する。
caller は `STG_DEFAULT_CALLER_ID` から解決し、`InvocationContext` の trust は
`VERIFIED_EXTERNAL`、`hitl_allowed` は `false` に固定する。scope/caller の未設定、空文字、
または空白文字を含む値は Graph 構築時と `provision_secrets()` 時に fail-closed とする。

Marketplace から公開するのは `invoke` のみで、JSON 文字列中の `ingest` / `feedback` は
Graph に渡す前に拒否する。予期せず `AWAITING_HUMAN` が返った場合も成功へ変換せず失敗とする。
Azure OpenAI の接続値は allowlist provider 経由で呼出しごとに取得し、client も毎回生成する。
接続値および client は `MarketplaceAzureLLM` のインスタンスに保持しない。

SDK 1.0.1 には `load_agent_config`、Marketplace bootstrap、environment provider、
`AzureOpenAIClient` がないため、adapter 内に 1.0.0/1.0.1 用の互換境界を置く。
Azure client だけを LLM メソッド内で遅延 import し、framework・security・graph の import は
module load 時に完了させる。

この adapter は LLM の注入シームだけを Azure に差し替える。受任可否を出力するフィールドは
引き続き closed schema に存在せず、証跡不足の抽出、clause/source 照合、provenance 検証、
`severity="candidate"` の強制は既存の決定論的 Stage 1 / Stage 3 / S-3 gate が担当する。
したがって LLM は候補ブリーフを下書きするだけで、検収・受任可否の判定境界には入らない。

> ⚠️ **PB-7 のスタブファイル自体は削除しない。**
> `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` は HITL 無効でも
> **exact-name で必須**（`gate-scaffold-integrity` の `REQUIRED_STG_COMPLETE` に含まれる）。
> `hitl.enabled` が false のときに `pytest.mark.skipif` で自動 skip されるのが正しい状態であり、
> 「HITL を使わないから不要」とファイルごと消すと CI が赤化する。
> あわせて scaffold 同梱の `scripts/test_pb7_scaffold_stub.py` は PB-7 ファイルの
> `_CONFIG_PATH` / `_hitl_enabled()` を assert するため、この API を落とさないこと。

## Import Isolation Confirmation
- [x] Level 0（`agenticstar`）を import しない
- [x] import 先は `framework/` と `shared/` のみ（`agents/base/` は不要）
- [x] 他テンプレを直接 import しない
- [x] 写経した parts は `src/<part>/` に置き、消費側は **`from src.<part> import ...` に統一**
      （top-level import は editable install の shim 依存になり非 editable 経路で `ModuleNotFoundError`）

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | `AgentBaseGraph` | `AutonomousBaseGraph` | **A** | 自律ループではなくモードごとに終端が決まる固定パイプライン |
| Composition pattern | `GraphNode`（内側 subgraph） | 3 スロットに直接実装 | **A** | Stage 1〜4 の直列 + 3 モード分岐を内側の独自トポロジで表現する必要がある（Cat2 標準） |
| 顧客分離 | 接続レベル分離（顧客専用 DB/KB） | 行レベル `tenant_id` フィルタ | **A** | 既存テンプレートの既定（Customs Shipment Document Completeness Validator / Return Fraud Investigation Memo Drafting Agent）。`InvocationContext` に `tenant_id` が無いことも実機確認済み |
| S-3 の主防御 | closed allowlist（構造） | 禁止語 denylist | **A** | denylist は言い換えで原理的に閉じない（Logistics Dangerous-Goods Document Consistency Agent / Financial Regulatory Consultation Response Evidence Brief Agent 判例）。denylist は**持たない** |
| HITL | 非採用 + `/feedback` 非同期 | D6 `interrupt()` | **A** | 出力が非結論・ターン間隔が数時間〜数日（§HITL 方針） |
| `error_strategy` | `propagate` | `handle` | **A** | 証拠ブリーフの部分結果は誤読リスクが高い |
| source ID の採番 | `source_registry` から LLM が**選択**のみ | LLM が採番 | **A** | LLM に採番させると捏造 ID を作れてしまう |
| 過去実績の KB 保持 | 3 点セット（相談内容・結論・判定理由） | 判定理由のみ（結論を除外） | **A** | ユーザー裁定 2026-07-27。理由だけ切り出すと観点として成立しない（「なぜ重要だったか」は結論に紐づく）。(B) では蓄積型の moat が立たず Stage 2 が汎用 RAG に退化する |
| 過去結論を Stage 3 prompt に入れるか | 入れる（利便性優先） | 入れない（引きずり回避優先） | **A** | ユーザー裁定 2026-07-27。間接継承は prompt から除外することではなく、**検索軸・証拠適格・prompt 上の役割分離**の 3 箇所で止める（§過去事例の 3 点セット） |
| 入力ファイル形式 | csv 等の軽量テキストのみ | PDF / xlsx も受理 | **A** | パーサ増・OCR 崩れ・依存膨張を避けるための既定方針 |

## データ層設計（blocker (a) / (d)）

**DB / KB はマルチテナント共有ストアではなく、顧客専用（物理分離）に provision された接続である。**
分離は **KB / DB 接続レベル**で担保する — 呼出元を認証（`caller_id`）→ 認可された顧客を解決 →
その顧客に **provision 済みの専用接続**を使う。入力に含まれる `customer_id` 等は
**認可済み顧客との照合のみ**に使い、行を絞り込む検索キーには使わない。
**default KB は持たない**（認可解決不能なら fail-closed）。
したがって parts の `scoped-retrieval` / `payload-store` の `scope_keys`（1 顧客 KB 内の区画分割用）は導入しない。

**顧客 → KB 接続の選択契約（セキュリティ核・実装フェーズ必須）:**
- `caller_id` は `InvocationContext` から取得する
- **入力パケット由来の `customer_id` で接続先 KB を選んではならない**（他社 KB への横断接続を防ぐ）
- 不一致・不明・未マッピングは fail-closed（default / fallback KB を持たない）

### DB schema（顧客専用接続内・原記録・append only）

> 各テーブルは**認可済み顧客専用の接続内**に存在する（行レベルの tenant カラムで共有ストアを分割するのではない）。

| テーブル | 主なカラム | 用途 |
|---|---|---|
| `policy_editions` | `policy_edition_ref`(PK), `profession_profile`, `status`, `approved_at`, `approved_by` | 受任方針の**版そのもの**の台帳 |
| `policy_clauses` | **PK(`policy_edition_ref`, `clause_id`)**, `clause_text`, `clause_ref` / FK → `policy_editions` | 版に属する条項。1 版に複数条項を持てる |
| `engagement_cases` | `case_ref`(PK), `policy_edition_ref`(FK), `submitted_at`, `evidence_declarations` | 受任案件の受付記録。**どの版で照合したか**を持つ |
| `evidence_records` | `source_id`(PK), `case_ref`(FK), `doc_type`, `doc_ref`, `span_ref`, `received_at` | 証跡の原記録。**`source_registry` の生成元**（S-3 の照合基準） |
| `review_perspectives` | `perspective_id`(PK), `case_ref`(FK, NOT NULL), **`policy_edition_ref`(FK, NOT NULL)**, `clause_id`(FK→`policy_clauses` 複合), `perspective_code`(NOT NULL, CHECK enum), `consultation_summary`(NOT NULL), **`decision_outcome`**(NOT NULL, **CHECK IN ('accepted','declined','accepted_with_safeguards','withdrawn')**), **`decision_rationale_raw`**(原文・監査用), **`decision_rationale_audit`**(PII マスク済), **`decision_rationale_safe`**(model 可視・長さ上限), `source_id`(FK), `decided_by`(**サーバ設定**), `decided_at`(**サーバ時刻**), `recorded_at` | 過去受任レビューの **3 点セット**（相談内容・結論・判定理由）。§過去事例の 3 点セット |
| `claim_snapshots` | `claim_id`(PK・**英字のみ・サーバ採番**), `case_ref`(FK), `customer_ref`, `policy_edition_ref`(FK), `clause_id`, `source_ids`, `text`, `claim_type` | invoke の **S-3 通過後**に保存する claim の**不変スナップショット**。`/feedback` はこれと全一致する `claim_id` のみ受理し、`consultation_summary` はここの `text` から転記する（クライアント値を使わない） |
| `feedback_audit_events` | `event_id`(PK), `case_ref`(FK), `actor`, `action`, `approved`, `recorded_at` | feedback の監査証跡 |

> **版の混同を DB 制約で防ぐ:** `policy_clauses` は複合 PK `(policy_edition_ref, clause_id)` を持ち、
> `review_perspectives` は `policy_edition_ref` を**必須列**として持つ（untyped な `provenance` に版を埋めない）。
> Stage 2 の KB 検索・Stage 3 の条項リストは常に `policy_edition_ref` を条件に含める。

### 過去事例の 3 点セット（相談内容・結論・判定理由）

> 🔴 **設計判断（2026-07-27 ユーザー裁定）。** 過去の受任レビュー実績は、業務上
> **「相談内容 / 結論 / 判定理由」の 3 点セット**で初めて意味を持つ。
> 「なぜその論点が重要だったか」は結論に紐づくため、**理由だけ切り出して結論を隠すと観点として成立しない**。
> したがって KB には 3 点セットを保持し、**Stage 3 の prompt にも過去事例の結論を含める**（利便性を優先）。

| 3 点セット | KB 列 | 内容 |
|---|---|---|
| 相談内容 | `consultation_summary` | 過去案件の叙述の**要約**（依頼者の生の識別情報は含めない） |
| 結論 | `decision_outcome` | 有限 enum: `accepted` / `declined` / `accepted_with_safeguards` / `withdrawn` |
| 判定理由 | `decision_rationale`（**3 値に分離**: `_raw` / `_audit` / `_safe`。下記参照） | 責任者がその結論に至った理由（`clause_id` に紐づく）。model が見るのは `_safe` のみ |

**この決定が持ち込むリスクと、実際に止まる範囲 / 止まらない範囲**

「過去結論を prompt に入れる」ことは、**過去結論の間接継承**（「去年 B 社を断ったから今年も断るべき」）の
リスクを持ち込む。受任可否は依頼者・時期・他案件との組合せで変わるため、引きずりは実害になる。
**prompt から除外することでは止めない**と決めた以上、止まる範囲と止まらない範囲を正直に分けて書く。

**止まる（機構で担保する）:**

| # | 対象 | 実装 | テスト |
|---|---|---|---|
| 1 | **検索順位が結論語彙に引きずられること** | KB は **検索テキストと表示 payload を分離**する。ベクトル化・全文索引の対象は `retrieval_text = clause_id + consultation_summary + perspective_code` **のみ**で、`decision_outcome` / `decision_rationale_safe` は **embedding に含めない**（top-k 確定後に `perspective_id` で引く表示用 payload）。`decision_outcome` で絞り込む検索も実装しない | AT-4b |
| 2 | **過去事例を本件の証拠として引用すること** | `recall_refs` の識別子は `history_ref`（**`source_id` とは名前空間が重ならない**）とし、`source_registry` に入れない。S-3 の拒否条件 #4/#5 は `source_registry` との完全一致を要求するため、`source_ids` に `history_ref` を挙げると **reject** される | AT-4c |
| 3 | **本件の結論フィールドが生成されること** | S-3 の closed allowlist（`acceptance_verdict` 等をスキーマに持たない・`severity` は `candidate` 固定） | §S-3 |

**止まらない（残余リスクとして受容する）:**

> 🔴 **過去の `declined` を読んだ LLM が、本件の正しい `source_id` を添えて
> 「受任を見送るべき事情がある」と自由文（`description`）に書く経路は、上記の機構では止まらない。**
> S-3 が検証するのは source ID の実在・現在案件・条項整合であって、
> 「その自由文がその証拠から導かれるか」ではないためである。
>
> これを止めるには、過去事例を観点コード選択や人間向け別ペインに限定し、`description` / `question` を
> **本件の証跡だけを渡す別パスで生成**する構成が要る。しかしそれは
> ①ユーザー裁定（過去結論を Stage 3 に入れる／利便性優先）に反し、
> ②HCR-C2-060 で巻き戻された「自由文を有限コード + テンプレート描画に置換」に接近する。
> したがって**採らず、残余リスクとして明示する**。
>
> **正確な言い方:** 本設計が構造的に止めるのは「**過去事例の直接引用**」と「**本件の結論フィールドの生成**」であり、
> 「**自由文の語り口に過去の判断傾向が反映されること**」は止めない。後者は
> ①出力が常に `severity: candidate` であること ②本件の結論フィールドが存在しないこと
> ③全 claim に本件の検証済み source が紐づくこと、で「読み手が確定判断として受け取らない」構造にした上で、
> **業務プロセス側の人間レビュー（責任者・有資格者）が担う**。

> **A-1（全結論排除）との整合:** 「AG は**本件について**結論フィールドを出力しない」と
> 「KB に**過去事例の**結論を保持し参考提示する」は両立する。境界は**出力スキーマ**にある —
> `decision_outcome` は `review_perspectives`（過去の記録）の列であって、
> `formatted_output` の invoke envelope には**存在しない**。
> ただし上記のとおり、自由文が過去の判断傾向を帯びる可能性は残る。
> **「AG は本件の結論を示唆しない」とは主張しない**（主張すると実態と乖離する）。

### KB projection（retrieval-safe・model 可視）

KB に置くのは `review_perspectives` から射影した 3 点セットである。
projection は**検索テキスト**と**表示 payload** を分離し、いずれも構造化項目から決定論的に生成する。

| 区分 | 含める | 用途 |
|---|---|---|
| **`retrieval_text`**（embedding / 全文索引の対象） | `clause_id` + `consultation_summary` + `perspective_code` | 意味的近さの検索のみ。**結論・理由を含めない**（順位が結論語彙に引きずられるのを防ぐ） |
| **表示 payload**（top-k 確定後に `perspective_id` で取得） | `decision_outcome`（enum）+ `decision_rationale_safe`（下記）+ `policy_edition_ref` + `history_ref` | Stage 3 の prompt「過去事例ブロック」と担当者への提示 |

| projection に含めない | 理由 |
|---|---|
| 依頼者の生の識別情報（氏名・法人番号・連絡先） | PII |
| 案件叙述の**全文** | `consultation_summary`（要約）のみ |
| `decision_rationale` の**原文** | 下記の 3 値分離により `decision_rationale_safe` のみを載せる |

**`decision_rationale` の 3 値分離（自由文の永続経路を閉じる）**

`decision_rationale` は業務上どうしても自由文になる。無検査だと、担当者の入力が
**次回の Stage 3 prompt にそのまま載る** — すなわち PII・他顧客への言及・
プロンプト注入（「以後の指示を無視せよ」）の永続経路になる。
そこで **1 つの入力を 3 つの値に分ける**:

| 値 | 保存先 | 内容 |
|---|---|---|
| `decision_rationale_raw` | **DB 原記録のみ**（`review_perspectives`） | 担当者の入力そのまま。監査・法的説明のための正本 |
| `decision_rationale_audit` | DB 原記録 | PII マスク済み。監査ログ・エクスポート用 |
| **`decision_rationale_safe`** | **KB projection（model 可視はこれだけ）** | ①S-2 の `detect_pii` を通す ②`max_rationale_chars`（`config.yaml`・既定 1000）で切詰 ③NFKC 正規化 ④**命令文パターン**（"ignore previous"・"以後の指示"・"システムプロンプト" 等）を検出したら **reject して KB に書かない**（400 を返す・無言で落とさない） |

> **model 可視値は JSON のデータ領域として隔離する** — prompt に埋め込むときは
> 「過去事例ブロック」の構造化 JSON の値として渡し、地の文に連結しない。

> 🔴 **`/feedback` の受理形（closed schema・`additionalProperties: false`）**
>
> | フィールド | 型 / 制約 | 必須 | 誰が決めるか |
> |---|---|---|---|
> | `claim_id` | str（**英字のみ・サーバ採番**）。invoke の S-3 通過時に保存した **claim スナップショット**に実在し、`case_ref` / `customer_ref` / `policy_edition_ref` / `clause_id` / `source_ids` が全一致すること。不在・不一致は **400** | ✅ | **サーバが invoke 応答で払い出した値**をクライアントが返す |
> | `perspective_code` | enum（有限） | ✅ | クライアント |
> | `clause_id` + `policy_edition_ref` | 当該版に実在すること（FK 照合） | ✅ | クライアント |
> | `source_ids[]` | `evidence_records` に実在（**`history_ref` は不可**） | ✅ | クライアント |
> | `decision_outcome` | enum `accepted` / `declined` / `accepted_with_safeguards` / `withdrawn`。**自由文不可** | ✅ | クライアント |
> | `decision_rationale` | str・`max_rationale_chars` 以下。上記 3 値分離を適用 | ✅ | クライアント |
> | `action` | enum `approve` / `edit` / `reject` | ✅ | クライアント |
> | `approved` | bool | ✅ | クライアント |
> | `decided_by` | — | — | **サーバ**（middleware の actor から設定。クライアント指定は 400） |
> | `decided_at` | — | — | **サーバ**（サーバ時刻。クライアント指定は 400） |
>
> **有効な組合せ:** KB へ書き込むのは **`action != "reject"` かつ `approved is true`** のときのみ。
> `action == "reject"` は監査イベントのみ記録し KB projection を作らない
> （`action=reject, approved=true` のような矛盾入力は 400）。
> `consultation_summary` は AG が **claim スナップショットの text** から決定論的に転記する（クライアント指定不可）。
> `retrieval_text` も同じスナップショット由来の値から生成する。
> 上記以外のキーは**明示的に 400**（無視しない）。書込は**全部成功か全部失敗**（部分書込を作らない）。
>
> **受入テスト:** `/feedback` に未宣言キー（例: `internal_note`）を付けて送ると 400 になり、
> `decision_outcome` に enum 外の値を入れると 400 になることを assert する。

### 顧客データ統治（blocker (d)）

| 項目 | 方針 |
|---|---|
| 最小データセット | 承認済み受任方針（1 版）・過去レビュー観点・当該案件の証跡宣言。依頼者の生 KYC 資料本文は `payload_ref` 経由の一時保持のみ |
| 正本の所在 | 顧客側システム。本 AG の DB は**照合のための派生記録**であり、受任判断の正本ではない |
| 保持期間 | `config.yaml` の `retention_days` で顧客が設定（既定値は設計値であり顧客環境で調整する前提） |
| 削除 | `case_ref` 単位の削除 API を運用ガイド（`docs/07`）に記載。KB projection も同時削除 |
| 監査 | `/ingest` `/feedback` の全書込に `emit_trace_event()` の監査イベントを紐付ける |

### 機微データの 2 層統治

| 層 | 守る対象 | 手段 |
|---|---|---|
| **層 1: checkpoint 隔離** | LangGraph が DB に平文永続化する **State** | `payload_ref` 間接化。案件叙述・KYC 資料の生本文は payload store に置き、State には opaque handle のみ |
| **層 2: 永続 KB / DB の統治** | AG 自身が蓄積するテーブル | 書込前サニタイズ・fail-closed・保持期間・監査イベント |

**本文が必要な箇所での解決契約（Stage 3 の実行に必須）:**

Stage 3 は案件叙述・KYC 資料の**本文**を LLM prompt に与える必要がある。したがって解決は
「server の invoke 戻り値でだけ resolve する」では**間に合わない**（Stage 3 の後になるため）。
正しい契約は次のとおり:

```python
# API entry: client ref を捨て、allowlist scrubber が trusted scope を注入する
safe_json = scrub_checkpoint_envelope(
    raw_json, payload_store, session_id=ctx.thread_id,
    trusted_scope=auth.scope, policy=CHECKPOINT_POLICY,
)

# Stage 3: 呼出限りのローカル変数として scope/session 付きで解決する
body = scope.payloads.resolve(
    state["payload_ref"], scope=auth.scope,
    session_id=ctx.thread_id, consume=False,
)
# body は State / 戻り dict / trace / subgraph output に一切戻さない
```

| 契約 | 内容 |
|---|---|
| 発行 | `src/api/server.py` が `put()` して発行した ref のみ。**クライアント供給の `payload_ref` は無条件破棄**（「client が送ってきたらそれを使う」早期 return を作らない） |
| 解決の権限 | `resolve()` は `session_id` と `customer_ref` scope の**両方**に束縛される。いずれかが不一致なら fail-closed（`unscoped get()` は実装しない） |
| 解決先 | Stage 3 の `execute()` ローカル変数のみ。**State・戻り dict・`emit_trace_event` の payload・subgraph output に本文を載せない** |
| TTL | `config.yaml` の `payload_ttl_s`。期限切れは fail-closed（invoke は `status="error"`） |
| ref の形式 | **英字のみ**（数字を含む ref は S-2 の `detect_pii` が `[MASKED]` に破壊する） |

**受入テスト（必須）:** 実 checkpointer で invoke を走らせ、
checkpoint の全直列化に本文のセンチネル文字列が **not in** であることを 3 モードで assert する。

**gate-bypass の先回り（実装フェーズ必須）:**
1. **クライアント供給の `payload_ref` は無条件破棄**する（server が put して発行した値だけを State に載せる）
2. **checkpoint スクラバは allowlist** で書く（denylist だと新フィールド追加時に漏れる）
3. S-3 の免除条件に `.*` のようなワイルドカードを書かない

## 受任方針の版管理・専門職プロファイル指定（blocker (b)）

| 項目 | 設計 |
|---|---|
| 版管理 | `policy_editions.policy_edition_ref` が版の識別子。条項は複合 PK `(policy_edition_ref, clause_id)` の `policy_clauses` に属する |
| 版の指定 | invoke は `policy_edition_ref` を**呼出ごとの必須入力**とする。**config には置かない**（`_validate_config` の検証対象にしない）。その版の条項のみを照合対象にし、複数版の混在照合をしない |
| 専門職プロファイル | `config.yaml` の `profession_profile`（例: `audit_firm` / `law_firm` / `consultancy`）で 1 つに固定する。**人間が設定する値**であり AG が推定しない。入力の版が別プロファイルのものなら fail-closed |
| 照合ロジック | Stage 3 の prompt を **2 ブロックに分ける** — ①「当該版の条項リスト」+「当該案件の証跡・叙述」（＝候補的衝突の根拠にしてよい）②「過去事例の 3 点セット」（＝参考。本件の根拠として引用してはならない旨を明示）。`clause_id` 単位で候補的関連を出させ、条項を跨いだ総合判断はさせない |
| 版の切替 | 版が変わったら `policy_edition_ref` を変えて再 invoke する。過去版で作られた観点は `review_perspectives.policy_edition_ref`（必須列）で区別されるため混同しない |

> **スコープ固定境界**: 一顧客組織 / 一専門職プロファイル / 承認済み受任手順（1 版）。
> 「専門サービス一般の全利益相反ルール」は扱わない（decision.md T9 の閉じ方）。

## S-3 出力安全契約（blocker (c)・testable schema）

`EvidenceBriefPostProcessNode._extra_security_gate_output(result)` が以下を検査する。
**主防御は closed allowlist（構造）であり、自由文の禁止語 denylist は持たない。**

Stage 3 の LLM 応答契約は `src/services/response_contract.py` の
`FINDING_FIELDS_BY_SECTION` / `FINDING_TEXT_LIMITS` を単一の正本とする。
同じ定数から prompt 用 JSON contract と `project_typed_finding()` の型射影を生成するため、
prompt と validator の手書き二重管理を行わない。未知キー、欠落、型違い、enum 外、
`description={"raw": ...}` のようなネスト値は、本文を後段へ渡す前に fail-closed となる。

**閉じた schema（invoke モード）— ネストも含めて全オブジェクトが exact-key:**
```yaml
formatted_output:                       # top-level key は INVOKE_TOP_LEVEL_KEYS（8 キー）と完全一致（過不足とも reject）
  mode: "invoke"
  gate_kind: "engagement_acceptance_evidence"
  meta:                   { degraded: bool, degradation_code: <enum|null> }   # 閉じた 2 キー
  policy_clauses:        [ { clause_id, policy_edition_ref, clause_ref } ]        # 案件 evidence の source_id は持たない
  evidence_sources:      [ { source_id, doc_type, quote_span, provenance } ]
  candidate_conflicts:   [ { claim_id, description, clause_id, source_ids, severity, confidence } ]
  gaps:                  [ { claim_id, description, clause_id, source_ids } ]
  verification_questions:[ { claim_id, question, clause_id, source_ids } ]

# ネストされたオブジェクトも exact-key（追加キーを一切通さない）
provenance:              { case_ref, record_id, doc_ref, span_ref, policy_edition_ref }
degradation_code enum:   [ "kb_empty", "llm_unavailable", "finding_dropped_unknown_source", null ]
severity:                固定値 "candidate" のみ
```

**正規定義（実装フェーズの validator とテストはこの定数を単一の出典とする）:**

```python
# src/nodes/post_process_node.py — 他の場所でキー集合を再列挙しない
INVOKE_TOP_LEVEL_KEYS = frozenset({
    "mode", "gate_kind", "meta",
    "policy_clauses", "evidence_sources",
    "candidate_conflicts", "gaps", "verification_questions",
})                                            # 8 キー
INGEST_TOP_LEVEL_KEYS   = frozenset({"mode", "gate_kind", "meta", "ingest_summary"})      # 4 キー
FEEDBACK_TOP_LEVEL_KEYS = frozenset({"mode", "gate_kind", "meta", "feedback_summary"})    # 4 キー
```

> 数え間違いによる validator とテストの食い違いを防ぐため、**キー集合は定数で 1 回だけ定義**し、
> 拒否条件・accept 条件・受入テストはすべてこの定数を参照する（本文中で個数を再掲しない）。

**拒否条件（いずれかに該当したら `SecurityViolationError`）:**

| # | 拒否条件 | 意図 |
|---|---|---|
| 1 | top-level の key が **`INVOKE_TOP_LEVEL_KEYS`（下記の正規定義・8 キー）と完全一致しない**（過剰キーだけでなく `mode` / `gate_kind` の**欠落も reject**） | 未宣言キーを一切通さない。set 差分だけで見ると必須キー欠落を見逃す |
| 2 | **ネストされた各オブジェクト**（`policy_clauses[]` / `evidence_sources[]` / `candidate_conflicts[]` / `gaps[]` / `verification_questions[]` / `provenance` / `meta`）の key が上記 exact-key 集合と一致しない | 「浅い所は閉じているがネストの深い所に判定を置ける」抜け道を塞ぐ。**`provenance` を任意 `dict` にしない**のが要点 |
| 3 | 出力のいずれかの階層に**禁止キー**が現れる（`acceptance_verdict` / `conflict_final` / `legal_advice` / `independence_impairment_verdict` / `aml_risk_rating` / `staffing_decision` / `recommendation` / `next_action` と、その日本語別名「受任可否」「利益相反判定」「独立性阻害判定」「推奨」等）。判定は **NFKC + casefold + `-`/空白→`_` + `_` collapse** の正規化後に**再帰**で行う | 責任境界フィールドを構造的に生成不能にする。#2 で既に落ちるが、意図を明示するため二重に持つ |
| 4 | `evidence_sources[]` の各要素が **`source_registry`（Stage 1 が `evidence_records` から決定論生成した immutable テーブル）のエントリと `source_id` / `doc_ref` / `span_ref` / `provenance` まで完全一致しない**。`quote_span` が空、または対応する `span_ref` の範囲外 | 🔴 **LLM の自己申告に閉じさせない。** 偽 `source_id` と辻褄の合う偽 `provenance` を同時に出せば通る、という抜け道を塞ぐ |
| 5 | `candidate_conflicts[]` / `gaps[]` / `verification_questions[]` の非空な `source_ids[]` の要素が `source_registry` に**実在しない**、`candidate_conflicts[].source_ids` が空、または各 finding / `policy_clauses[]` の `clause_id` が当該 `policy_edition_ref` の `policy_clauses` テーブルに**実在しない** | claim→source と claim→clause の**両方**を信頼側テーブルで照合する。証拠不在の gap / question は登録 clause を根拠として空配列を許す。**`policy_clauses[]` は案件 evidence の `source_id` を持たない**（方針条項の根拠と案件証跡は別レジストリ。混同すると通常の方針データで invoke が失敗する） |
| 6 | `evidence_sources[].provenance["case_ref"] != state["case_ref"]`、または `provenance["policy_edition_ref"] != state["policy_edition_ref"]` | **現在案件・現在版スコープ外**の source を証拠にできない |
| 7 | `severity != "candidate"`、または `confidence` が float 0.0–1.0 でない | **確定粒度を作れない**（`confirmed` 等を生成不能にする） |
| 8 | 固定 list フィールドが list でない、`meta.degraded` が bool でない、`meta.degradation_code` が enum 外 | 型崩れによる後段の誤読を防ぐ |

> **`cited_source_ids` は LLM の自己申告を使わない。** 検証を通過した claim の `source_ids` から
> post_process が**再計算**する（evidence-gate 部品への入力もこの再計算値）。

**ingest / feedback モードの envelope も exact-key で閉じる:**
```yaml
# ingest
{ mode: "ingest",   gate_kind: ..., meta: {...}, ingest_summary: { written_ids, rejected } }
# feedback
{ mode: "feedback", gate_kind: ..., meta: {...}, feedback_summary: { applied_count, audit_event_id } }
```

**accept / reject の対テスト（`docs/03_test_spec.md` に落とす）:**
- **reject**: `acceptance_verdict` を足した出力 / 全角「受任可否」キー / `candidate_conflicts[0].recommendation`
  を足す（ネストの追加キー）/ `provenance` に任意キーを足す / `source_registry` に無い `source_id` を
  辻褄の合う偽 `provenance` 付きで出す / 当該版に無い `clause_id` / `severity: "confirmed"` /
  過去案件の `case_ref` / 別版の `policy_edition_ref` / `quote_span` 空 / `mode` 欠落 / `gate_kind` 欠落
- **accept**: `INVOKE_TOP_LEVEL_KEYS`（8 キー）のみ、全ネストが exact-key、非空な全 `source_ids` が `source_registry` に実在、
  全 `clause_id` が当該版に実在、`candidate_conflicts[].source_ids` が非空、`severity` が `candidate` の出力

> **denylist を持たない理由（重要）:** 「受任可能である」という断定は
> 「特段の問題は見当たらないので受任いただけます」等の言い換えで正規表現をすり抜ける。
> 禁止語リストを置くと「網羅している」という誤った安心（false assurance）が生まれるため置かない。
>
> **残余リスクを正直に書く:** 上記 8 条件は「**判定フィールドを構造的に生成不能にする**」ものであり、
> `description` / `question` の自由文に断定的な言い回しが混じる可能性は**残る**。
> これを潰すために自由文を有限コードとテンプレート描画に置き換える設計も検討したが、
> **採らない**（CTDClinicalSummaryAgent で「100% 機械保証を追った過剰設計」として巻き戻された判例。
> 顧客が求めるのは「どこかに嘘があるかもしれない 100%」ではなく「機械が保証した範囲が明示された成果物」）。
> 自由文の残余は ①判定フィールドが**存在しない**こと ②`severity` が `candidate` 固定であること
> ③全 claim に検証済み source が紐づくこと、で「読み手が断定として受け取らない」構造を作った上で、
> 業務プロセス側の人間レビューが担う。
>
> この位置づけは「**製品安全の緩和策**」であって、法令遵守・職業倫理の充足を確立するものではない
> （基準の検証は顧客の法務・品質管理部門の責任）。

## API 契約（`src/api/server.py`・blocker (a)/(d) の実装面）

| endpoint | 認証 | 許可 mode | request（本文以外は allowlist 射影される） | response |
|---|---|---|---|---|
| `POST /invoke` | **middleware 必須**。case/evidence registry と claim snapshot を保存するため実副作用は write | `invoke` | `{ customer_id, policy_edition_ref, case_narrative, evidence_declarations[], kyc_documents[] }` | `{ status, output: <invoke envelope>, correlation_id }` |
| `POST /ingest` | **middleware 必須**（token 単独では不可＝任意顧客への書込を防ぐ） | `ingest` | `{ customer_id, policy_edition_ref, records[] }` | `{ status, output: <ingest envelope> }` |
| `POST /feedback` | **middleware 必須** | `feedback` | `{ customer_id, case_ref, claim_id, perspective_code, clause_id, policy_edition_ref, source_ids[], decision_outcome(enum), decision_rationale(≤`max_rationale_chars`), action(enum), approved }`。**`additionalProperties: false`（未宣言キーは 400）**・`decided_by` / `decided_at` はクライアント指定不可（400）・書込は全部成功か全部失敗 | `{ status, output: <feedback envelope> }` |
| `DELETE /cases/{case_ref}` | **middleware 必須** | — | — | `{ status, deleted: { db_rows, kb_docs, payloads } }` |

**エラーコード（fail-closed の可視化）:**

| 状況 | HTTP | `status` | 監査イベント |
|---|---|---|---|
| 認証なし / trust 不足 | 401 / 403 | — | `auth_denied` |
| `caller_id` に認可顧客のマッピングが無い | 403 | `error` | `authorization_unresolved` |
| 入力の `customer_id` が認可顧客と不一致 | 403 | `error` | `customer_mismatch` |
| 未知 `mode` / パケット形状不正 | 400 | `error` | `packet_rejected` |
| `policy_edition_ref` が存在しない / 別 `profession_profile` の版 | 400 | `error` | `policy_edition_invalid` |
| `payload_ref` の TTL 切れ / スコープ不一致 | 400 | `error` | `payload_unresolvable` |
| KB 空振り | 200 | `success` | `retrieval_empty`（`meta.degraded: true`） |
| 注入済み LLM の auth/config/timeout/provider 障害 | 400 | `error` | `llm_call_failed` |
| LLM 応答が JSON/宣言 schema を満たさない | 400 | `error` | `llm_output_parse_failed` / `workflow_failed` |
| backend 接続失敗 | 500 | `error` | `backend_unavailable` |

> **エラーコードは allowlist 化する:** outer graph が `error_log` を
> `policy_edition_invalid` / `payload_unresolvable` / `packet_rejected` / `llm_call_failed` /
> `llm_output_parse_failed` / `workflow_failed` の**閉じた集合**に写像し
> （`src/graph/graph.py`）、server はその `error_code` のみを 400 の detail に載せる。
> 未知のエラーは一律 `packet_rejected` に落とす（内部の例外文字列・stack trace を露出させない）。

> **`DELETE /cases/{case_ref}` は cascade する:** `engagement_cases` / `evidence_records` /
> `review_perspectives`（DB 原記録）→ 対応する KB projection → 未失効の payload、の順で削除し、
> 削除完了を `emit_trace_event("case_deleted", ...)` で監査記録する。
> **削除後に同じ `case_ref` で検索しても KB projection がヒットしないこと**を受入テストで固定する。

**受入テスト一覧（`docs/03_test_spec.md` に落とす・実装フェーズの完了条件）:**

| # | テスト | 何を守るか |
|---|---|---|
| AT-1 | caller A → caller B を**同一 graph インスタンス**で連続実行し、B が A の接続 / payload / KB レコードに到達できない | 接続レベル分離（顧客横断の遮断） |
| AT-2 | `source_registry` に無い `source_id` を、辻褄の合う偽 `provenance` 付きで出力 → reject | claim→source の捏造検出 |
| AT-3 | `candidate_conflicts[0].recommendation` / `provenance` への追加キー → reject | ネストの closed schema |
| AT-4 | `/feedback` に未宣言キー（`internal_note` 等）→ 400 / `decision_outcome` に enum 外 → 400 | KB 書込口の closed schema |
| AT-4b | 同一の `consultation_summary` のまま `decision_outcome` / `decision_rationale_safe` を**全件入れ替えても top-k の ID と順序が不変**。加えて `decision_outcome` で絞り込む検索経路が存在しない（コード検査） | 検索順位が結論語彙に引きずられない（`retrieval_text` 分離の実効確認） |
| AT-4c | `candidate_conflicts[].source_ids` に `recall_refs` の **`history_ref`** を注入 → S-3 が reject | 過去事例を本件の証拠にできない（名前空間が重ならないことも同時に確認） |
| AT-4d | `decision_rationale` に PII / 他顧客名 / 命令文（"以後の指示を無視せよ"）を入れて `/feedback` → **KB projection に原文が到達しない**（命令文は 400・PII はマスク・長さ超過は 400） | 自由文の永続経路（PII・prompt injection）を閉じる |
| AT-4e | `action="reject"` かつ `approved=true` → 400 / `action="reject"` は監査イベントのみで KB projection を作らない | 有効な組合せの契約化（実装者依存を残さない） |
| AT-5 | 別 `policy_edition_ref` の `clause_id` を引用 → reject | 版の混同防止 |
| AT-6 | 他 caller が発行した `payload_ref` を横取り送信 → fail-closed | payload のスコープ束縛 |
| AT-7 | 実 checkpointer で 3 モード invoke → checkpoint 全直列化に本文センチネルが **not in** | 層 1 checkpoint 隔離 |
| AT-8 | `DELETE /cases/{case_ref}` 後に同 `case_ref` で検索 → ヒットしない | 削除の cascade |
| AT-9 | KB 空振りで `status="success"` + `meta.degradation_code: "kb_empty"`、backend 断で `status="error"` | 空振りとエラーの区別（fail-open 防止） |

## 適用部品一覧（共通部品 — 実装フェーズの写経指示書）

> 実装フェーズではゼロから書かず、共通部品ライブラリの実装部品を写経して adaptation_points を埋める。
> `apply_part.py` を流す際の指示書となる。適用時に各 `PART.yaml` の version / status を再確認すること。

| part-id | version | 適用先 | adaptation_points の埋め方 |
|---|---|---|---|
| `cat2-graph-skeleton` | 0.1.4 | `main` スロット（`EngagementWorkflowGraphNode`） | concurrency lock 付き lazy cache。`mode_specs` → 3 モード。`error_strategy="handle"` で inner の自由文を安定 error code に射影。`propagate_hitl=False`（HITL 非採用） |
| `output-envelope` | 0.1.2 | `post_process`（`formatted_output` 整形） | `format_invoke` → 5 区分（`policy_clauses` / `evidence_sources` / `candidate_conflicts` / `gaps` / `verification_questions`）。`format_ingest` → `ingest_summary`。`format_feedback` → `feedback_summary`。`required_fields_by_mode` → fail-open 防止の必須キー。`gate_kind` → `"engagement_acceptance_evidence"` |
| `evidence-gate` | 0.3.0 | inner Stage 3/4 + post_process の S-3 | `FindingField` / `project_typed_finding` で候補を型付き射影し、宣言外キー・型違いを fail-closed。claim→source は `validate_provenance()` で実レコードへ照合 |
| `payload-store` | 0.4.0 | `src/api/server.py`（層 1 checkpoint 隔離） | `scope_keys=("customer_ref",)` と `session_id=ctx.thread_id` に束縛。英字 ref を使い、HTTP 処理の `finally` で raw 値を削除 |
| `llm-draft-stage` | 0.3.0 | inner Stage 3（`applicability_draft`） | 2 ブロック prompt に検証定数から生成した closed JSON contract を追加。LLM call failure は fallback せず `llm_call_failed`。例外文は State/trace/output に入れない |
| `ingest-row-pipeline` | 0.4.0 | inner `accumulate_ingest` | request-scoped な `trusted_scope` / `session_id` を constructor 注入。`write_batch` を 1 回呼び、DB+KB 失敗時は補償 rollback して 0 件適用 |
| `entry-adapter` | 0.4.0 | `src/api/server.py`（認証 + scope + operation policy） | `SCOPE_KEYS=("customer_ref",)`。`invoke` も case/snapshot を保存するため `WRITE_INGEST` に分類し、全 3 mode と削除を middleware-only にする |

**不採用の部品と理由:**

| part-id | 不採用理由 |
|---|---|
| `scoped-retrieval` | `scope_keys` は「1 顧客 KB 内の混ぜてはいけない区画」用であり、**マルチテナント分離用ではない**。本テンプレは顧客専用 KB（物理分離）のため導入しない。ただし**安全契約のみ写経踏襲**する（safe projection allowlist・`normalize_query` の共有・`source_id` 必須・空振り時 `degradation_code: "kb_empty"`） |
| `ingest-file-sanitizer` | 入力形式を csv 等の軽量テキストに絞るため、マジックバイト判定によるファイル種別分岐が不要（受理形式が 1 種のみ）。ただし**未知形式 reject の考え方は pre_process の形状検証に取り込む** |

**部品同梱テストの扱い:** 写経した部品のテストは `tests/parts/{part-id}/` に入る。
**削除・弱体化しない**。写経の忠実性は `verify_part.py` で機械照合する。

> **空振りとエラーを混同しない:** KB に該当観点が無いのは**空振り**であり
> `status="success"` + `meta.degraded: true` / `meta.degradation_code: "kb_empty"` とする。backend への接続失敗は **fail-closed** で `status="error"`。

## FinancialOutsourcingDueDiligenceEvidenceBriefAgent との差異

| 観点 | SVC-C2-106（本テンプレ） | FinancialOutsourcingDueDiligenceEvidenceBriefAgent（外部委託 DD 証拠ブリーフ） |
|---|---|---|
| 規則体系 | 事務所の**受任方針**（利益相反・独立性・AML 手順） | 金融監督の**外部委託管理**要求（監督指針・委託先管理） |
| 利用者 | 専門サービス事務所の**受任受付担当者** | 金融機関の**外部委託管理担当者** |
| 対象 | これから受任するかを判断する**新規案件** | 既に委託する / している**委託先事業者** |
| 入力コーパス | 依頼者情報・案件叙述・関係者・KYC 資料・独立性チェック資料 | 委託先の DD 資料・契約・再委託構造・監査報告 |
| KB スキーマ | `review_perspectives`（受任方針条項に対する照合観点） | 委託先評価項目に対する充足観点 |
| 出力区分 | `policy_clauses` / `candidate_conflicts` / `gaps` / `verification_questions` | 委託先評価項目に対する充足・不足の論点 |
| 排除するフィールド | `acceptance_verdict` / `conflict_final` / `independence_impairment_verdict` / `aml_risk_rating` | 委託可否・監督上の適合判定 |

**共通するのは「証拠ブリーフ型の非結論契約」という設計パターンのみ**であり、
規則体系・利用者・入力・KB スキーマ・出力区分・排除フィールドが全て異なる。
両者を 1 テンプレに統合すると、どちらの顧客にも合わない config 分岐の塊になる。

## `config/agent.yaml` / `config/config.yaml` の設計値

**`config/agent.yaml`（静的マニフェスト・実ファイルと同一形式）:**
```yaml
agent:
  id: "SVC-C2-106"
  name: "Professional Services Engagement Acceptance Evidence Agent"
  version: "0.1.0"
  category: "Cat 2"
  industry: "SVC"
  base_type: "AgentBaseGraph"           # L1 直継承（Level 2 base agent は使わない）
  module: "src.graph.graph"
  class: "EngagementAcceptanceEvidenceGraph"
  required_trust_level: "VERIFIED_EXTERNAL"
  requires:
    extras: []
    secrets:
      - LLM_API_KEY                     # LLM は必須（下記注記）
      - ENGAGEMENT_DB_DSN
      - ENGAGEMENT_KB_DSN
```

> **LLM の必須性を manifest と実装で一致させる:** `LLM_API_KEY` を必須 secret として宣言する以上、
> **LLM は起動時必須**である（`_validate_config` で `llm` の存在を検証する）。
> `llm-draft-stage` の deterministic fallback は injection seam が `None` の設計時だけに限定され、
> 本 graph の起動検証後には到達しない。注入済み provider の実行時失敗は
> `status="error"` + `error_code="llm_call_failed"` で返し、正常縮退に見せない。

**`config/config.yaml`（runtime パラメータ）:**
```yaml
profession_profile: "audit_firm"   # 人間が設定する。AG は推定しない
top_k: 8                            # 過去観点想起の件数
retention_days: 365                 # 顧客が自環境で調整する前提の初期値
payload_ttl_s: 900                  # payload_store の TTL
max_rationale_chars: 1000           # model 可視の decision_rationale_safe の上限
max_retry: 3
timeout_s: 60
memory_enabled: false               # HITL 非採用のため checkpointer 不要
hitl:
  enabled: false
```

> 運用パラメータ（`top_k` / `retention_days` / `payload_ttl_s` / `max_rationale_chars`）は
> **テンプレ利用者が自環境で設定する前提の初期値**である。

## ユーザビリティ設計レビュー（設計判断として修正した点）

エンドユーザー（受任受付担当者）のユースケースを想定して、以下を設計に反映した。

| 観点 | 当初案 | 修正後 | 理由 |
|---|---|---|---|
| 入力の最小性 | 証跡を全て構造化 JSON で要求 | **案件叙述は自由記述のまま受ける**（構造化を要求するのは `policy_edition_ref` と証跡宣言のみ） | 受付担当が受け取るのは自由記述の案件説明であり、事前構造化を要求すると導入前に人手が増える |
| 出力の実用性 | 候補的衝突のリストのみ | **`verification_questions` を独立フィールド**として返す | 担当者が次に取るアクション（依頼者への追加確認）が出力からそのまま決まる |
| 出力の粒度 | 衝突を重大度でランク付け | **`severity` は `candidate` 固定**にし、代わりに `confidence` を数値で出す | 重大度のランク付けは実質的な利益相反判定に近づく。確信度は「AG がどれだけ根拠を持てたか」の指標であり判定ではない |
| 縮退時の挙動 | KB 空振り時にエラー | **Stage 1 の `gaps` だけでドラフトを返し `meta.degraded` / `meta.degradation_code` を添える** | 導入初期は KB が空。過去観点が無くても「必須証跡の不足」は返せるので、初日から使える |
| エラー時の返却 | 例外をそのまま返す | 顧客解決失敗は fail-closed で `status="error"` + 監査イベント。KB 空振りは `success` + `meta.degradation_code: "kb_empty"` | 「セキュリティ的に止めるべき失敗」と「データが無いだけ」を利用者が区別できるようにする |

## Design-Entry Blocker（decision.md §12）の解決状況

| # | blocker | 解決 |
|---|---|---|
| (a) | DB / KB schema と原記録 / projection の分離 | ✅ §データ層設計（DB 4 テーブル + KB projection の含める/含めない表） |
| (b) | 受任方針の版管理・専門職プロファイル指定 IF・照合ロジック | ✅ §受任方針の版管理（`policy_edition_ref` 必須・`profession_profile` は人間設定） |
| (c) | S-3 安全契約を schema と受入テストへ | ✅ §S-3 出力安全契約（ネスト含む exact-key schema + 8 拒否条件 + accept/reject 対テスト）+ §API 契約 AT-2/3/5 |
| (d) | 顧客データの最小要件・保持/削除/監査 | ✅ §顧客データ統治 + §機微データの 2 層統治 + §API 契約（`DELETE /cases/{case_ref}` の cascade・エラーコード表・AT-1/6/7/8） |
| (e) | FinancialOutsourcingDueDiligenceEvidenceBriefAgent との差異明文化 | ✅ §FinancialOutsourcingDueDiligenceEvidenceBriefAgent との差異（7 観点の比較表） |

## 事前レビューの反映（push 前・2026-07-27）

設計フェーズの push 前に実ファイルの敵対的レビューを行い、High 8 件 / Medium 2 件の指摘を受けた。
対応は以下のとおり（棄却したものは根拠を明記する）。

| # | 指摘 | 対応 |
|---|---|---|
| H-1 | 顧客別 DB/KB 接続を `GraphNode` にキャッシュすると別顧客へ再利用され得る（フレームワークの禁止事項にも抵触） | **採用** — §inner の `BaseGraph` ABC 実装に `AuthorizedResourceResolver` 契約を新設。graph/node が持つのは顧客非依存の resolver のみ、接続は呼出ごとにローカル取得。AT-1 で固定 |
| H-2 | `payload_ref` の解決経路が Stage 3 に無く、本文を prompt に渡せない（実装不能） | **採用** — §機微データの 2 層統治に「本文が必要な箇所での解決契約」を新設（`thread_id` + `customer_ref` 両束縛・ローカル変数限定・AT-7） |
| H-3 | S-3 の closed allowlist がネストで閉じておらず、`provenance` が任意 dict・`recommendation` 等を追加できる | **採用（部分）** — 全ネストオブジェクトを exact-key 化し `provenance` も閉じた 5 キーに。**ただし「自由文を有限コード + テンプレート描画に置換」は採らない**（CTDClinicalSummaryAgent で「100% 機械保証を追った過剰設計」として巻き戻された判例。残余リスクは §S-3 末尾に正直に明記） |
| H-4 | claim→source が LLM 自己申告の `evidence_sources` に閉じ、偽 source + 偽 provenance で通る | **採用** — 照合基準を `source_registry`（Stage 1 が `evidence_records` から決定論生成・immutable）に変更。`policy_clauses[].source_id` と `clause_id` も検証対象に追加。`cited_source_ids` は検証済み claim から再計算。AT-2 |
| H-5 | `/feedback` の自由文経由で結論ラベルが KB projection に混入し得る | **採用（ただし方針は 2026-07-27 のユーザー裁定で変更）** — レビュー指摘（自由文経由の結論混入）自体は正しく、`/feedback` の受理形を closed schema にした。ただし「結論を KB に入れない」という当初方針は**ユーザー裁定で反転**し、3 点セット（相談内容・結論・判定理由）を保持する。間接継承は §過去事例の 3 点セット の 3 箇所（検索軸・証拠適格・prompt 上の役割分離）で止める。AT-4 / 4b / 4c |
| H-6 | `acceptance_policies` の単独 PK では 1 版に複数条項を持てず、版混同を DB 制約で防げない | **採用** — `policy_editions` / `policy_clauses`（複合 PK）に分割。`review_perspectives` に `policy_edition_ref` を必須列として追加。AT-5 |
| H-7 | `policy_edition_ref` が config 必須と入力必須で矛盾。LLM 必須性が manifest と fallback で不一致 | **採用** — `policy_edition_ref` は呼出ごとの入力に統一し `_validate_config` から除外。LLM は起動時必須、注入済み provider の実行時失敗は `llm_call_failed` に一本化 |
| H-8 | blocker (a)〜(d) が実装可能な API / 受入テストに落ちていない | **採用** — §API 契約を新設（endpoint ごとの request/response・エラーコード表・`DELETE` の cascade・受入テスト AT-1〜9） |
| M-1 | `degradation_reason` を success で返すと書いてあるが envelope では返せない（拒否条件 #1 に抵触） | **採用** — 閉じた `meta: { degraded, degradation_code }` を envelope に追加し、`degradation_code` を有限 enum 化。`mode` / `gate_kind` の**欠落も reject** する条件に修正 |
| M-2 | 設計書の `agent.yaml` 例が実ファイルの manifest 形式と異なる | **採用** — §config の YAML を実ファイル（`agent:` 配下の `id` / `module` / `class`）と完全一致させた |

### 第 2 ラウンド（3 点セット導入後の再レビュー・2026-07-27）

ユーザー裁定で「KB に 3 点セットを保持し Stage 3 prompt にも過去結論を入れる」へ設計変更したため、
変更部分に絞って再レビューした（High 4 / Medium 1）。

| # | 指摘 | 対応 |
|---|---|---|
| R2-H1 | 検索条件から `decision_outcome` を外しても、**projection 全文をベクトル化するなら検索順位が結論語彙に引きずられる**。AT-4b は「明示的な outcome フィルタが無い」ことしか見ていない | **採用** — KB を **`retrieval_text`（`clause_id` + `consultation_summary` + `perspective_code` のみ）と表示 payload に分離**。結論・理由は embedding に含めず top-k 確定後に引く。AT-4b を「outcome/rationale を全件入れ替えても top-k の ID と順序が不変」に強化 |
| R2-H2 | `source_registry` の reject は**直接引用しか止めない**。過去の `declined` を読んだ LLM が、本件の**正しい** `source_id` を添えて結論的な自由文を書く経路は S-3 を通る。よって「構造的に止める」という記述は実態と乖離する | **採用（記述を下げた）** — §過去事例の 3 点セット を「**止まる（機構で担保）**」「**止まらない（残余リスクとして受容）**」に分割。止まるのは「過去事例の直接引用」と「本件の結論フィールドの生成」であり、「自由文の語り口に過去の判断傾向が反映されること」は止まらないと明記。**「AG は本件の結論を示唆しない」とは主張しない**。代案として提示された「過去事例を観点コードに限定し description を別パスで生成」は、ユーザー裁定（利便性優先）と CTDClinicalSummaryAgent 判例に反するため不採用 |
| R2-H3 | `decision_rationale` は新しい PII・他顧客情報・**プロンプト注入**の永続経路。しかも projection 表は「自由入力は DB 原記録のみ」と書いており**自己矛盾** | **採用** — `decision_rationale` を **3 値に分離**（`_raw` = DB 原記録のみ / `_audit` = PII マスク済 / **`_safe` = model 可視。PII スキャン + `max_rationale_chars` 切詰 + NFKC + 命令文パターン検出で 400**）。model 可視値は構造化 JSON の値として渡し地の文に連結しない。AT-4d を新設 |
| R2-M1 | 追加した 3 列が**契約化されていない**（enum が説明文だけ・`decided_by`/`decided_at` の生成主体が未定義・`action=reject, approved=true` の扱いが実装者依存） | **採用** — DB に `NOT NULL` / `CHECK IN (...)` / FK を明記。`decided_by` は middleware の actor、`decided_at` はサーバ時刻（クライアント指定は 400）。`/feedback` を `additionalProperties: false` の表で契約化し、**KB 書込は `action != "reject"` かつ `approved is true` のときのみ**・部分書込を作らないと明記。AT-4e を新設 |
| R2-H4 | AT-4/4b/4c が設計書に列挙されただけで `docs/03_test_spec.md` は空欄。かつ `recall_refs` に `source_id` が無いのに AT-4c は「recall_refs 由来の ID」と書いており**テストが書けない** | **採用** — `docs/03_test_spec.md` の Business Logic Tests に AT-1〜10 を実ケースとして記載。`recall_refs` の識別子を **`history_ref`**（`source_id` と名前空間が重ならない）と定義し AT-4c を実行可能にした |
