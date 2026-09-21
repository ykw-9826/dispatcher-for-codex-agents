# DCA v1.0.2 — Dispatcher for Codex Agents

Release candidate notes. Publication requires separate approval.

## English

This update makes result interpretation explicit for noninteractive external
agents. Existing tasks need no new field: `AgentTask.runtime_contract` defaults
to `structured_output="strict_json"` and `rejected_user_input="fail"`. It does
not grant file access, tool permissions or approval authority.

- `json_or_single_fence` optionally accepts one exact outer JSON fence after raw
  JSON parsing fails. It removes only the specified wrapper bytes: no trimming,
  JSON repair, field insertion or reserialization. Raw and normalized bytes,
  SHA256 values, removed ranges and validation-input provenance are retained.
- Activity classifier `dca-activity/2` distinguishes observable execution,
  pre-execution rejection, no tool activity, and unknown/incomplete evidence.
  It checks thread, turn, item and terminal ordering. Invalid lifecycle evidence
  cannot acquire a rejection exemption or become success through valid JSON.
- `warn_if_runtime_rejected` requires call-correlated, lifecycle-consistent
  evidence that `request_user_input` was rejected before execution. Required
  input or unfinished work still blocks acceptance; DCA never answers on behalf
  of the user. The supported CLI mappings do not establish such a real rejection
  event; the positive offline demonstration uses an explicitly synthetic protocol.
- `dca revalidate` creates an independent, hash-pinned interpretation of approved
  historical artifacts. It neither runs old commands nor changes source attempts,
  and adds no model calls or token usage.
- `batch collect --selection` explicitly selects original or qualified derived
  results. Exact-once coverage and plan-owned roles remain mandatory; original
  plus derived double counting, automatic promotion and ledger rewrites are refused.

This is not a general Markdown or damaged-JSON repair feature. Stderr text, model
claims and nonzero exits do not by themselves prove tool rejection. Incomplete,
unknown or lifecycle-invalid evidence cannot authorize the opt-in acceptance
paths. Not every historical shard is recoverable: both supplied real-history
cases remained fail-closed and were not collected, even though their structured
payloads passed schema and application coverage checks.

Validation used offline tests, test executables and a clean wheel installation;
no new model/provider or notification requests were made. File/tool authority,
runtime protection and the notification boundary remain unchanged. See the
[runtime contract](runtime_contract_v1.0.2.md) for exact rules and evidence limits.

## 简体中文

本次更新让非交互外部 Agent 的结果解释方式由任务明确选择。旧任务无需新增字段：
`AgentTask.runtime_contract` 默认仍为 `structured_output="strict_json"` 和
`rejected_user_input="fail"`，不授予文件、工具或审批权限。

- 可选的 `json_or_single_fence` 只在原始 JSON 解析失败后，按精确规则移除一层
  外围 JSON fence。不 trim、不修复 JSON、不补字段，也不重新序列化。原始与
  归一化字节、SHA256、移除区间和实际校验输入的 provenance 分别保存。
- `dca-activity/2` 区分可观察的工具执行、执行前拒绝、无工具活动和未知/不完整
  证据，并检查 thread、turn、item 与 terminal 的顺序。生命周期错误不能获得
  拒绝豁免，也不能因为 JSON 合法就变成成功。
- `warn_if_runtime_rejected` 要求可按调用关联、生命周期一致的执行前拒绝证据。
  仍需用户输入或工作未完成时不能接受，DCA 不替用户回答。当前支持的 CLI 映射
  尚不能确认这种真实拒绝事件；离线正向 Demo 使用明确标记的合成协议。
- `dca revalidate` 对获准读取的历史 artifacts 生成独立、hash-pinned 的重新解释
  记录。不执行历史命令、不改原始 attempt，也不增加模型调用或 token 用量。
- `batch collect --selection` 显式选择原始或合格的 derived result，仍要求
  exact-once 覆盖和计划确定的角色，不重复计数、不自动提升角色、不改旧 ledger。

这不是通用 Markdown 或损坏 JSON 修复器。stderr 文本、模型自述和非零退出码，
都不能单独证明工具在执行前遭到拒绝。证据未知、不完整或生命周期错序时，不能
通过兼容选项放行。也不是所有旧 shard 都能恢复：本次提供的两个真实历史 case，
虽然结构化内容通过了 schema 和业务 ID 覆盖检查，最终仍失败关闭，均未收集。

本版使用离线测试、测试 executable 和全新 wheel 安装验证，没有新增模型/provider
或通知请求。文件与工具授权、runtime 保护及通知边界保持不变。精确规则和证据限制
见[运行合同](runtime_contract_v1.0.2.md)。
