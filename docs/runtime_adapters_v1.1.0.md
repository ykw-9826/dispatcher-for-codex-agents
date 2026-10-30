# Runtime adapter beta foundation (v1.1.0b1)

This beta is an engineering boundary, not live-runtime certification or a
production release. Package metadata is `1.1.0b1`. Production releases and
historical records are not migrated.

## Codex 0.158.0

The exact exec JSONL admission set is 0.153.4, 0.155.1 and 0.158.0.
There is no future-version wildcard. Unsupported versions are rejected before
task-process launch and during capture/revalidation acceptance, even under the
default strict contract. Activity interpretation remains dca-activity/3.
Optional web-search results are data, never a tool grant; on 0.158.0 the field
must be absent, null or an array of JSON values. Malformed types fail closed.
Profile resolution,
named permission compilation, immutable shards and derived revalidation keep
their existing contracts.

The fixed source comparison used OpenAI Codex rust-v0.158.0, commit
064c6b8c737f5b41d171fdda80bd9ef10ad06eb3:
exec/src/cli.rs is unchanged from 0.155.1; exec/src/exec_events.rs adds optional
WebSearchItem.results. ConfigProfile and named filesystem permission structures
do not require a new DCA schema. On an observed exact 0.158.0 version, command
compilation also disables daemon_auto_start, agent_message_board and
send_message_to_user_async. Existing host/tool restrictions still apply.
Restricted preflight does not start a version subprocess and cannot certify a
host version it has not observed.

Synthetic tests exercise these mappings, sidecar selection and final CLI
overrides. Binary version/help is not a strict-config parser gate, model
connectivity test or OS permission-enforcement test.

A no-model check on the fixed 0.158.0 binary accepted a synthetic root config
with DCA-generated feature/config overrides through `app-server --strict-config`
over stdio. No RPC was sent; the parent ended the idle process. A paired unknown
root key failed strict parsing. This proves root-config/override parsing only.
This binary rejects `--profile` on app-server and `--strict-config` on `mcp`;
the standalone-sidecar strict host check remains pending. `--listen off` without
remote authentication had no usable transport, so its exit was not counted as
successful idle startup. No exec prompt, shared daemon or production auth was used.

## Antigravity 1.2.12: interpretation, not production execution

The reference is Google's CLI headless documentation (init, step_update, result),
CLI changelog and the locally observed 1.2.12 help. Fixtures are invented examples
of that documented subset, not recorded authenticated sessions.
The help's unlimited timeout default conflicts with an older documentation
default; the inert command plan always supplies a finite print-timeout, with
a minimum of 1 ms to avoid rounding tiny durations to unlimited zero. It exposes
a separate finite parent process deadline; the offline helpers themselves do not enforce it.
No effort mapping is inferred.

These experimental Python helpers are not a stable production adapter API.
compile_headless produces an inert single-shot argv preview. It does not resolve
credentials, create state directories or launch a process. It uses explicit
model, print prompt, stream-json, json-schema, print-timeout and
disable-slash-commands. There is no resume/latest, session pool, account discovery,
authentication fallback or automatic model switch. Equals-bound print/model
values keep flag-like text in the data boundary. Prompts in argv are not a
confidentiality guarantee and are not persisted by this component.

evaluate_headless_capture uses the independent dca-agy-headless/1 wire subset
and the corrected dca-agy-text-only/2 admission rule. It requires one init,
matching conversation identities, ordered ACTIVE/DONE steps, exactly one
structured user_input step preceding responses, and exactly one final result.
Repeated ACTIVE/DONE updates of one index count as one step, not two inputs.
On the pinned 1.2.12 contract num_turns must still be 1: no real one-input
telemetry mismatch has been established to justify a version-specific exception.
It is not a physical request counter. Multiple user_input steps remain rejected.

init.tools is an advertised inventory of string names, not tool execution or
effective permission. It is retained in provenance as ADVERTISED_TOOL_INVENTORY;
a nonempty list alone is not a rejection. Typed tool steps, tool_info/tool_name,
subagent_info, or nonempty terminal denied_actions fail text-only admission.
Unknown or malformed evidence still fails closed; stderr prose never substitutes
for typed activity or proves enforcement.

The command must carry exactly one matching --json-schema argument before
launch. init.json_schema and result.json_schema must match the request, and a
successful terminal must contain structured_output. That field is the only
output source and must pass the requested schema/coverage checks. A response
string, including JSON or a JSON fence, cannot supply a missing structured_output.
response is checked only for consistency with the already-present structured
value, without fence recovery. Missing init schema is AGY_SCHEMA_NOT_EFFECTIVE;
missing terminal structured_output is AGY_STRUCTURED_OUTPUT_CONTRACT_VIOLATION.
SUCCESS plus exit zero alone never grants acceptance. Timeout and cancellation
override apparent success; diagnostics are fixed categories, never raw stderr.

