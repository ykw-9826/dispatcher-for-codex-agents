# Task capabilities / 单任务权限（v1.0.1 development）

This is the permission reference, not authorization to run a model. Automated
regression tests are offline; live checks require separate authorization and
run-local evidence. Existing production releases and hooks are unchanged.

## One authority contract

`AgentTask.capability_policy` contains `read_paths`, `write_paths` and `tools`.
Omission means three empty lists: the v1.0.0 stdin-only, read-only, no-tool mode.
`approved_input_files` still controls what the parent reads into the selected
column payload; it does **not** expose those files to the external process.
`ModelProfile.capabilities` remains model/adapter support metadata. Permission keys
there are rejected, so selecting a profile cannot silently grant tools or files.

调用者在现有 task JSON 内逐任务授权，不增加另一套 profile 权限。只读目录不会变成
可写目录，开启 shell 不会自动开启 web、MCP 或原生 subagent。任务本身和授权路径必须
由可信父进程或用户提供；不能把外部模型生成的授权清单直接作为批准。

| Field | Meaning |
|---|---|
| `read_paths` | Existing canonical absolute files/directories; descendants of a directory are included |
| `write_paths` | Existing canonical absolute files/directories; read/write and directory descendant creation |
| `tools` | Exact supported tool names below; no wildcard or unrestricted switch |

No implicit parent-directory grant is added for a single file. Relative paths,
glob patterns, `..`, symlink components/descendants, missing roots, filesystem-root or home-wide
grants are rejected. The selected Codex home and invocation output root cannot be
granted, nor can their ancestors or descendants. Keep source/output grants narrow,
separate from DCA artifacts, and stable during invocation. Do not place credentials
or other private material inside an approved directory. The caller coordinates
exclusive ownership of write roots; this is not a concurrent filesystem scheduler.

## Supported tool controls

| Grant | Codex control / constraint |
|---|---|
| `shell` | Enable command execution, disable unified exec |
| `unified_exec` | Enable the unified execution family (exec plus its process interaction); mutually exclusive with `shell` |
| `web_search` | `web_search=live`; command network remains disabled |
| `mcp:SERVER:TOOL` | Enable this exact tool on an already configured HTTPS remote MCP server |

Unrequested configured MCP servers are individually disabled; approved servers
receive an exact `enabled_tools` list. Existing `disabled_tools` cannot be undone.
Task grants cannot enable an effectively disabled server or add tools outside an
effective host `enabled_tools` list (including an empty list). Host arrays replace
earlier arrays; task grants only narrow the final result.
Stdio MCP, unknown tools, browser/computer control, apps/plugins, image tools,
native file-change tools and recursive agent tools are not grantable in this
version. They fail closed rather than silently enabling a tool family. Remote
MCP tools can access resources on their server: local read/write grants do not
constrain that remote service. Approve each remote tool's own data/action scope.

Codex `approval=never` is retained: the task supplies prior authorization and
cannot request a larger scope while running. Hooks, snapshots, automatic skill
MCP installation, native multi-agent and other independent tool features are
disabled. Shell subprocesses inherit no caller environment; configuration that
injects shell environment values conflicts with this policy. Provider credentials
remain with the Codex host, not the command environment.

## Host enforcement and limits

An explicit policy compiles to named `permissions.dca_task.filesystem` entries,
`default_permissions=dca_task`, and `network.enabled=false`. No
`danger-full-access`, `--add-dir` parent expansion or sandbox bypass is used.
The temporary agent cwd remains separate and read-only. `:minimal` supplies the
host's baseline runtime-readable paths; it is not a promise of zero OS runtime
file reads. Caller-approved roots are additions to that baseline.

### Internal Codex self-exec compatibility

Restricted Linux permissions can hide Codex's own standalone binary during
sandbox self-exec (upstream issue `openai/codex#29049`).
DCA adds **only the exact selected executable file** as an internal read dependency
for named-permission invocations. This is a compatibility shim, not an upstream
fix or a caller grant. No release directory, parent, auth/config file or other
runtime subtree is added. Absent/empty task policies retain the existing no-tool
restricted path without adding an internal grant.

Select an existing canonical absolute regular executable (or a PATH name resolving
directly to one). Launcher symlinks, symlink ancestors, hardlinked or nonexecutable
files fail closed; use the explicitly verified canonical binary with `--executable`
instead. DCA pins its stat identity and SHA256, uses that same path for the local
version probe and invocation, and rechecks before launch. A changed selection,
identity or byte hash yields a controlled `POLICY_VIOLATION` failure shard with no
agent start. Keep the binary and parent directories stable: these rechecks do not
claim an atomic filesystem snapshot against a privileged concurrent replacement.
Write grants overlapping the selected executable are refused even outside Codex home.

