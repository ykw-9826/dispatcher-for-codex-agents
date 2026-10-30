# Workspace-artifact acceptance (experimental)

`STRUCTURED_RESULT` and `WORKSPACE_ARTIFACT` answer different questions. The
existing Antigravity JSON path still requires the requested terminal schema and
structured_output; its historical failures are not reinterpreted. A coding task
instead needs the right files and independently passing tests. Final agent text
is recorded only for diagnosis, never treated as successful artifact evidence.

## Contract and limits

The experimental Python `WorkspaceArtifactTask` carries `workspace_root`,
`baseline_commit`, exact `allowed_write_paths`, `validator_commands`, a finite
deadline and `execution_mode=workspace_artifact`. It does not change AgentTask,
Codex capability_policy, structured collectors or activity/3 revalidation.
No normal CLI/adapter-registry route selects this experiment.

The first scope is a clean standalone, remote-free disposable Git repository.
No shared Git storage, symlinks, hardlinked files, test-file grants or implicit
directory-write grants are accepted. Git inspection ignores global config,
hooks, fsmonitor, textconv and external diff programs. The parent fixes the
baseline before agent launch and checks HEAD, branch, remote and local Git
configuration afterward. It hashes actual file bytes and modes, including
ignored/untracked files, rather than trusting the index or agent descriptions.

Only allowlisted existing source files may change. Tests and other tracked files
must remain identical; any new file is unexpected in this initial contract.
Deletion, executable-mode changes, links, configuration changes and whitespace
errors fail acceptance. Passing this final-state audit does not prove that no
transient action occurred, or provide exact-file OS enforcement.

Validators are preselected trusted operator argv, not shell strings supplied by
the agent. They run from the parent under Linux bubblewrap with no network,
read-only workspace/runtime/validator mounts and a minimal credential-free
environment. Parent-held validator files and interpreter bytes are hash-pinned.
Scope violations skip validators; an unchanged but intact workspace can still
be tested and remains a failed implementation. A whitespace-only diff-check
failure is recorded separately as `WORKSPACE_DIFF_CHECK_FAILED`: an otherwise
intact scope can run diagnostic validators, but even passing tests cannot turn
that artifact into acceptance. Git execution failures still fail closed. DCA
never edits agent output to make the check pass. Validator sandbox failure is not
permission to run generated code unsandboxed. This is a Linux-only experimental
boundary, not a portable sandbox framework.

Artifacts pass only when a permitted implementation changed, tests remained
unchanged, all validators passed and the Git/diff checks passed. A valid artifact
with abnormal runtime is `ARTIFACT_VALID_RUNTIME_ABNORMAL`, not production PASS.
Ordinary tool/command steps or soft permission denials do not alone decide
artifact quality. Unknown activity and unsupported web/MCP/subagent activity
cannot establish an accepted runtime capture.

## Official CLI experiment

The explicit Python admission entry pins an operator-reviewed task/config digest
and official executable hash/version. Only agy 1.2.13 is admitted for this probe;
the child-only auto-update opt-out applies to the version check and invocation.
The command requests sandbox, one fresh print prompt, stream-json and a finite
print deadline. It sets no JSON schema, agent or model override, resume flag,
global permissions, credential settings or API fallback. Authentication and
service access stay entirely owned by the official CLI.

The official headless guide distinguishes normal workspace access from
approval-dependent commands. The
probe does not bypass approvals. Local post-audit is not a claim that agy enforces
DCA's exact write allowlist at OS level. No arbitrary network, MCP, subagent,
custom-agent or session-reuse capability is certified.

Each attempt has exclusive private evidence storage with task/baseline, raw
stream, stderr, final audit, validator logs and a SHA256 manifest. There is no
DCA retry or mid-task backend switch. Physical provider requests and internal
AGY retries stay UNKNOWN; final reported usage is not supplemented with step
usage. Finite subprocess time is not a monetary hard cap.

## Current benchmark outcome

The small synthetic labels task deliberately fails its initial tests. A parent
reference implementation passes seven public cases and 1,111 hidden combinations.
The hidden oracle is outside the agent workspace; the task permits changes only
to `src/labels.py` and prohibits edits to tests or Git history.

H1 ended before init with a TLS handshake timeout, exit 1, terminal ERROR and zero
reported turns/usage. No source file changed. Git, tests and file scope remained
intact, but the coding task was not completed. This is evidence of a startup
transport failure, not model coding quality or a demonstrated approval blocker.
No H1B or T1 was run: neither the DCA-fix retry criterion nor the interaction-only
PTY criterion was met. PTY remains unimplemented, not failed or certified.

Subsequent file-only headless admission used native file tools without command
requests or permission denials and changed only the allowed source file. Its
extra blank line at EOF failed the mandatory Git whitespace check. This is not
evidence that interactive terminal approval or PTY is required; it also does not
complete the coding admission gate. Historical failure records remain unchanged.

`WORKSPACE_ARTIFACT` has offline validation and incomplete live admission.
No Antigravity backend or capability is production-admitted. Beta package
metadata is `1.1.0b1`; this work is not a production v1.1.0 release.

