# H14 项目规则、Skills 与扩展

## 项目规则如何生效

1. 系统和操作者策略决定身份、项目、读写范围、可用工具、网络与费用。用户当前的需求在这些范围内决定任务。
2. Harness 从本轮隔离工作树读取适用的 `AGENTS.md`。根目录规则先加载，越深的目录规则只在自己的子树内覆盖更广的规则。未指定目标时，Harness 加载根规则和受权的规则索引，目录正文按实际编辑目标发现。
3. 第一次尝试编辑尚未加载规则的目录时，`apply_edits` 返回 `PROJECT_GUIDANCE_REQUIRED`。本批次后续写入同样延迟。下一次模型请求携带已保存的规则，模型可在重新判断后发起新调用。原批次没有写入、内层编辑记录或 `patch_applied` 事件。
4. 显式选择的 Skill 在适用目录规则之下提供操作建议。README、构建配置和工具输出均作为引用资料进入上下文。
5. 规则带有来源路径、作用域、SHA-256 和字节边界。当前 Run 的规则在压缩中保留，恢复使用同一份字节和原模型请求指纹。项目授权、执行 epoch 与读写范围在读取、回放、模型发送和工作区写入边界重新检查。同一会话开启新一轮时重新选择适用规则。

规则通过普通 UTF-8 文件读取。符号链接、受保护路径和超出读权限的文件无法作为规则或 Skill 来源。适用规则超出交付上限时，写入被阻止。

## 在项目里添加 Skill

将说明保存在例如 `src/.agents/skills/review/SKILL.md` 的路径下。

```markdown
---
name: review
description: 检查变更与现有测试
---
检查目标代码和当前测试，列出已验证的行为与证据。
```

用户可以输入「使用 $review 检查 src/example.py」，也可显式给出 Skill 文件路径。在当前路径下，同名 Skill 采用最具体的适用目录。同一目录优先使用 `.agents/skills`，兼容旧版 `.codex/skills`。并列目录下出现歧义时，给出准确的 Skill 路径。发现阶段只读取有界的名称和描述，正文由显式选择触发。项目 Skill 的正文和元数据都没有工具安装、命令执行或授权语义。

## 操作者启用固定版本插件

在服务环境设置 `MGAR_V3_HARNESS_PLUGIN_LOCK` 为所有注册项目目录外的绝对路径，例如 `/home/imini/.config/mgar/harness-plugins.json`。操作者在宿主机安装已经审核的 Python 扩展和固定依赖。项目输入、模型、浏览器请求与 MCP 返回值都没有修改锁文件的权限。

在 `v3/` 用下面的示例为一个已注册项目安装并启用内置样例。项目 ID 可从 `mgar harness projects` 或项目列表获取。保留锁文件中其他插件的配置。

```bash
uv run --frozen python - "<PROJECT_ID>" <<'PY'
import sys

from mgar_v3.harness.extension_adapters import default_catalog
from mgar_v3.harness.plugins import PluginLock
from mgar_v3.settings import Settings

path = Settings().harness_plugin_lock
if path is None:
    raise SystemExit("Set MGAR_V3_HARNESS_PLUGIN_LOCK first")
catalog = default_catalog(path)
current = catalog.lock() if path.is_file() else PluginLock()
probe = catalog.pin(
    "workspace_probe", "1.0.0",
    projects=(sys.argv[1],), enabled=True, read_paths=("README.md",),
)
catalog.write(PluginLock(plugins=(
    *(pin for pin in current.plugins if pin.plugin_id != probe.plugin_id),
    probe,
)))
PY
```

此项目下一轮会话可调用 `workspace_probe__measure`，读取已授权的 `README.md` 前缀并在离线容器计算字节数及行数。容器只获准读取该文件，记录在原 Run 的命令账本中。Python 插件工厂属于操作者安装的可信代码，样例的项目数据处理使用 Broker 沙箱。

- 安装时可以把 `enabled` 留为 `false`。使用 `PluginCatalog.write` 原子替换锁文件，调整启用状态或切换到已装的新版本。卸载时从锁中移除该项。
- 锁固定插件标识、版本、源码和依赖 SHA-256、项目、工具与源路径范围、响应尺寸和超时。模型项另固定能力与价格指纹。运行中的调用持续复核原锁和授权。切换、撤销或移除会阻止旧结果回放或向模型发送，执行中的插件调用会清理资源。
- 一个既有 Run 保留原插件绑定。启用后新建一轮，对应的新 Run 会选择新锁；相同会话仍可延续正常的对话上下文。模型与工具结果的历史回放依旧经过当前授权。

## 扩展 SDK 与 MCP

`harness.plugins.ToolPlugin` 接收固定的命名空间、工具清单、安装在宿主机的源码和依赖文件，以及 `ToolMount` 工厂。实现 `TrustedExtension.specs`、`paths`、异步 `invoke` 和 `close`。工具名以 `插件名__工具名` 暴露，使用原工具日志、权限和调用额度。扩展结果的正文作为观测数据，扩展元数据无法生成补丁、测试证明或发布授权。`WorkspaceProbeExtension` 提供了可运行样例。[样例](../../src/mgar_v3/harness/extension_adapters.py)

`harness.plugins.ModelPlugin` 工厂接收已经授权的供应商凭据、实际上下文窗口、冻结的 `ModelCapabilities` 和 CAS。每个项目最多绑定一个模型扩展。当前产品的模型扩展使用已配置的 DeepSeek 凭据及工具接口，沿用 Session、Run、报价预占和用量结算。适配器提供实测 Token 数，Harness 以锁定价格核算。内置 `flash_compat` 演示复用现有 DeepSeek 传输。产品基线仍会发出启用思考及最高档推理配置。[注册契约](../../src/mgar_v3/harness/plugins.py) · [产品模型](../../src/mgar_v3/harness/product_models.py)

| 插件 | 显式授权与固定数据源 |
| --- | --- |
| `github` | 项目须有 GitHub 只读授权。仓库和完整 commit 来自任务绑定，只接受相对路径。配置了本地 MCP 时核验已有官方二进制锁。没有另设插件锁的旧部署沿用项目的 GitHub 只读授权。 |
| `knowledge` | 在锁中列出已获准的 `knowledge_snapshots` UUID，冻结操作者对每份快照的有效 ACL。可搜索快照并读取具体 chunk。 |
| `data` | 先准备现有 R4 独立只读数据源。用 `engine.tools.harness_data_identity(actor)` 获取画像指纹，写入锁的 `sql_profile_sha256`。源连接、查询规则和角色通过 R4 原有授权校验。 |

知识和数据 MCP 沿用 Engine 的任务 Bearer、调用额度和取证记录。配置原有 R4 MCP 地址时使用固定的 HTTP 端点，也可在同进程使用相同授权端口。GitHub 发布继续经过已有 Action 与 Outbox 审核流程。

## 工程验收命令

```bash
make verify TASK=HV4-H14
make verify TASK=HV4-H13
make lint
make typecheck
```

工程验收使用本机隔离 PostgreSQL、锁定的 Docker 镜像、受控模型、本地 HTTP MCP 和独立 stdio MCP。外部 GitHub 实站和付费模型的实站结果独立记录。
