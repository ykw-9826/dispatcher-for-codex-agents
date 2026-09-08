# Independent project governance / 独立项目治理

The current checkout is the engineering root, not a maintainer-specific absolute
path. A maintained project has its own Git history, packaging/lockfile, writable
environment and runtime. Temporary extracted snapshots used for validation are not
second development repositories. Do not import another project's source or share
its writable virtual environment. Cross-project use is through contract v1.

当前 checkout 是工程根，不以维护者的绝对路径作为使用要求。正式项目有独立 Git、
packaging/lock、可写环境及 runtime；验证用临时快照不是第二个长期开发仓库。不得
跨项目 source import 或共享可写环境，只通过 contract v1 使用。

Source, reviewed documentation, examples and tests are versioned. Actual configs,
credentials, state/ledgers, runs, reports, exports, environments and caches are not
public source. Preserve original private history and evidence. A public whitelist
snapshot is not a sanitized copy of private Git history: never push that history.
License, ownership, attribution and publication require explicit owner decisions.

源码、审查后的文档/示例/测试进版本控制；实际配置、凭据、状态/ledger、runs、报告、
exports、环境/cache 不属于公开源码。原私有历史和证据原字节保存，白名单快照不等于
私有 Git 历史已脱敏；不得推送该历史。许可证、owner、署名和发布需明确人工决定。

Use a project-local .venv. Shared uv/cache/managed Python are optional deployment
choices through environment variables, not fixed paths. Keep dependencies locked;
never upgrade Python, Codex or another project's environment implicitly. New Python
installations use --no-bin and an explicitly approved installation directory.
No home bin entries or root-external writes without prior authorization.

使用项目 .venv；共享 uv/cache/Python 是通过环境变量指定的可选部署策略，不是固定
路径依赖。依赖锁定，不隐式升级 Python、Codex 或其他环境。显式安装 Python 使用
--no-bin 和已批准目录；home bin 入口及根外写入必须事前获批。

Workspace configuration is 0600 and owned by the current user; the initialized
checkout and private runtime directories are 0700. Inputs are allowlisted,
outputs explicitly selected under configured runs/runtime. Missing or malformed
configuration fails closed, with no silent home/tmp fallback. Library callers
without workspace configuration must explicitly select a private TMPDIR for
temporary work. These checks do not certify arbitrary host OS isolation.

workspace 配置由本用户持有且为 0600；初始化 checkout 和私有 runtime 为 0700。
输入白名单、输出明确选择配置根下 runs/runtime。缺配置或配置错误时失败关闭，
不静默回退 home/tmp；无 workspace 的库调用须显式指定私有 TMPDIR。不能据此宣称
任意宿主 OS 隔离已通过验收。

The parent is the only artifact writer. Shards are immutable; retries require a
new attempt and explicit reason. Cancellation, permission waits, budgets, expiry
and ownership precede continuation. Events never authorize scientific adjudication,
model replacement, repository writes or a new project stage. No daemon, queue,
recursive swarm, silent fallback or automatic retry is part of this scope.

DCA's scope is independent external-runtime execution for a Codex/GPT controller,
not a generic multi-agent framework. Native same-provider child orchestration
belongs to Codex. Capability comparisons are versioned observations, not permanent
claims. Interface cleanup must preserve existing production releases and historical
artifacts; no compatibility alias or automatic migration is implied.

父进程是唯一 artifact writer。shard 不可覆盖，retry 须新 attempt 和明确理由；取消、
权限等待、预算、期限及所有权优先于续接。事件不授权科学裁决、模型替换、仓库写入或
下一阶段；不扩建 daemon、队列、递归 swarm、silent fallback 或自动 retry。
