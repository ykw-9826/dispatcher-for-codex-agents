# CLI/API contract v1

Distribution: `dispatcher-for-codex-agents`, version `1.0.0`.
Python namespace: `dispatcher_for_codex_agents`.
Entry points: `dca`, `dca-notify`.
Execution API: `dispatcher_for_codex_agents.agent_harness`.
Product name: **DCA — Dispatcher for Codex Agents**.

## Scope

DCA connects a Codex/GPT controller to independent external model processes and
handles their batch execution, validation, recovery, provenance, events and
notifications. It does not replace native spawn/send/wait, maintain a generic
native agent tree, or orchestrate same-provider native Codex subagents.

Terminology: an **external agent / 外部 Agent** is an independent model process
launched by DCA. A **native subagent / 原生子线程** is created by Codex's native
collaboration tools. OS child-process fields and Codex `SubagentStop` protocol
events retain their literal meanings; neither turns an external agent into a
native subagent. Domain acceptance helpers and screening vocabularies are absent.

`AgentTask`, `ModelProfile`, `InvocationAdapter` and `InvocationResult` describe
one bounded external call. Only `CodexCliAdapter` (`codex_cli`) is implemented in
the production registry. Reserved alternate adapters fail closed. There are no
dynamic adapter imports, provider fallback or automatic retries.

## Input and output

`invoke` and `batch run` select a sidecar profile. `--runtime-home`/`--executable`
and the existing `--codex-home`/`--codex-executable` aliases select the standalone
runtime. Model/provider authority comes from the actual sidecar, not agent prose.
Missing provider-reported served metadata remains `NOT_REPORTED`; a configured
model or generic event model field is not proof of the model served.

`AgentTask` input is an allowlisted, column-selected, sorted stdin payload framed
by `DCA_AGENT_TASK_V1` and `DCA_AGENT_TASK_END`. External agents do not receive the
source worktree as cwd. The parent is the only result-artifact writer.

Single `invoke` uses the caller's strict JSON schema. Batch output requires
`results[]`, each item with a required string `record_id`; the caller supplies
the remaining fields. `--record-id-column` identifies source IDs. Each profile
must return every planned ID exactly once, without missing, extra, duplicate or
cross-batch IDs. These checks preserve application-level output coverage, not
HTTP/API/notification exactly-once delivery.

An attempt contains `agent_task.snapshot.json`, the redacted profile, input/output
hash manifests, JSONL events, stderr, final output and `InvocationResult`. Results
include `agent_process_started`, `agent_subprocess_count` and
`agent_working_directory` provenance. Subprocess counts are not API request counts.
Collections include `agent_results_long.tsv`, `agent_results_by_profile.tsv` and
separate authoritative/shadow/diagnostic outputs. Roles do not approve conclusions.
All are run-local execution envelopes, not a global domain schema.

## Recovery, bridge and notification

Core release acceptance separates execution/system checks from a semantic oracle.
The former is mandatory; the latter is diagnostic by default and may be an explicit
application-level gate. DCA preserves results and provenance, not a guarantee of
content correctness. Declared schema constraints remain mandatory. See the
[acceptance policy](acceptance_policy.md); no runtime status or envelope changes.

Successful immutable attempts are skipped. Failed/incomplete attempts remain
blocked until a human supplies a new attempt ID and reason in an explicit retry
plan. Do not rewrite existing artifacts to fit a new contract.

Bridge `JobSpec` uses `agent_executable` and `agent_home` for external jobs.
Bridge summaries use `external_agent_invocations`; batch job artifacts expose
`result_acceptance=NOT_GRANTED`. Execution/format success is not business approval.
`SupervisorSpec` still binds the controller's own executable, model/provider,
working directory, allowed actions, expiry and finite budgets. Continuation is
limited to a dedicated DCA-owned controller thread; arbitrary open IDE threads
cannot be taken over. Cancellation, ownership and permission checks come first.

Notification selection uses `DCA_NOTIFY_CONFIG`, `DCA_NOTIFY_SESSION_ID` and
`DCA_NOTIFY_TURN_ID`. Payloads set `result_approval_claimed=false` regardless of
execution status. Hooks remain opt-in and require host trust. Send failures are
non-blocking; do not reset/replay event or delivery ledgers.

Exit codes: 0 success; 2 input/config invalid; 10 provider failure; 11 timeout;
12 CLI/batch failure; 13 terminal/event failure; 14 schema/coverage failure;
15 policy; 16 existing shard; 17 bridge blocked; 130 cancellation.

## Pre-release interface break

The distribution, Python namespace and main executable use the names above,
with no old-name entry points or import shims. Workspace selection uses
`DCA_WORKSPACE_CONFIG`. Controller dynamic tools are `dca_start_approved_jobs`
and `dca_read_verified_results`; the parent event is `dca_parent_terminal_event`.
These names change, not task execution, result fields or delivery semantics.

Earlier development task/import/notification names, framing, batch envelope and
artifact names are removed, without compatibility aliases. Existing installations,
plans, bridge specs, hooks and history are not migrated automatically. Finish an
old run with its pinned old release. Prepare new specs/plans for this interface;
never mix old attempts into a new collection or replay a delivery ledger.
An installed retired notification hook blocks new hook installation; remove it
only through a separately authorized operation. Internal on-disk mutex names stay
stable across releases to preserve single-writer exclusion, not as public aliases.

Workspace paths remain deployer-selected canonical paths. Use a private workspace
configuration or an explicit private TMPDIR for library calls; no implicit home
or system temporary fallback. CLI output stays under configured runs/runtime.
Read-only configuration and event checks are not universal cross-host OS isolation
proof. Existing sandbox, hard timeout, terminal/schema checks, no recursion and
single-writer constraints remain unchanged.

接口面向独立外部模型调用，不替代 Codex 原生子线程调度。批处理统一使用
`results[].record_id`，业务字段由调用者提供。旧安装和历史输出继续保留原样，
不自动迁移、不补兼容别名，也不改写旧 ledger。生产安装不随开发源码自动切换。
