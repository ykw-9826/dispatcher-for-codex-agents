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
No Antigravity backend or
capability is production-admitted. Package metadata remains 1.0.4 on the
unreleased development branch; this work is not a v1.1.0 release candidate.