## Autonomous-workspace post-audit (not live-admitted)

The autonomous command/capture foundation is pinned to AGY **1.4.10**, not a
wildcard admission for future CLI versions. It uses the documented `--sandbox`,
`--project` (official project ID/name), `--output-format stream-json`, and an
explicit finite `--print-timeout`. It emits no schema, model/auth override,
resume flag or permission bypass. Before the prompt-bearing spawn,
`launch_autonomous_workspace` verifies the pinned hash, runs a bounded no-prompt
`--version` probe, rechecks the hash, and rechecks it again immediately before
attempt creation. It then performs one fresh subprocess, records and kills on
deadline, and runs the parent post-audit only after protocol success. It does
not register the production adapter. A project selector alone proves neither its
workspace folders nor its effective permission rules. The child receives an
explicit minimal allowlist and preserves the real parent's `SSH_CLIENT` and
`SSH_CONNECTION` context; DCA still does not copy authentication files, tokens
or the full environment.

The reviewed 1.4.10 pin is SHA256
`49287151de19bb36578125fbd46ee3fa47e96c0b6d29d5e2439a4e4e0880fa5d`.
The caller must pass this digest through `AutonomousRuntimeApproval`; the actual
binary is checked against that approval before the version probe and again before
attempt creation. The 1.3.5 pin and its failed smoke evidence remain historical
records and are not overwritten by this pin update.

Native sandbox enforcement is not an admission gate for a controlled AGY
development benchmark because this Codex execution host denies unprivileged
namespace creation. The launcher supports explicit `sandbox=False`; this mode
omits `--sandbox` and records `UNSANDBOXED/DEFERRED`, never `PASS`. It emits no
permission bypass flag, does not alter global configuration, and remains limited
to an explicitly reviewed disposable workspace. Validator sandbox unavailability
is recorded as `VALIDATOR_SANDBOX_UNAVAILABLE` and `DEFERRED`; validation is not
fabricated. Protected paths, Git/filesystem scope, hygiene and validator identity
still fail closed before a functional result can be accepted.

The synthetic 1.4.10 capture subset permits multiple turns and known local coding
tool steps. Final cumulative usage is recorded once; per-step usage is not added
again. Missing usage is `NOT_REPORTED`, and physical provider requests/internal
retries remain `UNKNOWN`. Terminal success cannot certify sandbox enforcement or
override the independent parent artifact audit. Unsupported events, recursion,
incomplete steps and contradictory terminal events fail closed. This is offline
protocol validation, not proof of current login, subscription, permission or live
coding acceptance. Historical structured-result/exact-file contracts are unchanged.

`AutonomousWorkspaceTask` is a separate result/recovery contract for an entire
disposable repository. It is not an exact-file write grant, a second
`capability_policy`, or permission to launch an external process. The existing
structured-result and exact-file workspace-artifact contracts remain unchanged.
There is no automatic registry promotion or live invocation entry in this module.

The parent freezes a clean, standalone, remote-free Git baseline, actual file
hashes/modes, optional exact protected paths and trusted validators. New source
and build files are allowed in this audit contract, but links, changed Git
configuration/storage, HEAD, branch or remote fail closed. Comparing with the
baseline and enumerating ignored/untracked files prevents a clean current diff
from hiding changes. Shared Git worktrees are not admitted by this first audit.

Protected-file changes are rejected by default. An explicit `restore` policy
records a recovery plan, restores the frozen bytes/mode and records
`SUPERVISOR_RESTORE_APPLIED` before independently validating the recovered tree.
Recovery never changes history or conceals the agent's original changes.
Protected paths changed in Git's index fail closed even if their working files
have been restored; DCA does not silently reset the index or leave those changes
ready for a later commit.
Each audit has exclusive output storage and a recomputable file hash manifest.

Scope, functionality, hygiene and runtime completion are separate results.
Whitespace-only problems can produce `FUNCTIONAL_PASS_CLEANUP_REQUIRED`; failing
tests, scope violations or an abnormal/unknown runtime can never become PASS.
The parent must quiesce the external process before audit. Final-state hashes do
not prove absence of transient actions; the official runtime must separately
enforce its approved sandbox/file/tool boundary.

Validators reuse the credential-free, network-free, read-only Linux sandbox.
The synthetic multi-file benchmark has independent public tests, 1,464 hidden
combinations and a parent-only build oracle. Builds run on a scratch copy inside
the validator sandbox and are checked against source bytes without making the
repository mount writable. Neither reference fixes nor hidden validators belong
in the external agent's task workspace.

This is not evidence that authentication, network, web, MCP, subagents, custom
agents or session reuse has passed live admission. Native AGY sandbox
enforcement also remains separately uncertified. The beta package identity does
not satisfy the separate autonomous coding and release-review gates.

The current minimal live admission requires a reviewed pinned AGY 1.4.10 binary,
`--version` and pre-spawn hash checks, one fresh logical invocation, no retries,
the fixed synthetic prompt, an explicit official project selector, and the
existing parent audit. Native AGY sandbox enforcement and the validator bwrap
layer may be deferred only in this controlled development mode; neither can be
promoted to enforcement evidence by a successful benchmark.
