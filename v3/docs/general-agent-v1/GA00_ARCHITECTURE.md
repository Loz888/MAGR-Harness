# GA00 架构盘点与差距矩阵

核对对象为 GitHub `Loz888/MAGR-Harness` 的 `main`，源码 `6402afeb0257867be88edc1108e37e8e5cf16cc5`。下表的工程状态来自本轮注册测试。每行的测试文件另列在机器记录里，各行可复用同一项测试。真实 Provider、专用外部账户 E2E 及独立复验在 GA00 视图均为 `NOT_RUN`。本机实际 PostgreSQL、Redis、Docker 和 HTTP 属工程验收。

| 能力 | 源码 | 接线 | 工程检查 | 通过／失败／错误／跳过 | 真实 Provider | 外部 E2E | 独立复验 |
| --- | --- | --- | --- | ---: | --- | --- | --- |
| Web 与 FastAPI | 有 | 已接入 | PASS | 38／0／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| CLI 与 Python SDK | 有 | 已接入 | PASS | 12／0／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| CodingLoop 与任务策略 | 有 | 已接入 | PASS | 80／0／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| 审批、完成条件及证据 | 有 | 已接入 | PASS | 65／0／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| Broker、权限与沙箱 | 有 | 已接入 | FAIL | 84／1／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| 工作区与增量编辑 | 有 | 已接入 | PASS | 34／0／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| PostgreSQL、配额与恢复 | 有 | 已接入 | PASS | 57／0／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| 同一会话上下文 | 有 | 已接入 | PASS | 56／0／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| 仓库级持久记忆 | 有 | 已接入 | FAIL | 44／2／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| 模型适配与计费 | 有 | 已接入 | PASS | 42／0／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| MCP 与固定知识源 | 有 | 已接入 | PASS | 50／0／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| 项目规则及插件 | 有 | 已接入 | PASS | 17／0／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| 多用户队列与调度 | 有 | 已接入 | PASS | 11／0／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| 配对 Runner | 有 | 已接入 | PASS | 16／0／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| 动作、Outbox 与 GitHub 交付 | 有 | 已接入 | PASS | 45／0／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| 仓库分析与领域路由前身 | 有 | 已接入 | PASS | 31／0／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| 可选仓库专家 | 有 | 已接入 | PASS | 18／0／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |
| 运行诊断与恢复 | 有 | 已接入 | FAIL | 9／2／0／0 | NOT_RUN | NOT_RUN | NOT_RUN |

## 当前任务执行主线

