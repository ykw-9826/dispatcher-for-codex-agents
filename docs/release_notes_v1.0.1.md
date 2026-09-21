# DCA v1.0.1 — Dispatcher for Codex Agents

Release candidate draft; no tag, GitHub Release or package upload is implied.

## English

This update adds optional task-scoped file and tool permissions to external agent
execution. Without a policy, the existing read-only, no-tool behavior remains.

- Explicit read/write roots and tool allowlists fail closed. Supported grants are
  shell or unified exec, web search, and exact configured HTTPS MCP tools.
- Standalone Codex profile sidecars supply model/provider selection. Reasoning
  effort and verbosity are optional; missing values are not inferred. Invalid
  configuration and I/O failures preserve complete immutable failure artifacts.
- Restricted Linux self-exec compatibility adds only the selected canonical
  Codex executable as an internal read dependency (related upstream issue:
  `openai/codex#29049`). It never grants an entire release or Codex home.
- A separate runtime deny boundary rejects user read/write grants overlapping
  the selected standalone release, including installations outside Codex home.
  Unknown layouts fail closed for explicit capability tasks before launch.
  Requested permissions, effective permissions and protection metadata remain
  separately auditable. See [capability policy](capability_policy.md).

Previous authorized exact-read, default-restricted and exact-write live checks
passed on Ubuntu 24.04 / Codex 0.155.1 through production `codex exec`. The later
deny-boundary fix is checked offline; it does not broaden normal permission
compilation. No live calls were repeated for this candidate. The same Codex
version's `codex sandbox linux` diagnostic dispatch was broken; that diagnostic
failure is separate from the verified production path.

Only the canonical `standalone/releases/<release-id>/bin/codex` layout is
recognized for explicit capability tasks. Launcher symlinks and ambiguous paths
are refused. Other installations/platforms are not certified. Identity/hash
rechecks are not atomic protection against privileged concurrent replacement.
Event audits are not OS enforcement. Missing served-model metadata remains
`NOT_REPORTED`; provider compatibility warnings do not prove model identity.
DCA validates execution and result integrity, not external-agent answer quality.

Deferred to v1.0.2, not implemented here: structured-output JSON fence
normalization and raw/normalized hashes, tool-attempt taxonomy, noninteractive
`request_user_input` handling, and historical shard revalidation.

## 简体中文

本次更新为外部 Agent 增加可选的单任务文件与工具权限。没有显式 policy 时，仍按
原有只读、禁用工具的方式运行。

- 文件读写范围与工具分别授权，非法配置失败关闭。支持 shell 或 unified exec、
  web search，以及已配置 HTTPS MCP server 的精确工具白名单。
- 支持 Codex standalone profile sidecar。model/provider 必填，reasoning effort
  与 verbosity 可选，缺失时不推断默认值；配置或 I/O 错误保留完整不可变失败记录。
- Linux 受限文件系统的 self-exec 兼容处理仅添加所选 canonical Codex binary 的
  内部 read 依赖，不开放整个 release 或 Codex home；相关上游问题为
  `openai/codex#29049`。
- 独立的 runtime 拒绝范围阻止用户读写所选 standalone release 及重叠路径，安装
  位于 Codex home 之外时同样生效。显式 capability 任务遇到未知布局会在启动前
  失败关闭。用户请求、实际权限和保护元数据分开记录，详见[权限文档](capability_policy.md)。

此前获授权的精确读取、默认受限及限定写入 live 检查，已在 Ubuntu 24.04 /
Codex 0.155.1 的 production `codex exec` 路径通过。本次 deny-boundary 修复
只做离线验证，不扩大正常权限编译结果，没有重跑 live 调用。同版本
`codex sandbox linux` 诊断命令的 dispatch 故障，不等于 production 路径失败。

显式 capability 目前只识别 canonical
`standalone/releases/<release-id>/bin/codex` 布局；拒绝 launcher symlink 与
有歧义路径，不宣称其他安装或平台均已认证。identity/hash 复检不构成对特权并发
替换的原子防护，event audit 也不是 OS enforcement。served model 缺失保持
`NOT_REPORTED`，provider warning 不能证明实际服务身份。DCA 保证执行与结果
完整性检查，不保证外部答案正确。

继续留待 v1.0.2，未在本版实现：JSON 外围 fence 归一化与 raw/normalized hash、
tool-attempt taxonomy、非交互 `request_user_input` 处理、历史 shard 重新核验。
