# DCA operations and recovery / 操作与恢复

DCA runs independent external model processes for a Codex/GPT controller. It does
not replace native same-provider child orchestration. The versioned
[native capability matrix](native_capabilities.md) explains the current boundary.

Technical baseline: development `dispatcher-for-codex-agents==1.0.3`, namespace
`dispatcher_for_codex_agents`, [contract v1](contract_v1.md). This tool is independent
of any scientific project. This guide describes capability, not authorization
to run a real task or alter a production installation.

## Installation and paths / 安装与目录

Start in a new checkout. `python3 scripts/init-workspace.py --dry-run` previews
initialization; omitting `--dry-run` makes the checkout/private directories 0700,
writes workspace and disabled notification configs 0600, and refuses existing
configurations. It never downloads software or touches host hooks. Use the
commands in the bilingual README to sync the lockfile and run help/offline tests.

新 checkout 内可先 dry-run，再初始化。只处理该 checkout 的权限/配置，已有配置不
覆盖，不下载软件、不碰宿主 hooks；安装及测试命令见 README。

| Path | Ownership |
|---|---|
| src, tests, examples, scripts, docs | Reviewed source and documentation |
| configs/workspace.json | Private, non-secret `{version: 1, workspace_root: canonical absolute root}` |
| configs/notifications.json | Private non-secret configuration; initializer uses no sinks |
| .venv | This project's independent development environment |
| runtime/state, runtime/logs | Private event/delivery records and local logs |
| runtime/cache, runtime/tmp | Project-local caches and temporary work |
| runs | Explicit invocation/batch output roots |
| releases | Immutable source+lock builds, never moved old virtual environments |
| reports, exports | Private/local evidence and reviewed whitelist deliverables |

No fixed installation path is required. `scripts/dca-env.sh` derives the checkout
from the script location, quotes paths including spaces, and sets only the child
command environment. Defaults are project-local. Optional settings:

| Setting | Purpose |
|---|---|
| DCA_WORKSPACE_CONFIG | Absolute canonical configs/workspace.json; auto-discovered in .venv and release layouts |
| DCA_UV_ROOT | Optional approved shared toolchain root for bin/python/tools |
| UV_CACHE_DIR | Optional approved cache, overriding the project-local uv cache |
| UV_PYTHON_INSTALL_DIR | Explicit location for managed Python; no home bin side effects |
| UV_PROJECT_ENVIRONMENT | Default checkout/.venv; release builder explicitly selects its own venv |
| DCA_UV_EXECUTABLE | Optional uv executable for the release-building script; otherwise uv on PATH |

Use the pinned interpreter and lock; no implicit upgrade or Python download.
Explicit Python installations use `uv python install --no-bin` and an approved
directory. A populated cache supports `uv sync --frozen --offline`; a first online
sync downloads dependencies, not model responses. Do not install into a different
project's environment or add source imports through PYTHONPATH.

固定解释器/lock，不隐式下载或升级 Python。显式安装使用 --no-bin 和批准目录。
共享 uv/cache 是可选配置，不是强制布局；项目间不共享可写环境、不跨 source import。

CLI activation requires a valid workspace: current owner, config 0600, initialized
root/runtime paths 0700, canonical paths without symlinks. Outputs must be explicit
under runs/runtime. Missing config fails closed, even from a differently named
environment. Library callers retain explicit output paths but must supply private
TMPDIR when no workspace is selected; no implicit system temporary fallback.
Codex's own sessions/auth/trust/cache are host-managed external dependencies;
DCA does not relocate or certify all host storage.

## External agent and batch / 外部 Agent 与批处理

Use the included `examples/test_demo.py` for a complete executable example; it
calls the existing CLI, not a second orchestration implementation. The demo's
schema and records are invented. Real tasks require separately approved input
allowlists, selected columns, profiles, instructions and output schema.

- `invoke --task --profile --attempt-id --shard-root [--dry-run]` creates one
  immutable invocation. Prompt and selected data travel via stdin.
- `batch plan` freezes source, IDs, columns, profile roles, shard size and schema.
- `batch run` defaults to one worker, allows explicit two, never higher. Each
  profile is serial and uses identical membership. `--dry-run` starts no external agent.
- `batch status` reconstructs immutable artifacts; `batch collect` checks schema,
  hashes and exact-once IDs, separating authoritative/shadow/diagnostic tables.
- `batch retry-plan` requires a new attempt and human reason for failed/incomplete
  work. Success is skipped; failure does not trigger fallback or automatic retry.
- `batch monitor` is deterministic local monitoring, not a model-prompt loop.