The adapter separately derives a **deny boundary**, not a permission grant, from
the canonical standalone layout `standalone/releases/<release-id>/bin/codex`.
Only that selected release directory is the runtime protection scope, wherever
the installation lives. User read and write requests for the binary, release,
siblings, descendants or ancestors containing the release are rejected. Adjacent
non-overlapping data directories remain eligible. Codex home and artifact storage
retain their independent protections.

Unknown installation layouts fail closed with
`RUNTIME_PROTECTION_BOUNDARY_UNRESOLVED` before even a version subprocess for
explicit capability tasks. An absent/empty policy keeps the existing restricted
path and does not require layout discovery. This is not support for every Codex
installation layout. `runtime_protection` provenance records the layout rule,
selected file identity, protected root and user-grant check independently of both
requested and compiled permission hashes. It never authorizes reading that root.

`AgentTask.capability_policy` remains the sole **user-task** authority: user requests
for Codex home, its release/binary descendants, credentials/config or shard storage
are still rejected. Internal support does not enter requested serialization/hash.
`internal_runtime_support_grants` is recorded separately, with
`source=CODEX_SELF_EXEC_COMPAT`, canonical file identity, SHA256, Codex version,
`injected_by_dca=true` and `access=READ_EXEC_SUPPORT`. The actual Codex filesystem
entry is `read`, not a fictitious public execute permission. The compiled policy
and its hash include this exact dependency; no tool/write/network grant follows.
Do not remove or disable the shim until the selected runtime's self-exec behavior
has been independently verified; no automatic version-based relaxation is used.

The adapter requires Codex 0.153.4 or newer for explicit grants. The local 0.153.4
`config/read` interface accepted named filesystem permissions without any model
turn. Legacy `sandbox_mode`/`sandbox_workspace_write`, an existing `dca_task`
permission table, or conflicting host grants cause a prelaunch policy failure.
Use an explicitly selected compatible task runtime home; DCA does not edit global
configuration or copy authentication automatically. Deployment/admin restrictions
may further deny access; DCA never relaxes them.

**Live host isolation is not certified by fake tests.** Earlier host checks were
blocked by bubblewrap user namespaces; remediation is external to DCA. Native tool exposure,
actual read/write denial and shell environment isolation must be checked on a
working sandbox before using permissions for untrusted live work. Event auditing
detects unauthorized calls after the fact; it cannot undo their effects and is
not a substitute for OS or runtime enforcement. A command-string recursion check
is additional detection, not a proof against arbitrary obfuscated programs.
`host_os_isolation_verified` therefore remains `false` in provenance. Do not use
a permissive sandbox workaround to make a failed host test appear to pass.

## Manual CLI use

In an otherwise valid task JSON, add this object after replacing the illustrative
paths with approved existing canonical paths. The command tool is necessary for
the model to read/write via commands; file grants alone do not enable it.

```json
"capability_policy": {
  "read_paths": ["/approved-project/input.txt"],
  "write_paths": ["/approved-project/generated"],
  "tools": ["shell"]
}
```

Validate first (no inference; a local `--version` query may occur):

```bash
sh scripts/dca-env.sh .venv/bin/dca invoke \
  --task runtime/task.json --profile task-profile \
  --attempt-id capability-check-01 --shard-root runs/capability-check --dry-run
```

Removing `--dry-run` starts a real model request and requires separate approval.
Use the existing `--runtime-home` option if the approved sidecar is in a dedicated
compatible Codex home. There are no new ambient environment grants. Batch plans
retain default restricted tasks; this change does not silently grant capabilities
to existing batch/bridge jobs or alter their collector/notification semantics.

Provenance separates these records:

- `requested` and `requested_policy_sha256`: the original task authorization.
- `compiled_command_policy` and `compiled_command_policy_sha256`: the actual
  compiled sandbox/filesystem table, canonical explicit read/write grants, actual
  invocation cwd, emitted feature flags, approval, shell environment, web/network
  and recursion controls, plus enabled/disabled MCP identities and tool lists.
- `compiled_command_policy_emitted`: whether that command was handed to a started
  agent process, not whether the kernel enforced it. Preflight can record a compiled
  preview with this flag false; a rejected compilation has a null compiled record.
- `host_os_isolation_verified=false` and `enforcement_confirmation=NOT_VERIFIED`:
  neither compilation nor a successful fake invocation certifies OS enforcement.