1. **任务入口**。网页从统一项目页发起会话。FastAPI 先验证用户和项目授权，再调用 `HarnessProductService.create/send`。产品端推断任务类型、检查项目准备和授权，并建立 `CodingTaskRequest`；受管理任务入队，其他任务经现有本地工作线程。Headless CLI 经 `HarnessClient` 访问相同会话接口，也可读取事件、导出和控制任务。[网页请求](../../web/agent-product.js#L6) · [Web/API](../../src/mgar_v3/api.py#L556) · [产品路由](../../src/mgar_v3/harness/product_api.py#L185) · [任务构建](../../src/mgar_v3/harness/product.py#L815) · [CLI](../../src/mgar_v3/harness/headless.py#L123) · [SDK](../../src/mgar_v3/harness/client.py#L166)
2. **计划、执行与证据**。`HarnessCodingService._run` 将工具注册表、Workspace、Broker、LongContextManager、持久预算和 LocalVerifier 装配进同一 CodingLoop。五种任务策略分别处理 `inspect`、`bugfix`、`feature`、`refactor` 与 `run_app`。任务策略及验收清单决定代码读取、编辑、测试、构建和服务检查。证据绑定任务、授权与当前工作树，最终验收读取相应回执和 CAS。H12 评分器另有封存候选及独立 Judge 的路径。[执行装配](../../src/mgar_v3/harness/service.py#L3676) · [五种策略](../../src/mgar_v3/harness/coding_loop.py#L174) · [验收](../../src/mgar_v3/harness/coding_loop.py#L3016) · [LocalVerifier](../../src/mgar_v3/harness/adapters.py#L561) · [H12 Judge 调度](../../src/mgar_v3/quality_eval/h12_runner.py#L1190)
3. **权限、运行和恢复**。WorkspaceRegistry 从登记来源创建任务工作树。CapabilityAuthority 在执行前约束 actor、项目、路径、lease 和 epoch。Docker Broker 运行固定命令和受限网络，并保留生成日志出处。Engine 通过 PostgreSQL 持久状态和锁管理 Run，HarnessStore 和 RecoveryIO 保存工作树、请求与回执。已知结果可按原操作复用，未知用量或远端写入需要对账。Redis 用于缓存和依赖探针，GA00 按 PostgreSQL 中的状态判定任务事实。[Workspace](../../src/mgar_v3/harness/workspace.py#L712) · [授权](../../src/mgar_v3/harness/broker.py#L320) · [Docker Broker](../../src/mgar_v3/harness/broker.py#L494) · [Engine](../../src/mgar_v3/coordination/engine.py#L45) · [取回 Run](../../src/mgar_v3/harness/store.py#L619) · [持久模型](../../src/mgar_v3/harness/persistence.py#L158)
4. **调度和设备边界**。API 生命周期装配 HarnessScheduler，supervisor 定期领取受管理任务并巡检 Runner。Runner 路径另有设备配对、工作区绑定、固定计划、领取和回执，配对设备端通过本机 Broker 执行。生产多机拓扑属于后续验证范围。[生命周期](../../src/mgar_v3/api.py#L190) · [调度循环](../../src/mgar_v3/api.py#L251) · [Scheduler](../../src/mgar_v3/harness/scheduler.py#L15) · [Runner 领取](../../src/mgar_v3/harness/runner_store.py#L376) · [设备执行](../../src/mgar_v3/harness/runner_device.py#L615)
5. **模型与扩展**。产品端解析当前绑定的模型与预算，DeepSeek 适配器按已固定配置发出请求，DurableModel 将操作与原用量账本关联。ProductExtensionRegistry 从 PluginCatalog 的版本和工作区范围装配受信扩展，对派发时的 lease 与工具范围再次核验。GitHub 有只读 MCP 适配，知识和结构化数据有独立 MCP 角色。[模型解析](../../src/mgar_v3/harness/product.py#L342) · [DeepSeek](../../src/mgar_v3/harness/product_models.py#L114) · [扩展](../../src/mgar_v3/harness/extensions.py#L88) · [插件选择](../../src/mgar_v3/harness/plugins.py#L241) · [项目规则和 Skill](../../src/mgar_v3/harness/project_guidance.py#L92) · [GitHub MCP](../../src/mgar_v3/mcp/factory.py#L26) · [R4 MCP](../../src/mgar_v3/mcp/r4.py#L16)
6. **上下文与记忆**。CodingLoop 通过 LongContextManager 和 ContextBuilder 组装、压缩和持久化单 Run 上下文，后续轮次从产品会话、证据引用和 inbox 接续。RepoSage 有按 repository 定界的 AnalysisMemory，使用 MemoryStore、MemoryRetriever 与 MemoryWriter；Workbench 的修复及交付路径还具有 PersistentMemoryLoop。GA10 需要的用户级跨项目生命周期、可撤销个人偏好和跨租户召回授权尚需统一协议。[上下文](../../src/mgar_v3/harness/long_context.py#L298) · [ContextBuilder](../../src/mgar_v3/context/builder.py#L436) · [仓库记忆](../../src/mgar_v3/repository_analysis/memory_loop.py#L110) · [持久记忆](../../src/mgar_v3/memory/loop.py#L212)
7. **副作用与交付**。Engine 的动作提案经过目标、权限、内容哈希与当前 Run 检查，获批后由 Dispatcher 领取。调用在事务外执行，失败或未知状态由原账本结算和对账。Harness 的 GitHub 交付另有绑定任务补丁及发布步骤的账本与 PublicationDispatcher。Workspace 文件修改则走 Broker／工作区编辑和单独回写许可。GA04 的邮件、日历、分享、删除等副作用需要统一 ActionContract 接口。[动作验证与审批](../../src/mgar_v3/coordination/engine.py#L1058) · [Dispatcher](../../src/mgar_v3/effects/dispatcher.py#L22) · [GitHub 审批](../../src/mgar_v3/harness/github_delivery.py#L394) · [发布账本](../../src/mgar_v3/harness/github_ledger.py#L135) · [本地回写](../../src/mgar_v3/harness/product.py#L946)

8. **本机运维**。CLI 用 `mgar doctor` 运行诊断，用 `mgar ops` 备份、恢复和导出指标。恢复先验证快照和目标，使用持久收据区分数据库导入与目录发布，允许发布中断后用原目标重试。完整工程测试单列 PostgreSQL 迁移、Redis 缓存、运维和恢复断言。[运维 CLI](../../src/mgar_v3/ops/cli.py#L31) · [恢复操作](../../src/mgar_v3/ops/snapshot.py#L279)

## GA01–GA13 复用与差距

| 领域或契约 | 已有可复用能力 | 从当前调用链确定的缺口 | 对应卡 |
| --- | --- | --- | --- |
| 统一任务与结果 | 产品会话、CodingTaskRequest、TaskBinding、CodingResult、RepoSage AnalysisSpec | 跨领域目标、预算、交付物与成功证据共用的 GeneralTaskSpec 和 OutcomeContract | GA01、GA09 |
| 动态规划 | CodingLoop TaskPlan、验收清单、RunGraph 轨迹、后续输入和 checkpoint 安全点 | 通用 DAG、跨 Executor 依赖、条件节点、Graph Revision/CAS 及局部 Replan | GA02 |
| 能力登记与路由 | 项目插件锁、工具范围投影、产品 intent 推断、Engine 既有 Router | Native、MCP 与各领域 Executor 共用带风险和版本的 CapabilityDescriptor，节点级动态筛选 | GA03 |
| 副作用治理 | CapabilityLease、Engine Approval/Outbox、GitHub PublicationDispatcher 和未知回执对账 | 通用 ActionIntent、作用域化补偿、邮件/日历/分享/删除风险和审批策略 | GA04 |
| 统一验证与证据 | 当前树验收绑定、独立 Coding Judge、CAS、RepoSage 来源和引用 | 数据结构及公式、邮件／日历回执、网页结果与时效定义的通用 VerificationPlan | GA05 |
| 网页研究 | RepoSage 固定 GitHub 提交调查、R4 知识检索和引用 | 通用公开 Web Search/Open/Snapshot、多源冲突核验、时效和报告交付 | GA06 |
| 数据分析 | R4 受限只读 SQL、结构化业务分析、Docker 执行 | CSV/XLSX/JSON 文件入口、参数和随机种子、图表及文件结构验收 | GA07 |
| 办公自动化 | 现有审批、读写权限和回执框架 | 邮件、日历、文档、表格 Executor 及受控真实账户闭环，草稿和发送分别授权 | GA08 |
| 编码执行 | 五种 CodingLoop 策略、权限、工作区、验证器及恢复，SDK 已调用同一入口 | 通用任务及 TaskGraph 节点适配，保留旧 API 和 H12 评测口径 | GA09 |
| 用户记忆与知识 | RepoSage 仓库级记忆、Workflow 持久经验、R4 本地知识和 SQL MCP | 用户级记忆授权与生命周期、项目间召回、跨域 KnowledgeSourceDescriptor 和 ACL 投影 | GA10、GA11 |
| 通用评测与 RC | H12 的编码独立持出及 H13–H18 分项工程验收 | 六类冻结任务、通用验收矩阵、按域真实 Provider／外部系统验收及独立 RC 结论 | GA12、GA13 |

分项检查对本地实现和接线负责。先前 H12 开发与持出各 11/12、H17 单 Agent 对照及 H18 恢复受阻后通过的结论保留在各自冻结快照。GA00 未将它们记为当前源码的真实 Provider、外部 E2E 或独立通过。[H12 成绩](../harness-v4/H12_R08_FORMAL_REAL_MODEL_20261118.md) · [H17 独立记录](../harness-v4/H17_INDEPENDENT_ACCEPTANCE_20261119.md) · [H18 独立记录](../harness-v4/H18_PUBLISH_RETRY_INDEPENDENT_20261119.md)