The result schema comes from the caller. DCA contains no domain-specific subset
selector, screening taxonomy or domain acceptance helper. Generic schema, hash
and exact-once coverage checks remain; they do not approve the meaning of a result.
See contract v1 for exit codes and current public names.

全部真实任务须另有明确授权。父进程仍是唯一结果 artifact writer；默认 external agent
只处理 stdin，不使用工具。单任务可以通过 [capability_policy](capability_policy.md)
明确授权文件读写范围和工具。hard timeout、TERM/KILL、无 recursion/fallback/自动 retry
的边界不变。领域筛选辅助模块已
移除；业务字段由调用者提供。格式、覆盖和角色隔离检查不代表模型结论已获批准。

## Public interface and test demos / 公共接口与测试 Demo

The Python execution API is `dispatcher_for_codex_agents.agent_harness.AgentTask`.
Batch input selects `--record-id-column`; output is `results[].record_id` plus
caller-defined fields. Notifications use `dca-notify` and `DCA_NOTIFY_*`.
The current contract has no retired project-specific aliases. Old plans, specs,
attempts and production installations must remain paired with their old release;
this cleanup does not migrate or rewrite them.

`examples/cross_provider_demo.py --output-root runs/cross-provider-test` exercises
one batch through the existing bridge, independent external configurations,
collector and controller handoff. The controller and both external agents use
bundled test executables. No API key, real host, inference request or notification
is needed. `--dry-run` checks the plan without starting agents; there is no live
flag or implicit promotion from a test configuration. Generated test authorization
expires after five minutes. Model names illustrate configuration only.

测试 Demo 使用虚构目录记录。跨 provider 示例把 GLM、DeepSeek 和主控制器全部模拟，
通过真实 DCA batch/collector/bridge 路径完成虚拟调用与事件交接；不证明新版本 live
连通性，不发送通知。真实外部模型任务必须另行审批输入、现有 provider 配置和预算。
旧结果不迁移、不覆盖；恢复旧任务仍使用原固定 release。

## Evidence limits / 证据边界

Execution/system acceptance is the core release hard gate. A semantic oracle is
diagnostic by default: a wrong but schema-valid answer preserved faithfully is
not a DCA transport failure. Applications can separately require semantic success;
DCA does not guarantee external agent content correctness. See the
[acceptance policy](acceptance_policy.md) for warning and historical-report rules.

执行、完整性和 provenance 是 DCA 的验收责任；内容正确性默认独立诊断，业务层可
显式设为门禁。不得改正外部答案或覆盖旧报告来取得 PASS；输出 schema 仍严格验证。

Configured model/provider are not proof of the served model. Missing provider
metadata remains `NOT_REPORTED`. Exact-once output coverage is not a guarantee of
HTTP/API or notification exactly-once delivery. Read-only settings, no-tool flags
and event inspection do not certify universal cross-host OS isolation. Notification
errors do not change task state. No polling by the main model does not remove the
cost of inference when either a controller or an external model runs.

## Bridge authorization / 续接授权

`examples/bridge.example.json` deliberately has invalid placeholders and expiry
0.0. It is not runnable authorization. For a real task, create a new private spec
with canonical executable/config/cwd paths, the host's existing model/provider,
the three explicit allowed actions, fixed jobs, finite turn/wake budget, timeout,
and expiry within 72 hours. Never silently refresh an old authorization.