Only final-result usage is retained. Step counters are not added again.
Absent usage is UNKNOWN, partial usage stays partial, and thinking tokens are
not added to output tokens. A subprocess or logical invocation does not establish
a physical provider request count. Built-in transient retries are documented but
not controlled or counted here; provider requests and observed internal retries
remain UNKNOWN. Offline interpretation itself starts zero logical invocations
and zero DCA retries; it does not reconstruct the source call's retry history.
The process deadline is not a billing cap.

All nonempty capability grants are unsupported. Even an empty requested policy
does not prove agy is restricted: its real workspace/tool/network behavior has
not been admitted. Results therefore record effective capability as null and
enforcement as NOT_VERIFIED. The production registry reserves antigravity_cli
and rejects it with ANTIGRAVITY_LIVE_NOT_ADMITTED. Declared aliases, Codex
profile/provider selection, and an agy executable (including resolved symlinks
and declared wrappers) are rejected before a version or task subprocess starts.
There is no default-registry enable switch. Explicit custom executable code remains caller-trusted;
these checks do not authenticate arbitrary renamed programs or sandbox Python
callers who replace the registry with their own executable implementation.

## Experimental official CLI admission

The selected route is DCA -> official agy CLI subprocess. Authentication and
normal CLI app data/cache are owned by agy. DCA does not read OAuth, keyring or
credential files, copy credentials, log in/out, call private APIs or fall back to
an API-key route. Normal CLI-owned state writes are expected; full home-state
isolation is not an admission requirement. This architecture is not a claim of
a vendor subscription exception.

The default registry and ordinary profile/executable aliases stay closed.
An explicit Python AntigravityCliAdapter entry requires a reviewed local config
digest plus a separately approved official executable path/hash/version.
ANTIGRAVITY_EXPERIMENTAL_LIVE must be the boolean true in that config; an
environment variable never enables execution. Provider must be antigravity_cli;
the profile selects the historical custom-agent probe or the default-agent probe
below. Both require one fresh session, call_limit=1, finite deadline, no resume,
no fallback and zero DCA retries. Each empty disposable workspace is single-use
and must be outside source checkouts. No writable global configuration is created.

Before approval, review official plugin metadata and known non-credential
customization locations without opening authentication material. Active global
hooks/plugins require operator trust; otherwise the zero-active baseline can be
accepted. CLI-owned state/cache changes are distinct from DCA config mutations.

The historical custom-agent text-only probe has not been admitted. Its exact
locally generated agent definition excludes default components and has empty tools, MCP, plugins
and skills, no subagent role, and sandbox requested. The documented exclusion
does not disable global post-invocation hooks, hence the separate baseline.
A matching agent/cwd, a validated generated text-only definition, no observed
typed tool/subagent/denied-action activity, one structured input, and valid
structured terminal output are required for success. excludeDefaultComponents
is explicitly true; tools, MCP, skills and plugins are empty, subagent is false
and commandExecutionPolicy is off. This configuration is distinct from the
CLI's advertised init inventory; a nonempty inventory does not prove exclusion
failed. Event validation is not proof of OS enforcement. All nonempty file/tool
grants still fail compilation; no capability is advertised as live-verified
solely from this implementation or fake tests.

The official model selection may be left unset; provenance records null rather
than guessing a model or using fallback. CLI-selected model metadata in init is
not provider-reported served-model proof. Optional documented init model/agent
and step duration fields are validated, never interpreted as permissions. The
schema echo is mandatory for this schema-requesting adapter.

At most four separately authorized logical invocations may be used for an
admission session; this adapter does not promise a provider request cap. Built-in
retries/physical requests remain UNKNOWN unless independently observed. The
finite process deadline is not a billing cap. Raw captures and exact argv are
private artifacts; only synthetic, non-secret prompts are suitable for these
initial probes. On L1 failure stop, do not automatically retry or proceed to L2.


The headless, custom-subagent and CLI 1.2.1 changelog documentation distinguish
available tools, typed invocation details, and excludeDefaultComponents.
The component-exclusion field was introduced before the pinned 1.2.12 runtime.
These rules do not infer MCP inheritance or OS enforcement from metadata.
Historical shards/verdicts are immutable; any corrected interpretation is a
separate hash-pinned companion record, never an overwrite or implicit recovery.
No further live invocation is authorized by an offline contract correction.


## Exact 1.2.13 admission candidate

The live candidate is exactly 1.2.13; 1.2.12 remains an explicit historical
offline capture/fixture version. Neither prereleases nor future versions are
auto-admitted. The strict text-only/schema/one-input contract is unchanged.

