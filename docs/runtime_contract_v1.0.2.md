# Noninteractive runtime contract (1.0.2)

`AgentTask.runtime_contract` is interpretation policy, never permission authority.
Defaults are `structured_output="strict_json"` and `rejected_user_input="fail"`.
Unknown fields, enum values and types fail closed. Omission preserves strict
JSON and unauthorized-tool rejection. Batch plans freeze the same policy in each task.

`json_or_single_fence` first tries the existing JSON parser. Only on JSON parse
failure may it remove the exact opening `b"```json\n"` and a standalone closing
`b"```"`. Exterior whitespace is ASCII JSON whitespace only. Byte slicing retains
every other byte, including whitespace, Unicode escapes and number spelling.
Valid raw JSON never gets transformed to repair a schema failure. The selected
response is still the last completed agent message, not text found in tools or
reasoning. Schema, ID coverage, task roles and capability policy remain mandatory.

Raw persisted JSONL (subject to existing credential redaction), extracted final
UTF-8 bytes and optional normalized bytes are distinct evidence. New completed
captures use a versioned exact artifact list; prelaunch failures and old
eight-file shards keep their layout. Old shards are not rewritten.
Hashes, extraction locator, removed ranges and validator input are recorded.

Tool activity is per call: `TOOL_EXECUTED`, `TOOL_ATTEMPT_REJECTED`, or
`UNKNOWN_OR_INCOMPLETE`. A complete supported observable stream without calls
can report `NO_TOOL_ACTIVITY`; it does not prove absence of unobservable behavior.
Nonzero process exit is execution failure, not proof of pre-execution denial.
Unknown, malformed, truncated or contradictory events never prove rejection.
Model prose, quoted logs and uncorrelated stderr never authorize warning-success.

Classifier `dca-activity/2` checks one ordered thread/turn/terminal lifecycle.
Items (including the selected final message) must occur inside the turn; updates
need a preceding start, item identities/types cannot conflict or reopen after
completion, and started items must finish before the terminal. Completed-only
items and intermediate agent messages remain supported CLI behavior. Lifecycle
uncertainty clears all rejection exemptions and normal per-call classifications.
A completed supported stream with invalid ordering fails `EVENT_STREAM_INVALID`
even under the default contract. Schema success cannot override this failure.

Only `warn_if_runtime_rejected` can downgrade confirmed pre-execution rejection
of `request_user_input`, with no execution conflict, pending/required input or
unfinished required work. Every other acceptance gate must still pass. Permission
requests, unauthorized execution and recursion remain failures. The parent never
answers questions, approves requests or enters Plan mode.

## Protocol evidence and limits

Fixed official sources: OpenAI Codex tags `rust-v0.153.4` and `rust-v0.155.1`,
`codex-rs/exec/src/exec_events.rs` (both SHA256
`c404928e0f2a463e19d1b263081c9d5e0380aec9f651a05ee0766f7bb7527f32`).
CLI item types include command execution, file change, MCP and web search.
Neither version defines a call-correlated user-input rejection item. Do not
interpret App Server events as CLI JSONL. Such real historical records remain
unsupported/unknown until their precise runtime provenance is supplied.
`features.default_mode_request_user_input=false` disables the Default-mode
entry on supported Codex versions; this is not a claim that all interaction
surfaces are hidden. Existing ephemeral exec, approval-never, deadline and
tool controls continue to apply.

The offline demo uses an explicitly synthetic protocol, `dca.synthetic-runtime/1`.
Its structured pre-execution rejection evidence is test machinery, not a captured
Codex event or newly verified provider behavior. Synthetic provenance is retained
through revalidation and collection.

## Historical revalidation

Revalidation consumes an explicit, hash-pinned source descriptor and its exact
source artifact allowlist. No old commands/scripts execute. The original task,
schema, coverage basis and capabilities stay fixed. Only the two interpretation
options change. An independent sealed derivative records source hashes, old/new
outcomes, policy/version hashes, warnings, and zero new calls/usage. Old usage is
reference-only. Unsupported formats, missing/tampered files, unsafe paths and
duplicate output IDs fail closed; original bytes are never modified.

Collection requires explicit original-or-derived selection per logical shard.
There is no discovery/promotion of historical failures. Duplicate original plus
derived selection fails; exact-once coverage and plan-owned authority are unchanged.
Original failures remain failures in the original ledger. No scientific or other
business acceptance is inferred from successful format interpretation.

## Use

For `invoke`, add this object to the existing task JSON. Neither option belongs
in `ModelProfile.capabilities`:

```json
{
  "runtime_contract": {
    "structured_output": "json_or_single_fence",
    "rejected_user_input": "warn_if_runtime_rejected"
  }
}
```

For batch planning, pass `--runtime-contract policy.json` alongside the normal
`batch plan` arguments. The policy file contains just the two inner keys above.
Dry-run validates it without reserving an attempt. Library callers use
`plan_batch(..., runtime_contract=RuntimeContract(...))`.

A caller first pins the **exact** source attempt with the read-only
`describe_source(absolute_shard_path)` library function, then saves/reviews the
returned JSON as `source.json`. Its format is `dca.invocation-shard/1`, with an
absolute canonical `source_directory`, task/profile/attempt IDs, and a `files`
mapping from every allowed basename to SHA256 (including `output_sha256.tsv`).
The supported original eight basenames are:

```text
agent_task.snapshot.json
model_profile.snapshot.redacted.json
input_sha256.tsv
events.jsonl
stderr.log
final_output.json
invocation_result.json
output_sha256.tsv
```

New captured attempts add `interpretation.json` and `raw_final_output.bin`, plus
`normalized_output.bin` only when wrapping was removed. All files are covered by
the existing output hash manifest. `interpretation.json` declares exactly
`dca.invocation-interpretation/1`; arbitrary extra files are rejected. Prelaunch
failures have no captured response and retain the eight-file layout.

Run the following in the initialized checkout, using explicit paths to your
reviewed descriptor and policy. No agent is started:

```sh
dca revalidate --source-manifest "$PWD/source.json" \
  --runtime-contract "$PWD/policy.json" --revalidation-id interpretation-001 \
  --output-root "$PWD/runtime/tmp/revalidation"
```

Library equivalent: `revalidate(source_manifest=..., runtime_contract=...,
revalidation_id=..., output_root=...)`. The output is sealed and contains
`source_manifest.json`, `revalidation_record.json`, `evaluation.json`,
`interpretation.json`, `raw_final_output.bin`, optional `normalized_output.bin`,
and the exact versioned `revalidation_sha256.json`. Verification recomputes the
interpretation as well as hashes. The copied source descriptor preserves its
original bytes. No original TSV is opened: input fingerprints and the original
task/schema are hash-pinned; collection additionally checks the frozen plan's
input hash and task against the source. Standalone records without a batch ID
enum report coverage `NOT_APPLICABLE`, not a claimed coverage PASS.

Select exactly one result per planned profile/shard in `selection.json`:

```json
{
  "version": 1,
  "selections": [
    {
      "profile_id": "example-agent",
      "shard_id": "shard-0001",
      "attempt_id": "attempt-001",
      "revalidation_directory": "/absolute/approved/derived/interpretation-001"
    }
  ]
}
```

Use null or omit `revalidation_directory` to select the original SUCCESS result.
Include all planned profile/shard pairs once; duplicate original plus derived,
missing pairs, wrong attempt/task, failed derivations and role overrides fail.

```sh
dca batch collect --plan-root "$PWD/runtime/tmp/example-batch" \
  --collection-id selected-001 --selection "$PWD/selection.json"
```

Collection preserves warnings and evidence references in
`result_interpretation_audit.json`, plus a hashed `selection.snapshot.json` for
explicit selections. `failed_shards.tsv` still describes the original attempt
ledger: a successful derived collection does not erase those original failures.
Original and derived results cannot both contribute to coverage or token usage.
Source hashes are integrity checks, not signatures or proof that an arbitrary
third-party transcript is genuine; the caller owns the approved evidence source.

## Warning/failure matrix

| Evidence | Default | Explicit warning policy |
| --- | --- | --- |
| Raw valid JSON, complete supported stream | Existing acceptance gates | Same gates |
| One exact outer JSON fence | Schema failure | Only the structured-output opt-in enables normalization |
| Confirmed pre-execution user-input refusal | Policy failure | Warning only if every other gate passes |
| Prose/stderr claiming refusal, unknown or conflicting activity | No rejection exemption | Failure, never warning-success |
| Unauthorized execution, permission requests, recursion | Failure | Failure |
| Missing terminal, bad schema/coverage, timeout, nonzero exit | Failure | Failure |

Default tasks retain valid-stream acceptance and gain conservative activity
diagnostics. An unknown diagnostic is not upgraded to rejection. Opt-in contracts
fail if tool activity cannot be classified; their acceptance is deliberately
conservative. `NO_TOOL_ACTIVITY` only concerns the supported observable stream.
No generic business-semantic oracle is invented: required task work must be
expressed by the caller's existing schema/coverage gates; observable unfinished
work or required user input also prevents a rejection exemption.

## Offline demonstration and historical handoff

```sh
sh scripts/dca-env.sh python examples/runtime_contract_demo.py \
  --output-root runtime/tmp/runtime-contract-demo
```

This runs four virtual calls (two profiles, two shards), retains their strict
failures, and explicitly collects four synthetic derivatives. Three records per
profile remain exact-once; authoritative and shadow roles come only from the
plan. No provider, notification, old command, or historical Evidence is invoked.
The original shards' hashes must be unchanged. Choose a new output directory
for another run; neither attempts, derivatives nor collections are overwritten.

Real-history acceptance requires an exact source
descriptor/allowlist, trusted hashes, original task/schema/input fingerprints,
full raw events/stderr and exit/terminal provenance, exact CLI version, and the
original plan when collecting. A refusal visible only in stderr is insufficient.
Unsupported old filenames or runtime formats stay unsupported; they are not
silently relabeled as the synthetic protocol. Captured bytes are never proof of
filesystem enforcement or of external-agent answer correctness.
Successful JSON normalization and application coverage do not recover a source
whose lifecycle or tool evidence remains unknown. Such a derivative stays failed
and ineligible for collection. A legacy application plan or field layout is not
silently converted into the generic batch contract to make collection succeed.