`bridge start` requires `--spec`, a new `--state-root` and `--new-thread`.
`--dry-run` checks local configuration and executable schemas only. A real start
can incur inference costs, even with deterministic virtual external jobs. The single
supervisor blocks on OS completion/events; no separately launched monitor is
needed. An explicitly created DCA controller thread receives the verified tool
completion via stdio App Server. No arbitrary open IDE window attachment is
supported. The host's experimental dynamic-tool interface must pass local schema
gates; see [official App Server](https://learn.chatgpt.com/docs/app-server).

示例默认过期且带占位值。真实任务先人工批准路径、现有模型配置、动作、固定 jobs、
期限和有限预算。真实 bridge 即使子任务为虚拟调用，也可能产生主模型费用。续接只限
DCA 专用 controller thread，不接管 IDE 原窗口；本地 schema 门禁优先。

`bridge status` is read-only. `bridge cancel` revokes authorization.
`bridge recover` conservatively checks persisted state; it does not rerun external jobs
or resolve ambiguous sends by guessing. One controller writer per thread;
unknown external ownership blocks takeover. Cancellation, permissions and expiry
precede any wake. Events never grant approval for model switches, retry, commit,
scientific decisions or a next stage. Main Stop events cannot wake themselves.

Trusted parent events carry manifest hashes, not executable external agent instructions.
Early/duplicate/out-of-order events are locally recorded and deduplicated. Reserved
send/ACK loss can remain AMBIGUOUS; do not claim cross-process exactly-once.
Only aggregate completion, blocking failure, required action or confirmed anomaly
wakes the controller. Ordinary successful shards do not each wake or notify.

No model requests are made by the waiting/watchdog layer. Watchdog checks follow
the existing 3/5/15/60-minute policy and do not alter invocation hard timeouts.
Beyond 720 minutes, diagnostics use process identity, file progress, CPU/I/O and
two stagnant checks, not duration alone. This is tested with fake clocks.
SIGHUP/SIGINT/SIGTERM cancel and clean up the finite supervisor. SIGKILL/power
failure only preserve already-fsynced records; SSH loss and power recovery have
different guarantees. Manual ambiguity review can be required.

## Notifications and safe integration / 通知与集成

Default sinks are empty. ServerChan (Turbo/SC3), DingTalk and HTTPS webhook are optional; the example
notification config is an inactive template with placeholder paths. Keep any
actual key-bearing config/secret outside the source repository and distributable,
current-owner 0600. Do not commit real webhook targets. No sink is enabled or
host hook installed by the demo or workspace initializer.

`dca-notify` supports hook/emit/bind and explicit hook installation/rollback.
Hooks include UserPromptSubmit, Stop, SessionEnd, Interrupt, PermissionRequest
and optional SubagentStop for native Codex subagents, subject to actual host support. Review/trust exact
definitions in the host; do not bypass trust or combine duplicate top-level notify
and Stop pushes. See [official hook trust](https://learn.chatgpt.com/docs/hooks).
An installation operation requires separate authorization to modify host files.

默认不启用 sink，demo/初始化不安装 hooks 或发送通知。密钥/真实通知目标不进入源码
和包；宿主 hook 安装另行批准、精确定义人工信任，不绕过 trust 或重复启用顶层 notify。

Messages use Chinese metadata templates: turn ended, external task complete, failure,
action required. No assistant body, prompt, abstract, full text, key or long ID.
The complete event stays local. Notification errors are non-blocking and do not
change task/scientific state. Same-run harness terminal delivery suppresses an
immediate main Stop duplicate; failure/action is not swallowed by success.
Never reset/copy back/replay an older delivery ledger to “fix” a notification.

### Notification configuration in 1.0.3 / 三通道与权限通知

`configs/dca_notifications.example.json` contains three disabled targets. Keep
the existing ledger path and Turbo `sink_id`; use a distinct stable ID for each
additional destination. Maximum configured targets: three (including disabled
entries). Do not reuse an ID for a different destination or replay an old event.
Choose direct DingTalk **or** Turbo forwarding to that group, not both.

- `kind=serverchan`: `send_key_env_file` points to an external 0600 file containing
  one `SERVERCHAN_SENDKEY=...` assignment. `SCT...` selects Turbo;
  `sctp<decimal-uid>t<token>` selects SC3. The endpoint is derived, never supplied
  by a caller URL. Both require HTTP 2xx and integer `code=0`.
- `kind=dingtalk`: `webhook_env_file` contains `DINGTALK_WEBHOOK_URL=...` for the
  official robot endpoint. `signing=HMAC_SHA256` requires a separate
  `signing_secret_env_file` containing `DINGTALK_SIGNING_SECRET=...`. Both files
  must be outside repositories, canonical, current-owner 0600. `signing=NONE`
  is explicit and omits the signing file; the operator must configure the robot's
  supported keyword/IP security policy. The fixed body contains `DCA` if that is
  the selected keyword. No outgoing bot, enterprise application or inbound server.
- Existing `kind=webhook` retains its `{title, body}` wire contract and explicit
  success-field/value configuration. It is not a DingTalk alias.

Permission policy is `permission_notification_policy`: `OFF` (default, including
old configs) or `REQUEST_OBSERVED`. OFF records a private observation only, without
loading sink credentials or sending. REQUEST_OBSERVED uses `[DCA] 权限请求` and
neutral text; it never claims a person is waiting, and never returns allow/deny.
Only an allowlisted tool category is retained, not the command, description,
transcript or tool arguments. A locally generated observation ID distinguishes
requests; the host has not promised a stable request ID, so duplicate hook
invocations are not claimed to be request-level exactly-once.

Stop's title is `[DCA] Codex 本轮结束`: a main turn ended, not a claim of tests,
scientific success or release completion. UserPromptSubmit remains local-only.
No previous ledger event is relabeled. See [Codex hooks](https://learn.chatgpt.com/docs/hooks)
and [approval security](https://learn.chatgpt.com/docs/agent-approvals-security): a raw
PermissionRequest can precede automatic or other-hook approval.

本版本默认关闭权限请求的外部通知，避免 Auto-review 时误报“需要人工操作”。
选择 REQUEST_OBSERVED 也只表示观察到请求，不能证明最终审批 UI 或人工等待。
本地 diagnostics 不保存命令/描述正文；不解析 transcript，不接管审批决策。
三条发送路径为 SC3 App、钉钉直连、Turbo 微信；手机接收须逐设备另行人工验收。

Fan-out stays sequential. Per sink: 0.5s HTTPS socket timeout, 0.9s child deadline;
newly generated/migrated hook definitions use 4s (three deadlines plus 1.3s local
overhead margin). Offline slow-fixture tests include configuration and ledger
work. This is a tested local budget, not an OS scheduling, large-ledger or delivery
SLA; historical production transport problems are not declared solved. No retry,
service, queue, or persistent worker is added. Existing hooks do not auto-update.

Each sink records independent `elapsed_seconds`, `attempted`, controlled failure
code and available HTTP/business code. `SENT` means **service accepted**, not
phone delivery. `NOT_ATTEMPTED/CONFIGURATION_ERROR` means local rejection;
`DELIVERY_UNKNOWN` means an attempted/possibly dispatched send lacks confirmed
acceptance. A reservation before dispatch is NOT_ATTEMPTED. Retrying the same
event does not automatically retry any of these outcomes. No raw provider error
body, signed URL, key or token is stored. Root schema/identity and ledger-integrity
errors stop the whole operation; sink-local configuration/transport failures do
not prevent other valid targets. Preserve the existing per-event/per-sink dedupe
and ledger flock/append/fsync behavior.

Delivery identities are checked across the entire ledger before processing begins:
outer SHA256, nested ID when present, and the canonical hash of the nested identity
must agree. Corruption stops all sends without modifying history. Context, event
observations and permission observations without delivery keys remain non-dedupe
records. Protocol labels are parent-owned once a sink is constructed, including
connection failures, deadlines and abnormal child results; the initial pre-construction
identity reservation has no protocol label. No credentials are added to these records.

### Explicit hook migration / 显式迁移

Do not put three-target config behind old handlers. Separately approve a new
installed release and migrate UserPromptSubmit, Stop and PermissionRequest
together before enabling the new config. This task does not activate production.

`dca-notify hooks-migrate --codex-home <home> --config <existing-config>
--executable <new-release/venv/bin/dca-notify> --from-executable <old-executable>`
previews exact changes; repeat `--from-executable` for a mixed old/current setup.
Apply only after review with `--apply --expected-sha256 <preview.previous_sha256>`.
The old release launcher/metadata and the new 1.0.3 release identity are checked
without executing them. Validation checks release paths, the release's own Python
shebang, distribution/entry-point metadata and a console-wrapper AST allowlist.
Supported uv suffix/slice and pip `re.sub` argv normalization may precede
`sys.exit(main())` or `raise SystemExit(main())`, optionally under the main guard.
Comments/strings cannot stand in for imports/calls; extra logic, imports or shadowed
`main` are rejected before backup/apply. Only the three recognized, unfiltered, single-handler
DCA groups are replaced; unrelated hooks survive. Unknown or duplicate DCA
definitions fail closed. A hooks backup and hash-guarded rollback receipt are
written; config, secret, delivery ledger, approval reviewer and trust are untouched.
Review/trust the changed definitions manually in Codex. PermissionRequest can
remain installed with OFF. Ordinary hooks-install still refuses legacy hooks;
migration is never implicit. Existing legacy mutex names remain internal only.

Protocol references: [ServerChan SDK](https://github.com/easychen/serverchan-sdk),
[SC3 API](https://sc3.ft07.com/doc),
[DingTalk security](https://open.dingtalk.com/document/robots/customize-robot-security-settings).

## Releases, rollback and publication / 安装回退及公开

Development changes do not update a production release automatically. When a
release build is separately authorized, scripts/build-release.sh requires a clean
Git checkout and creates a new commit-addressed release from source+lock. Do not
move a venv or modify an old release in place. Verify entry paths/imports and fake
tests before any explicitly approved hook switch.

Back up configuration, record hashes, ensure writers are idle, and preserve current
event/delivery state before rollback. A source rollback does not authorize ledger
rollback. Host hook writes use backup/receipt and same-file locking; cross-filesystem
updates are not universally atomic. Unknown send state needs manual review.

For publication, package only the source whitelist; never push the original private
history or include archived evidence. The owner-approved MIT License and copyright
remain unchanged. This development task does not publish a Release or package.
See [public preparation](public_preparation.md). No daemon, queue, swarm, new
adapter or automatic scientific decision functionality is added by this preparation.