The current official CLI help retains the relevant headless flags in the
recorded 1.2.12 interface baseline. Current documentation retains the stream and
custom-agent concepts. A verified, version-specific 1.2.13 changelog/source diff
was not available; help agreement is not proof of equivalent runtime behavior.
No new permission or default-agent execution mode is admitted by this change.

Official troubleshooting documents a background self-updater and the exact
environment value AGY_CLI_DISABLE_AUTO_UPDATE=true. DCA sets it only on its
experimental admission children, including the no-prompt version probe. It does
not edit shell profiles, global environment, updater state or the official binary.
This is consistent with version drift, not proof of a particular updater actor.

Before each prompt-bearing invocation, the adapter verifies the approved binary
hash, runs a bounded --version check, rechecks the hash, and checks it again
immediately before spawn. A mismatch aborts without a model prompt; drift found
after shard reservation produces an immutable controlled failure with zero
logical invocations. Only the override's name/value is recorded, never the
inherited environment. Approved executable hashes are local review evidence,
not machine-specific public constants.

These checks detect observed replacement; they are not an atomic hash-and-exec
primitive against a different process replacing the pathname in the last interval.
The process-local updater opt-out does not disable other already-running CLI
processes. A later live attempt still requires its own version/hash recheck.
No live invocation, permission enforcement or structured-output fix is certified
by this offline version admission.


## Default-agent text-only admission probe

The explicit experimental profile antigravity_default_text_only selects a fresh,
empty disposable workspace and omits both agent and model overrides. It cannot
inherit the historical local custom-agent file or inject MCP, skills or plugins.
The same reviewed binary/config identities, strict stream/schema checks and
finite deadlines apply. The single-use marker is outside the workspace so even
the initial agent cwd contains no DCA file. This is an admission probe, not a
production registry entry. Nonempty capability grants remain unsupported.
The profile changes agent selection only; it does not prove effective tool or
filesystem restrictions and never upgrades advertised inventory into authority.

## Final 1.2.13 admission outcome

The default-agent probe remained blocked. One fresh prompt produced one unique
user_input and num_turns=1, with matching requested/init/terminal schemas.
However, the successful terminal had no structured_output. The stream also
contained one typed run_command attempt (ACTIVE/DONE) and one command denial.
There was no tool-output evidence establishing successful command execution.
The advertised tool list alone was not a failure, and no multiple-input issue
recurred in this probe.

DCA rejected the capture rather than treating exit zero or terminal SUCCESS as
acceptance. A synthetic fixture covers this combination and the independent
missing-structured-output case, including immutable failure shards and no retry.
The exact caller schema may use a string-only const node: it fully constrains
type/value, stays unchanged on the wire, and does not admit unconstrained nodes.

The follow-on workspace probe was not run after L1 failed. Neither the command
denial nor the unchanged workspace proves general filesystem isolation.
The reason the default agent requested a command is not established by this
capture. This is version-scoped evidence, not a claim about all future agy
versions or a grant of subscription usage rights.

| Capability | Offline mapping | Live evidence | Admitted |
|---|---|---|---|
| Text-only inference | Synthetic process/stream tests pass | Strict L1 failed | NO |
| Structured output | Exact schema/terminal validation | Required field missing | NO |
| Workspace read | Unsupported grant | Not tested | NO |
| Workspace write | Unsupported grant | Not tested | NO |
| Outside-workspace isolation | No equivalent enforcement mapped | Not tested | NO |
| Exact-file read | Unsupported grant | Not verified | NO |
| Exact-file write | Unsupported grant | Not verified | NO |
| Shell | Unsupported grant | Unrequested typed attempt and denial; no success proof | NO |
| Network | Unsupported grant | Not verified | NO |
| Web | Unsupported grant | Not verified | NO |
| MCP | Unsupported grant | Not verified | NO |
| Subagent | Rejected evidence | Not verified | NO |
| Custom agent | Diagnostic configuration only | Historical contract failure | NO |
| Resume/session reuse | No command mapping | Not tested | NO |

The production registry stays closed. The experimental Python admission entry
requires fresh explicit approval; this outcome does not authorize another live
call. Beta package metadata is `1.1.0b1`; this does not create a production or
v1.1.0 final release. Authentication and service access remain entirely owned by
the official CLI, with no DCA credential access or API fallback.

## Separate workspace-artifact admission experiment

Coding artifact acceptance is not recovery of the failed structured-result
contract. The [workspace-artifact contract](workspace_artifact.md) audits an
independent Git workspace and parent-run tests; agent prose is diagnostic only.
The first headless coding probe failed before init with a TLS handshake timeout,
zero reported turns and no file changes. This did not establish an interaction
blocker or a working coding backend. No retry or PTY invocation followed.
Beta metadata is `1.1.0b1`; both AGY production routes remain unadmitted.
