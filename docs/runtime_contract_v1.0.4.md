# Codex exec runtime contract — 1.0.4 supplement

This versioned supplement updates the lifecycle interpretation in the unchanged
[1.0.2 contract](runtime_contract_v1.0.2.md). It does not change task permissions,
normalization, schema, coverage, provider dispatch or acceptance policy.
Package version `1.0.4` and activity-classifier version `dca-activity/3` are
separate identifiers. The output normalizer and schema-validator versions remain
`dca-output/1` and `dca-schema/1`.

## Initial pre-turn diagnostics

Rule `ALLOW_PRETURN_COMPLETED_ERROR_ITEM` applies only after a valid initial
`thread.started` with a nonempty string `thread_id`, before the first
`turn.started`. It accepts zero or more events with this shape:

```json
{
  "type": "item.completed",
  "item": {"id": "notice-1", "type": "error", "message": "Synthetic notice"}
}
```

The item ID must be a nonempty string and satisfy the existing identity and
uniqueness checks. The message must be a string. No message keyword, provider or
model name selects this rule. Each accepted item is audited as
`PRETURN_NON_FATAL_DIAGNOSTIC`, not a tool call, confirmed rejection or successful
turn. Raw items/messages remain unchanged in the captured event artifact; the
activity audit records applied rule IDs and exact event-index/item locators.

The exception does not apply before thread start, inside a turn, after a terminal,
between turns, or after a top-level `error`. A stream error closes the initial
window permanently; a later turn start or successful terminal cannot erase it.
Other pre-turn item types, started/updated items, malformed diagnostics and unknown
pre-turn events remain hard lifecycle failures. In-turn error-item interpretation
is unchanged. Unsupported CLI versions do not acquire this exception.

## Wire evidence

The supported interface is `codex exec --json`, not App Server JSON-RPC. Fixed
OpenAI sources are `rust-v0.153.4` and `rust-v0.155.1`,
`codex-rs/exec/src/exec_events.rs` (both SHA256
`c404928e0f2a463e19d1b263081c9d5e0380aec9f651a05ee0766f7bb7527f32`).
They distinguish unrecoverable `ThreadEvent::Error` from non-fatal
`ThreadItemDetails::Error` with `message: String`.

At `rust-v0.155.1`, `codex-rs/exec/src/event_processor_with_jsonl_output.rs`
projects ConfigWarning, Warning and DeprecationNotice into completed error items
with generated identities. Their origin is not a typed discriminator in the
resulting item; DCA does not infer a model-metadata subtype from message prose.

## Acceptance and historical revalidation

`NO_TOOL_ACTIVITY` has its existing meaning: no tool activity in the supported
observable stream, not proof about unobserved behavior. It may pass a task that
requires no tools when all existing terminal, exit, permission, schema and
coverage gates pass. The new diagnostic rule adds no exemption to these gates.

Stderr-only user-input signals cannot establish a structured tool attempt,
request identity, pre-execution rejection or same-turn correlation. Such evidence
remains `UNKNOWN_OR_INCOMPLETE`, with no confirmed-rejection exemption. An opt-in
runtime contract fails closed with `POLICY_VIOLATION` for that uncertainty even
when lifecycle and schema independently pass. The separately labelled synthetic
test protocol is not evidence that exec emits a user-input rejection item.

Revalidation remains explicit and immutable. New derivatives record the source
attempt, per-file hashes, captured raw JSONL SHA256, classifier version, applied
rule IDs, lifecycle and taxonomy. Raw-event hashes are not whole-shard hashes.
Old verdicts, ledgers and sealed derivatives are not rewritten or auto-promoted.
Verification recomputes using the installed classifier; an older derivative must
be checked with its original implementation or replaced by a separately identified
derivative, never silently migrated. Version 2 remains identifiable in its original
records and the preserved 1.0.2 documentation.

The [core acceptance policy](acceptance_policy.md) is unchanged: schema-valid
content is not automatically correct, and runtime success grants no business or
scientific approval.
