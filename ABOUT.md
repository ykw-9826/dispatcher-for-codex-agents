# DCA — Dispatcher for Codex Agents

## English

Run external models from a Codex/GPT controller, with batch execution, result validation, recovery and event-driven continuation. Optional notifications; no main-model polling.

## 简体中文

让 Codex/GPT 主控制器调用外部模型，管理批次、检查结果、恢复失败任务，完成后通过事件交回主线程。可选通知，无需主模型轮询。

## Combined / 中英合并

External model execution and event-driven continuation for Codex/GPT. 让 Codex 调用外部模型，批量执行、检查结果并自动回接。

## Scope

DCA is not an official OpenAI product. Only `CodexCliAdapter` is production-ready;
alternate adapters are not implemented. Continuation requires a dedicated
DCA-owned controller thread, not an arbitrary existing IDE conversation.
No main-model polling does not mean free inference.

DCA 不是 OpenAI 官方产品。目前只有 `CodexCliAdapter` 可用于生产，其他 adapter
尚未实现。续接限 DCA 专用控制器线程，不能接管任意 IDE 对话；无轮询不等于推理免费。
