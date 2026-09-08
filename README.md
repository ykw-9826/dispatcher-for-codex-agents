# DCA — Dispatcher for Codex Agents

Run external models from a Codex/GPT controller, with batch execution, checked results and event-driven continuation.

[English](#english) · [简体中文](#chinese)

<a id="english"></a>

## Why DCA

Codex already has native subagents. In our Codex 0.153.4 test, they could run in parallel and report back, but a custom child could not switch away from its parent's provider. An Astra parent therefore could not send its native children to GLM or DeepSeek through the configured external provider. This is a [version-specific observation](docs/native_capabilities.md), not a permanent claim about Codex.

I wanted to hand suitable work to external models, use less GPT quota, and stop copying prompts and results between tools. DCA runs those tasks in independent processes and keeps their results together. It does not replace Codex's native agent tree or spawn/send/wait tools. Actual savings depend on the models and workload; controller work still uses inference.

For long jobs, DCA waits for completion events, then returns checked results to its dedicated Codex controller thread. The main model does not have to keep asking whether the work is done.

## What it does

- Hands approved tasks to external agents through independent runtime processes.
- Runs batches with the same inputs for each selected model.
- Saves results and checks their format, completeness and integrity.
- Keeps model outputs separate so you can compare them.
- Preserves failed attempts; recovery skips valid completed work and requires approval for retries.
- Returns verified completion events to its own Codex controller thread.
- Optionally sends ServerChan or HTTPS webhook notifications.

## Quick start

You need `uv` on PATH and **Python 3.11.15**, pinned in `.python-version`. From a new checkout, run these commands **in your terminal**. `uv sync` creates the project's `.venv` and installs the locked dependencies.

```bash
python3 scripts/init-workspace.py
scripts/dca-env.sh uv sync --frozen
.venv/bin/dca --help
.venv/bin/dca-notify --help
```

Initialization creates private workspace paths and disables notifications; it refuses to overwrite existing configuration. Dependency installation may contact the package index, but does not call a model. A populated cache supports `--offline`. If the pinned Python is missing, first select an approved `UV_PYTHON_INSTALL_DIR` and use `uv python install --no-bin`. See [installation and recovery](docs/dispatcher_for_codex_agents.md).

Run the **test demo**:

```bash
.venv/bin/python examples/test_demo.py --output-root runs/test-demo
```

Three invented records go through planning, virtual calls, validation, recovery and collection. It needs no API key or Codex installation: **no real model calls, inference charges or notifications**. Use a new output directory each time; results are in `demo_result.json` and `plan/collections/demo/`.

To exercise the full external-provider flow, including the controller handoff:

```bash
.venv/bin/python examples/cross_provider_demo.py --output-root runs/cross-provider-test
```

This also runs entirely on bundled test executables. GLM, DeepSeek and the Codex controller are simulated; it checks distinct provider configurations and a real DCA event handoff, not live model quality or current provider connectivity. Add `--dry-run` to validate the test plan without executing its external agents. There is no live switch; real calls require separate approval and reviewed configuration.

## How it works

```text
Codex/GPT controller -> DCA -> GLM external agent      -> checked results
                           -> DeepSeek external agent -> controller summary
```

DCA writes result files; external agents receive only selected input over stdin. Failed attempts are kept intact. Cancellation, missing or expired permission, or another writer owning the controller thread blocks continuation. See [operations](docs/dispatcher_for_codex_agents.md) and [contract v1](docs/contract_v1.md) for the details.

## CLI

- `invoke` — run one external agent task.
- `batch plan/run/status/collect/retry-plan/monitor` — manage batches and their results.
- `bridge start/status/cancel/recover` — run a dedicated controller with event-driven continuation.
- `dca-notify` — optional notifications and explicitly approved hook integration.

```bash
.venv/bin/dca bridge start --help
```

[bridge.example.json](examples/bridge.example.json) is deliberately inactive. A real controller requires fresh, time-limited authorization and an explicit inference budget. Starting one can incur charges even when its external jobs are virtual calls.

## Current limitations

- DCA is a third-party tool, **not an official OpenAI product**.
- Only `CodexCliAdapter` is production-ready. Alternate runtime adapters are not implemented; reserved adapters fail closed.
- Continuation works only in a **dedicated DCA-owned controller thread**. It cannot wake an arbitrary existing IDE Codex conversation.
- No main-model polling does **not** mean free inference. External model work and controller continuations still cost money.
- Current external agents receive read-only, no-tool tasks. DCA does not build a generic native agent tree or recursively delegate work.
- Linux has been tested locally; Windows and macOS are not verified. Bridge support depends on the installed Codex App Server interface.
- DCA does not make scientific decisions for you, silently switch models, or retry failed work without approval.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Use synthetic examples, keep credentials out of contributions, and run the offline checks:

```bash
scripts/dca-env.sh .venv/bin/python -m pytest
scripts/dca-env.sh .venv/bin/ruff check .
scripts/dca-env.sh .venv/bin/black --check .
```

## License

MIT License. See [LICENSE](LICENSE).

---

<a id="chinese"></a>

## 为什么做 DCA

Codex 已经有原生多 Agent。在我们对 Codex 0.153.4 的测试中，子线程可以并发执行、等待和回传结果，但 custom child 不能覆盖父线程的 provider。因此，Astra 主线程还不能直接把原生子任务交给已配置的 GLM、DeepSeek 外部 provider。这是[特定版本的实测结果](docs/native_capabilities.md)，不是说 Codex 永远不支持。

我写 DCA，是想让主 Codex 把合适的任务交给外部模型，少用一些 GPT 额度，也省掉手工来回搬 Prompt 和结果的麻烦。它负责启动独立模型进程、管理长任务和结果，不替代 Codex 原生的子线程调度。实际能省多少取决于模型和任务，主控制器本身仍有推理开销。

任务跑着的时候，主模型不必反复问“完成了吗”。DCA 等待完成事件，检查结果，再交回自己管理的专用 Codex 控制器线程。

## 能做什么

- 把获批任务交给外部模型 Agent，分别启动独立进程。
- 批量执行，让选定的模型处理相同输入。
- 保存结果，检查格式、完整性和文件校验值。
- 分开保存各模型输出，方便对比。
- 恢复时跳过已完成项，保留失败记录；重试须先批准。
- 任务完成后，通过核验过的事件自动交回专用主 Codex。
- 按需发送 Server酱或 HTTPS webhook 通知。

## 快速开始

需要 PATH 中已有 `uv`，以及 `.python-version` 固定的 **Python 3.11.15**。在新 checkout 根目录，**打开终端执行以下命令**。`uv sync` 会创建项目 `.venv` 并安装锁定依赖。

```bash
python3 scripts/init-workspace.py
scripts/dca-env.sh uv sync --frozen
.venv/bin/dca --help
.venv/bin/dca-notify --help
```

初始化会创建私有工作区路径，默认关闭通知，不覆盖已有配置。安装依赖可能访问包索引，但不会调用模型；缓存齐备时可用 `--offline`。缺少指定 Python 时，先选定获批的 `UV_PYTHON_INSTALL_DIR`，再使用 `uv python install --no-bin`。详见[安装与恢复](docs/dispatcher_for_codex_agents.md)。

运行**测试 Demo**：

```bash
.venv/bin/python examples/test_demo.py --output-root runs/test-demo
```

Demo 用 3 条虚构记录走完规划、虚拟调用、校验、恢复和汇总。不需要 API key 或安装 Codex：**不调用真实模型，不产生推理费用，不发送通知**。每次选一个新输出目录；结果在 `demo_result.json` 和 `plan/collections/demo/`。

想看包含主控制器回接的跨 provider 完整流程，可以运行：

```bash
.venv/bin/python examples/cross_provider_demo.py --output-root runs/cross-provider-test
```

这个示例也只运行随包测试程序。GLM、DeepSeek 和 Codex 控制器都是模拟的，验证的是独立 provider 配置与 DCA 事件交接，不是实时连通性或模型质量。加 `--dry-run` 只检查测试计划，不执行外部 Agent。示例没有 live 开关；真实调用必须另行批准并核对配置。

## 工作方式

```text
Codex/GPT 主控制器 -> DCA -> GLM 外部 Agent      -> 校验结果
                        -> DeepSeek 外部 Agent -> 主控制器汇总
```

结果文件统一由 DCA 写入，外部 Agent 只经 stdin 接收选定输入。失败记录不会覆盖；任务取消、权限缺失或到期、控制器线程已有其他 writer 时，都不会自动继续。详细用法见[操作与恢复](docs/dispatcher_for_codex_agents.md)和 [contract v1](docs/contract_v1.md)。

## CLI

- `invoke`：执行一个外部 Agent 任务。
- `batch plan/run/status/collect/retry-plan/monitor`：管理批次及结果。
- `bridge start/status/cancel/recover`：启动专用控制器，管理事件续接。
- `dca-notify`：可选通知，以及需要明确授权的 hook 集成。

```bash
.venv/bin/dca bridge start --help
```

[bridge.example.json](examples/bridge.example.json) 默认不可运行。真实控制器需要重新授权、设定期限和有限推理预算。即使外部任务只是虚拟调用，真实主控制器仍可能产生费用。

## 当前限制

- DCA 是第三方工具，**不是 OpenAI 官方产品**。
- 目前只有 `CodexCliAdapter` 可用于生产。其他 runtime adapter 未实现，选择预留 adapter 会 fail closed。
- 自动续接仅限 **DCA 拥有的专用 controller thread**，不能唤醒任意已经打开的 IDE Codex 对话。
- 没有主模型轮询，**不等于推理免费**。外部模型执行和主控制器续接仍有费用。
- 当前外部 Agent 只处理只读、禁用工具的任务；DCA 不自建通用原生 Agent 树，也不递归派发任务。
- 目前在 Linux 本地验证过，尚未验证 Windows/macOS。bridge 需要所安装的 Codex App Server 接口支持。
- 科学判断由你来做。DCA 不会悄悄切换模型，也不会未经批准重试失败任务。

## 参与贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。请用合成示例、不提交凭据，并运行离线检查：

```bash
scripts/dca-env.sh .venv/bin/python -m pytest
scripts/dca-env.sh .venv/bin/ruff check .
scripts/dca-env.sh .venv/bin/black --check .
```

## 许可证

本项目采用 MIT License，详见 [LICENSE](LICENSE)。
