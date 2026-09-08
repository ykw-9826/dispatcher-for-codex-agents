# Native Codex and DCA / 原生 Codex 与 DCA

DCA runs independently configured external model processes for a Codex/GPT
controller. It is not a general multi-agent framework or a substitute for native
spawn/send/wait. Use native Codex for its own child-thread orchestration.

DCA 管独立外部模型进程及其长任务，不替代 Codex 自带的子线程机制。

## Versioned observation / 版本化能力记录

Observed on **Codex CLI 0.153.4**, 2026-09-07, with a `gpt-6-astra` parent and
custom roles requesting `glm-5.3` and `deepseek-v4-flash` through an existing
external provider. This records a prior live test; the interface cleanup did not
repeat it. No account details, session IDs or private runtime artifacts are
required to interpret the result.

| Capability | Codex 0.153.4 observation | DCA boundary |
|---|---|---|
| Native child threads | PASS | Does not replace native threads |
| Parallel native children | PASS, overlapping lifetimes | Independent external processes, not a native agent tree |
| Parent wait / failed-result collection | PASS | Batch result validation and durable artifacts |
| Per-child model override | PASS | Model selected by each standalone sidecar |
| Per-child provider override | UNSUPPORTED in tested custom-role path | Independent runtime configuration, not parent inheritance |
| Astra → Volcengine GLM/DeepSeek native child | UNSUPPORTED in tested path | External CodexCliAdapter processes remain the applicable route |
| Follow-up after successful native child | NOT_TESTED: both children failed | Not a claim that native follow-up is unavailable |

The two children were created but returned HTTP 400 errors saying their requested
models were unsupported with a ChatGPT account. No external-provider result was
obtained. Effective routing is inferred from these errors and the version-pinned
implementation, not from a child-reported provider field. Served model metadata
was absent: `NOT_REPORTED`. Failure billing and physical request counts were not
observable and remain `UNKNOWN`.

The [0.153.4 role implementation](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/agent/role.rs)
applies bounded overrides including `model`, but does not include `model_provider`
in `AgentRoleOverrides`. The parent-derived provider stays in place. General
[subagent documentation](https://learn.chatgpt.com/docs/agent-configuration/subagents)
does not override that version-specific observation. Future versions may differ;
retest before changing this matrix.

两个原生子线程确实创建且有并行时间，但模型名覆盖后仍走父线程的 ChatGPT 路径，
没有取得火山模型结果。上述 UNSUPPORTED 只针对这个版本和 custom-role 路径；
不代表 Codex 没有多 Agent，也不推断未来版本。单独 child 的成功 follow-up 未测。

## What DCA adds / DCA 负责什么

- Independently configured external calls through `CodexCliAdapter`; other
  runtime adapters remain reserved and not implemented.
- Immutable plans/attempts, selected stdin inputs, strict schemas and exact-once
  result coverage, with authoritative/shadow/diagnostic outputs kept separate.
- Explicit retry plans and recovery that do not overwrite failed attempts.
- Verified events returning to a dedicated DCA-owned controller thread, plus
  optional notifications. An arbitrary open IDE conversation cannot be awakened.

The [test demo](../examples/cross_provider_demo.py) uses simulated GLM/DeepSeek
configurations and a simulated controller. It exercises DCA's existing execution,
collector and bridge; it proves neither fresh live connectivity nor model quality.
There is no live switch. Only separately approved real tasks may use real hosts.

跨 provider 测试 Demo 全程使用虚拟调用，包括主控制器。它验证工具通路，不新增
live 验收证据。真实任务仍需独立授权；主模型无轮询不代表实际推理免费。

DCA is a third-party tool, not an official OpenAI product. It does not make
automatic scientific decisions. Its value here is external execution and result
management, not recreating native collaboration tools.
