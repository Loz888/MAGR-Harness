# HV4-H14 开发交接

下一位 Agent 的任务是完成 **HV4-H14 项目指令、Skills、MCP 与插件接口**。请先阅读根目录和 `v3/` 的 `AGENTS.md`、V3.1 队列与 H14 任务卡，再以卡片为验收依据继续开发。[H14 任务卡](TASKS.md#hv4-h14) · [V3.1 队列](../../../v31-upgrade/TASK_QUEUE.md) · [权威边界 ADR](../adr/0001-v31-boundaries.md)

## 从哪里开始

- 当前本地仓库为 `/home/imini/magr-harness-h13`，分支为 `hv4-h13`。公开主线起点为 `d703fc71f0feeaa4e45b997eecf4081d38f73d30`，H13 源码提交为 `fc2f7d24f1a8771df8ff9ef51508bd87b085ecc5`。同一工作区可以从 `hv4-h13` 建独立 H14 分支。H14 卡片的正式依赖是 H01、H04、H11。
- H12 正式质量门独立复核为 PASS。开发集与持出集均为 11/12。冻结 Worker 是 `33e39a4`，评分与产品发布状态可查公开验收。编码 MVP 产品状态为 `NOT_RELEASED`。[H12 正式矩阵](H12_R08_FORMAL_REAL_MODEL_20261118.md)
- H13 实现把 `mgar harness` 和 Python `HarnessClient` 接到 Web 的 `/api/v3/agent` 会话和 `/api/v3/harness` 任务接口。支持事件游标、暂停、恢复、取消、Diff、产物和 `mgar.harness.session.v1` 导出。H13 还补了 Runtime 到暂停点时的 PG 状态写入，沿用原 Run 和 epoch。工程验收实跑 38 项，全部通过。[H13 使用与兼容契约](HEADLESS.md) · [机器验收](accept/HV4-H13/accept.json)

## 已有实现与 H14 的缺口

| 范围 | 已有实现 | H14 需要完成 |
| --- | --- | --- |
| 项目与子目录指令 | `build_project_context` 从授权工作树快照发现根和子目录 `AGENTS.md`，按目标路径选择祖先指令。未指定目标时加载根指令并给出子目录索引。上下文按 JSON 引用资料传入，记录来源、哈希和字节边界。[实现](../../src/mgar_v3/harness/project_context.py) | 明确系统/操作者、当前用户、项目根和更深有效作用域之间的规则。验证运行中新增目标路径时如何获得适用指令，以及恢复时如何引用当时的版本。 |
| Skills | 项目的 `.agents/skills`、`.codex/skills` 包含受限 `SKILL.md` 元数据发现，用户显式用 `$name` 或路径调用时加载同作用域正文。[实现](../../src/mgar_v3/harness/project_context.py) | 固化来源、作用域、重名选择、失效及兼容示例。项目 Skill 正文与操作者安装的可信扩展有不同的授权路径。 |
| 扩展和 GitHub MCP | `TrustedExtension`、`ExtensionAccess`、`ProductExtensionRegistry` 已封装命名空间、版本、只读白名单、范围校验、超时、输出大小、取消和卸载。现有 GitHub 只读扩展通过 `HarnessProductService.extensions` 按项目授权挂载，固定仓库和 commit。调用前后均检查当前权限。[注册及 GitHub 扩展](../../src/mgar_v3/harness/extensions.py) · [产品接线](../../src/mgar_v3/harness/product.py) | 把现有组合点扩展为操作者管理的版本化注册及锁定格式，用受控扩展实例验证安装、启用、升级和卸载。继续使用共享 ToolRegistry 入口。 |
| 知识与数据 MCP | 现有 `mcp/r4.py` 通过角色绑定的 Bearer 调用 `Engine.tools`。`ToolAuthority` 校验任务、epoch、调用额度、知识快照和只读数据范围。GitHub 客户端工厂校验固定 MCP 二进制 SHA-256。[R4 MCP](../../src/mgar_v3/mcp/r4.py) · [ToolAuthority](../../src/mgar_v3/agents/tools.py) · [GitHub 工厂](../../src/mgar_v3/mcp/factory.py) · [已有锁](../../config/github-mcp-lock.json) | 将 GitHub、知识和数据适配到统一入口。先明确编码 Run 如何取得各自窄作用域授权，再生成面向模型的工具规格。保留原工具回执、对账和审计路径。 |
| 模型适配 | 现有 `ModelAdapter` 协议和模型能力、选型、计价逻辑已被 Harness 使用。产品目前通过 `resolve_model` 与 `product_models.py` 选择模型。[模型与能力](../../src/mgar_v3/harness/model.py) · [产品模型](../../src/mgar_v3/harness/product_models.py) · [端口契约](../../src/mgar_v3/harness/contracts.py) | 在组合点注册一个受控适配器并展示能力、认证和计价校验，让新增适配能复用 Session／Run／预算和输出协议。 |

## 实施顺序与验收

1. 先建立基线。用 Python 3.12 锁定环境和隔离的 PostgreSQL。运行 `make verify TASK=HV4-H13`。项目指令、现有扩展和 MCP 回归入口见下方命令。若修改共享 Engine、API 或 schema，先检查仓库交接记录和当前迁移编号。[共享边界交接](SUPER_AGENT_HANDOFF.md)
2. 在现有的项目快照和扩展组合点内冻结 H14 契约。注册至少要绑定扩展 ID、版本、工具或模型能力、来源/锁定哈希和运行限制。操作者管理安装、授权及生命周期。项目文本、模型和工具输出进入上下文时继续作为数据。
3. 接上 GitHub、知识及数据适配，提供扩展 SDK 和可重现的兼容样例。隔离执行由 Broker 管理。扩展按当前 grant、actor、项目/仓库、Run 和 epoch 执行。GitHub 的发布副作用继续通过已审核的 Action/Outbox 流程。未知结果先核账，权限撤销和卸载要取消在跑调用、收回工具并清理进程。
4. 用受控工具与模型验证扩展无需改动主循环即可生效。验收覆盖根/子目录冲突、Skill 注入、同名工具覆盖、过期 epoch、授权撤销、锁文件或依赖哈希漂移、生命周期收尾及副作用路由。注册实际的 `make verify TASK=HV4-H14`；保存测试、源码指纹和原始回执，工程与真实外部服务验收状态分别记录。

```bash
cd /home/imini/magr-harness-h13/v3
uv sync --frozen
make verify TASK=HV4-H13
uv run --frozen pytest -q tests/test_harness_product_context.py \
  tests/test_harness_product_extensions.py tests/test_harness_product_models.py \
  tests/test_mcp.py tests/test_r4_mcp.py
make lint
make typecheck
```

使用本机已验证的 R03 Docker 镜像和匹配的 `.local/r03/sandbox-lock.json`。在干净环境按项目文档准备镜像与 PostgreSQL。CLI 只需操作者令牌调用 API，集成测试另用隔离数据库。H13 的原始测试日志由验收脚本写入 `v3/evidence/HV4-H13/`。[环境说明](../../README.md) · [H06 命令契约](COMMAND_EXECUTION.md)

上述五个 H14 基线文件在 H13 源码上已实跑 **91 项通过**，零失败、错误和跳过。这组结果覆盖已有能力，H14 新增集成需按卡片单独验收。[基线记录](accept/HV4-H13/accept.json)
