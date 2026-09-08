# Contributing / 参与贡献

Bug reports, documentation improvements, and runtime adapter proposals are welcome.
Use synthetic, minimal reproductions; never submit API keys, tokens, private
configuration, transcripts, or confidential research data.

Before a PR, initialize the checkout as described in README and run the existing
offline tests and checks:

```bash
scripts/dca-env.sh .venv/bin/python -m pytest
scripts/dca-env.sh .venv/bin/ruff check .
scripts/dca-env.sh .venv/bin/black --check .
```

New adapters must fail closed and preserve contract v1, explicit authorization,
role separation, and immutable results. Silent fallback is not accepted. Automatic
scientific adjudication is outside DCA core. Do not require real model calls or
notifications to validate a PR. Contributions are under the project's MIT License.

欢迎 bug report、文档改进及 runtime adapter 提案。请使用最小合成复现，不提交 API
key、token、私有配置、transcript 或机密科研数据。PR 须通过上述现有 tests、Ruff 和
Black。新 adapter 必须失败关闭并保持 contract v1、显式授权、角色隔离和不可变结果；
不接受 silent fallback，不将自动科学裁决纳入 DCA core。验证 PR 不应依赖真实模型或
通知调用；贡献沿用项目 MIT License。
