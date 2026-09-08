# Public-source preparation / 公开源码准备

The first public candidate is a whitelist snapshot, without .git. Do not push the
development branch's original history: it contains private deployment and source
mapping information. Neither a new branch nor .gitignore removes old Git objects.
The source whitelist and SHA256 manifest define the reviewed bytes. A one-time
public staging repository starts new, parentless history from that snapshot,
with a local origin for the owner-approved public repository. It is not a second
maintained development repository. No push, tag, GitHub Release or package-index
upload is performed by this preparation.

首次公开候选是无 .git 的白名单快照。不得推送开发分支原历史，其中保留私有部署和
来源映射。新分支或 .gitignore 不会删除旧 Git 对象。审查范围以白名单及 SHA256 为准；
一次性 public staging 仓库从该快照建立无父提交的全新历史，并设置所有者批准的公开
仓库为本地 origin；它不是第二个长期维护的开发仓库。本轮不执行 push/tag，不创建
GitHub Release，不上传包索引。

Excluded: actual configurations, secrets, authentication/trust stores, private
reports/provenance, runs, scientific materials, transcripts, environment/cache/
runtime and all Git metadata. Synthetic fixtures and compatibility protocol names
are explicitly allowed. Review generic fake credential markers as test data, not
usable credentials. Do not bundle dependency wheels or their installed metadata
as project-owned code.

排除实际配置、密钥、认证/信任存储、私有报告/来源、runs、科研材料、transcript、
环境/cache/runtime 及全部 Git 元数据。允许明确的合成 fixture 和兼容协议名称；
fake 凭据标记只是测试数据，不是可用凭据。依赖 wheel 及安装元数据不能冒充项目代码。

## License and attribution / 许可证与署名

The owner explicitly approved the standard [MIT License](../LICENSE), with
`Copyright (c) 2026 ykw-9826`. Repository ownership remains with `ykw-9826`.
License and owner are confirmed. The initial public history exists; further pushes
still require user approval and must not import the private development history.
This preparation does not create a GitHub Release or publish to PyPI.

所有者已明确批准标准 [MIT License](../LICENSE)，版权行为
`Copyright (c) 2026 ykw-9826`，仓库所有者仍为 `ykw-9826`。
许可证及 owner 已确定，首次公开历史已建立；后续 push 仍须用户批准。本次不创建 GitHub Release，
不发布 PyPI。

A scoped review found no separately copied third-party implementation or real
research fixtures in the prepared source. This is not proof of complete authorship
or a legal clearance. Runtime dependency: Pydantic (and locked transitives).
Development tools: pytest, Ruff and Black; build tools: setuptools and wheel.
Their license notices remain with their distributions; inspect the exact locked
versions before redistributing any dependencies. Official protocol documentation
is referenced by links, not vendored documentation/source.

范围内审查未发现单独复制的第三方实现或真实科研 fixture，但这不证明全部作者身份，
也不是法律确权。运行依赖为 Pydantic 及锁定间接依赖；开发工具为 pytest/Ruff/Black，
构建工具为 setuptools/wheel。许可声明随各自 distribution 保存，若再分发依赖须核验
锁定版本。官方协议文档仅链接引用，不复制整份文档或其源码。

## Verification / 验证

The fake demo and full tests must pass from an initialized clean snapshot using
only tracked/whitelisted inputs. No existing installation, private config, secret,
real model or phone request may be required. The public candidate has no hosted
CI workflow or badge. Local fake/offline results do not certify a live runtime or
every operating system. Further publication requires explicit user approval.

fake demo 和全量测试必须在初始化后的干净快照内通过，只用白名单输入，不依赖已有
安装、私有配置、密钥、真实模型或手机请求。本候选没有托管 CI workflow/徽章。
本地 fake/offline 结果不证明 live runtime 或所有 OS 已验收；后续发布须另行批准。
