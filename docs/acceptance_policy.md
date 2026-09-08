# Core execution acceptance and semantic diagnostics

Policy revision: 1. Scope: DCA v1 release acceptance, not a new runtime schema.

DCA's responsibility is bounded task execution, result integrity and provenance.
It does not guarantee that an external agent's answer is correct. A runtime
SUCCESS is not a factual endorsement or permission to act on a result.

## Execution/system acceptance: release hard gate

The approved execution must use the configured runtime/profile and stay within
its input, timeout, call, tool and writer boundaries. Required checks include:

- Complete, valid event streams, expected terminal events and successful exits.
- The declared output schema and exact-once planned record coverage.
- Complete immutable artifacts, reproducible hashes, and collector preservation
  of the returned JSON values without semantic correction.
- Recorded usage, requested/configured identity, served identity when reported,
  and interpretable failures and warnings.
- No unauthorized retry, model fallback, resume, recursion, notification or
  project write. Existing isolation limitations still apply.

A violation of these checks remains blocking. Correct-looking content cannot
excuse missing IDs, an invalid schema, corrupt artifacts or a policy violation.
Conversely, a schema-valid wrong answer preserved faithfully does not by itself
fail DCA core execution acceptance.

## Semantic oracle: diagnostic by default

Compare the external answer with an independent expected answer or business rule
and record that result separately. By default, an oracle mismatch is diagnostic
and does not determine the core transport/runtime release gate. Do not repair the
answer, discard the mismatch, alter a successful InvocationResult, or retry it
without approval merely to obtain a passing diagnostic.

An application may explicitly adopt semantic correctness as its own acceptance
gate before execution. Any constraints encoded in the declared JSON schema remain
mandatory: this policy does not downgrade schema validation. Business acceptance
and DCA system acceptance must remain distinguishable in reports.

## Provider compatibility warnings

Model-catalog decode failures, fallback model metadata and reasoning-summary
ordering errors are compatibility warnings when the required execution checks
still pass. Preserve the original evidence and label acceptance with warnings;
do not silently remove them or treat every occurrence as harmless. If they break
terminal capture, schema, integrity, routing or policy checks, execution fails.

Fallback model metadata is not proof of model switching. Neither metadata nor a
configured model proves the provider-served identity. Missing served metadata
stays `NOT_REPORTED`; physical request counts stay `UNKNOWN` when unobservable.

## Historical interpretations

A changed acceptance policy requires a new, separately dated summary referencing
the original reports and their hashes. Preserve the original report, invocation,
input, output, event stream and manifest bytes, including any earlier FAIL label.
A reinterpretation is not a new live test and does not change the original answer.

## 中文说明

DCA 负责受约束的任务执行、结果完整性和 provenance，不保证外部 Agent 内容正确。
执行／系统验收是 core release 的硬门禁；语义 oracle 默认是独立诊断项。格式合法且
被原样保存的错误答案，可以同时得到“执行 PASS”和“语义 FAIL_DIAGNOSTIC”。

业务方可以事前显式把语义检查设为自己的门禁；写入输出 schema 的约束始终必须
满足。不得以这次分层为理由放宽 schema、终态、覆盖率、完整性或安全检查。

model catalog、fallback metadata 和 reasoning-summary ordering 告警应保留并披露。
只有执行硬门禁仍通过时，才能作为非阻断兼容性告警；不能据此推断 served model。
缺失值继续写 NOT_REPORTED。重新解释只能写新 summary，不能覆盖旧 FAIL 报告，
也不代表新增 live 验收、修正模型答案或批准自动重试。