The compiled hash covers only this sanitized security projection, not the entire
Codex configuration or its credentials. MCP endpoint records contain scheme, host,
port and a SHA256 of the full endpoint; path, userinfo, query, headers and credential
values are not stored. A different endpoint changes the compiled hash even for the
same requested grant. Credential-only/header changes are deliberately not certified
by this hash. Dry-run argv is a redacted preview, not a command to copy and execute.
The actual invocation uses the validated URL; do not publish process diagnostics
that expose raw argv. Capability paths are intentional local audit data:
do not publish private task snapshots. Existing output hashes, terminal/schema
checks, timeouts, explicit retry and immutable attempt rules remain mandatory.

## Local configuration compilation

Capability compilation reads system configuration, the selected Codex home's base
configuration and the existing sidecar, then trusted project configurations from
the detected project root toward the invocation cwd. The nearest project setting
wins. Tables merge recursively; lists/scalars replace. Project configs outside that
root are not consumed. Before reading any project config, the compiler checks the
entire canonical root-to-cwd interval against the original base layers' explicit
trust records. The root must be explicitly trusted. An `untrusted` record anywhere
on that interval blocks all project config consumption, including when intermediate
directories have no record. Neither a deeper `trusted` record nor a later base
layer or task/tool grant can erase a deny. All-trusted applicable records retain
the existing nearest-config precedence.

Only lexical dot/separator normalization is accepted for trust paths. Symlink
aliases, relative/parent-traversal paths, ambiguous mappings and distinct spellings
mapping to the same path fail closed. Unsafe trust-path spellings are rejected even
outside the interval; safely canonical unrelated records do not affect its grants.
Unresolved roots/trust, symlink configs and project-layer attempts to redefine
trust/root discovery also fail closed. No fuzzy alias matching is used.
Capability settings inside legacy named-profile tables are rejected
as ambiguous; this does not implement standalone-profile migration or a general
Codex config resolver.

MCP safety checks run on the final merged definition. HTTP, stdio and mixed
`url`/`command` definitions cannot be granted. The compiler pins the checked HTTPS
URL, exact enabled tools and final disabled tools into the invocation's explicit
overrides. This prevents a less specific URL from being validated while the child
receives a different endpoint. Remote credentials remain in host configuration,
not in capability provenance. Keep host configuration stable during an invocation;
this change does not add filesystem/configuration TOCTOU protection.

Every capability-consumed configuration table and nested value is type checked.
Malformed input raises a controlled `CapabilityError` with field-level messages,
not raw values. Actual invocation records a `POLICY_VIOLATION` failure envelope
before any agent starts; an earlier model/profile parse failure uses the existing
`PROFILE_CONFIGURATION_INVALID` path. All eight shard files and their hashes are
retained, without overwriting an attempt. CLI dry-run reports input failure without
creating an invocation, while actual invoke delegates capability failures to the
same shard writer. The sidecar resolver normalizes invalid UTF-8, TOML syntax and
its model/provider/table structure errors to `ProfileResolutionError`, using only
the config role, basename and error category. It does not catch cancellation or
process-exit exceptions. Base and existing sidecar files use the same boundary;
this does not change sidecar discovery or migrate standalone-profile formats.
Profile failures use `PROFILE_CONFIGURATION_INVALID`: dry-run returns exit 2
without a reservation; actual invoke returns exit 12 with the complete immutable
failure shard. A second attempt with the same ID is refused without rewriting it.
No failure handling grants fallback permissions or retries.

## Separately authorized real smoke

Before any live request, an operator must approve a profile, confirm a functioning
native sandbox, review the exact tool exposure and grant only disposable fixtures.
Suggested budget: three independent invocations, one each for default denial,
single-file read plus adjacent-file denial, and bounded write plus outside-write
denial. Use a short verifiable JSON schema, timeout 120 seconds and call_limit 1;
no retry, fallback, notifications or recursion. Check tool events, filesystem
sentinels, shell environment and resulting provenance/hashes. Stop on the first
unexpected access; do not promote a post-hoc policy failure to an isolation PASS.

## Compatibility

Existing task JSON remains valid without the new optional field. InvocationResult,
adapter registry, collector, bridge and notification interfaces are unchanged.
Explicit grants require compatible host configuration; unsupported/conflicting
grants produce `POLICY_VIOLATION` (CLI exit 15) before inference and retain a failure
shard. Invalid JSON/contract input is rejected at CLI validation (exit 2) before an
invocation exists. Finish old frozen plans with their pinned release; no migration
of historical artifacts, releases, hooks or ledgers occurs.
