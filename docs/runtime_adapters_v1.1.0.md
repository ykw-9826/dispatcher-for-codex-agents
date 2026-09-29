# Runtime adapter offline foundation (proposed v1.1.0)

This is an unreleased engineering boundary, not live-runtime certification.
Package metadata remains at the frozen 1.0.4 baseline until a release decision.
Production releases and historical records are not migrated.

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
a separate finite parent process deadline; only the fake driver enforces it here.
No effort mapping is inferred.

These experimental Python helpers are not a stable production adapter API.
compile_headless produces an inert single-shot argv preview. It does not resolve
credentials, create state directories or launch a process. It uses explicit
model, print prompt, stream-json, json-schema, print-timeout and
disable-slash-commands. There is no resume/latest, session pool, account discovery,
authentication fallback or automatic model switch. Equals-bound print/model
values keep flag-like text in the data boundary. Prompts in argv are not a
confidentiality guarantee and are not persisted by this component.

evaluate_headless_capture uses an independent dca-agy-headless/1 contract:
one init, matching conversation identities, ordered ACTIVE/DONE steps, one final
result, and one fresh turn. SUCCESS plus exit zero is necessary but not
sufficient: schema, coverage and complete lifecycle must also pass. Unknown,
malformed, tool-bearing, inconsistent or incomplete streams fail closed.
Unsupported extensions are not silently accepted. Caller cancellation and timeout
override any apparent success. Error diagnostics use fixed categories, not raw
runtime stderr or parser contents.

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
There is no enable switch. Explicit custom executable code remains caller-trusted;
these checks do not authenticate arbitrary renamed programs or sandbox Python
callers who replace the registry with their own executable implementation.

## Next admission gate

Before any live executable registration, establish state-path isolation without
copying production auth, sessions or plugins; verify authentication entitlement;
and independently prove file/tool/network enforcement equivalent to the task
policy. Test exact real terminal/error/usage shapes and retry observability with
separately authorized, bounded live calls. Do not treat fake process termination
tests as real sandbox evidence.

## Authentication and permission admission

As checked on 2026-09-28, the official FAQ
and terms, section 6 prohibit third-party tools
from accessing the service through Antigravity login. DCA therefore records
`ULTRA_ROUTE=NOT_ADMITTED_BY_PUBLIC_TERMS`. Only an explicit vendor exception
covering this architecture could change that gate; its status is
`ULTRA_ROUTE_EXCEPTION=PENDING_EXPLICIT_VENDOR_EXCEPTION`. Wrapping the official
CLI without extracting OAuth does not establish permission to use a subscription.

The CLI authentication guide
describes a separate Gemini API-key provider. That route needs explicit project,
credential-reference and spending authorization: `API_ROUTE=NOT_AUTHORIZED`.
No key is discovered or used by this foundation, no modelProvider setting is
changed, and there is no fallback between API and account-based authentication.
The subscription exception does not block offline API-route design.

The headless contract permits
cached authentication and specifies an authentication error for unauthenticated
noninteractive runs. It is not a state-isolation guarantee. The
permission rules allow workspace
file reads/writes by default; write implies read, directory grants are recursive,
and deny takes precedence over ask and allow. Terminal
sandboxing and web/network
rules interact. These semantics are not proven equivalent to DCA exact-file
capabilities: `ANTIGRAVITY_PERMISSION_ENFORCEMENT=NOT_VERIFIED`.

If API live testing is separately authorized, first establish a dedicated
temporary workspace and isolated state/auth with sandbox on, no ungranted network,
outside-workspace denial, read-only versus writable boundaries, and observable
headless soft-denial behavior. Do not use a real development or scientific
workspace for initial permission probes. Cached production credentials, plugins,
hooks and background sessions must not enter that workspace. No such enforcement
or authentication test has been performed by this offline foundation.

## Future live accounting contract (not authorization)

At most four DCA logical invocations may be proposed in a separately approved
acceptance plan. This does not guarantee four physical provider requests: runtime
internal retries may be unobservable, in which case the request count stays
UNKNOWN. Each proposed call requires a unique attempt, no DCA retry, a finite
parent deadline, raw-stream preservation, usage/provenance capture and one fresh
conversation without resume/latest. A process timeout is not a monetary limit.

`ANTIGRAVITY_LIVE_AUTHORIZATION=BLOCKED_PENDING_API_OPT_IN_OR_VENDOR_EXCEPTION`.
Authorization alone will not replace the independent state/permission enforcement
gate. This checkpoint executes none of these proposed invocations.
