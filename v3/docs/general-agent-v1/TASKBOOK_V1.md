# MAGR General Agent 下一阶段建设任务书
**版本：V1.0**  |  **日期：2026-11-19**  |  **项目：MAGR-Harness**

**基线参考：** `Loz888/MAGR-Harness`，此前评测快照 `6402afeb0257867be88edc1108e37e8e5cf16cc5`。执行 GA00 时必须重新读取当前 main 并冻结真实基线。

## 0. 任务书目标与执行规则

目标：保留现有 Harness Runtime、Coding Loop、Broker、Workspace、Memory、MCP、插件和持久化任务机制，增加通用任务认知层、跨领域执行、通用验证和统一评测。第一阶段定位为 **Digital General Agent**，优先覆盖 Coding、Web Research、Data Analysis、Office Automation。

本阶段不默认包含物理机器人、任意桌面 GUI 自动化、实时多模态感知、无限制网页操作或开放式第三方 Agent 市场；这些列入后续阶段。

### 执行协议
- 每次只执行一张任务卡，完成后停止等待批准。
- 先读取当前仓库工作协议、源码和任务卡；不能把任务书描述当成实现事实。
- 真实模型、真实 API、真实外部写入与 Mock/Fixture 结果必须分开记录。
- 外部写操作必须经过 ActionIntent、权限检查和适用的人工审批。
- 不删除或覆盖历史失败证据，不通过放宽测试获得 PASS。
- 每卡提交源码、测试、`accept.json`、独立复验入口并更新任务队列。
- 建议证据路径：`v3/docs/general-agent-v1/accept/GAxx/accept.json`；若仓库已有统一规范则沿用现有规范。

### 验收状态
| 状态 | 含义 |
|---|---|
| TODO / IN_PROGRESS | 未开始 / 正在实现 |
| IMPLEMENTED | 源码已实现，验收未完成 |
| ENGINEERING_PASS | 工程测试通过 |
| REAL_PROVIDER_PASS | 真实模型或服务调用通过 |
| EXTERNAL_E2E_PASS | 真实外部系统端到端通过 |
| INDEPENDENT_PASS | 独立复验通过 |
| BLOCKED / NOT_RUN | 阻塞 / 未执行，不得称为通过 |

## 1. 总体架构决策

```text
User / Event
  -> Intent & Goal Understanding
  -> General Planner / TaskGraph
  -> Capability Registry / Domain Router
  -> Coding | Research | Data | Office Executors
  -> Universal Agent Runtime (RunState / Context / Memory / Budget / Scheduler)
  -> Capability & Action Broker (Grant / Lease / Epoch / Idempotency / Approval)
  -> MCP / Native Tools / Connectors
  -> External Systems / Workspace
  -> Evidence / Receipt / Verifier
  -> Result & Artifact Delivery
```

核心原则：Planner 决定目标和分解；Router 选择能力；Executor 负责领域执行；Broker 是副作用授权边界；Verifier 根据外部证据判断结果；Memory 只能提供参考，不能授予权限。

### 复用现有能力
- Model / Provider Adapter；CodingLoop 与五种 Coding Task Policy。
- Workspace、Incremental Edit、Broker、Grant、Lease、Epoch、Sandbox。
- Test、Readiness、Verifier、Evidence、Receipt、Artifact/CAS。
- PostgreSQL 权威状态、Redis 缓存、ContextBuilder、LongContextManager。
- MemoryStore、MemoryWriter、MemoryRetriever、MCP Client、Skills、Plugin Lock。
- Scheduler、持久任务、恢复机制和 Outbox（以当前源码为准）。

### 新增统一抽象
`GeneralTaskSpec`、`OutcomeContract`、`TaskGraph`、`CapabilityDescriptor`、`DomainExecutor`、`ActionContract`、`VerificationPlan`、`KnowledgeSourceDescriptor`、`GeneralRunState`、`GeneralBenchmarkCase`。

## 2. 任务总览

| ID | 任务 | 优先级 | 依赖 |
|---|---|---|---|
| GA00 | 当前版本冻结与架构盘点 | P0 | 无 |
| GA01 | GeneralTaskSpec 与 Outcome Contract | P0 | GA00 |
| GA02 | General Planner 与动态 TaskGraph | P0 | GA01 |
| GA03 | Capability Registry 与 Domain Router | P0 | GA01 |
| GA04 | Universal ActionContract 与副作用治理 | P0 | GA01、GA03 |
| GA05 | 通用 VerificationPlan / Evidence Contract | P0 | GA01、GA04 |
| GA06 | Web Research Executor | P0 | GA02、GA03、GA05 |
| GA07 | Data Analysis Executor | P1 | GA02、GA03、GA04、GA05 |
| GA08 | Office Automation Executor | P1 | GA03、GA04、GA05 |
| GA09 | Coding Executor 统一适配 | P1 | GA01、GA03、GA05 |
| GA10 | Personal / Cross-domain Memory | P1 | GA01、GA05 |
| GA11 | Cross-domain Context & Knowledge Gateway | P1 | GA03、GA05、GA10 |
| GA12 | General Agent Benchmark Suite | P0 | GA01–GA09 |
| GA13 | General Agent RC 集成验收 | P0 | GA00–GA12 |

## 3. 详细任务卡

### GA00 — 当前版本冻结与架构盘点
**优先级：P0**  |  **依赖：无**

**目标**
建立真实工程基线，确认当前实现、调用链、测试和未完成能力。

**工作内容**
- 读取当前 main、版本、Commit、工作树状态；旧评测快照只作参考。
- 盘点 Web/API、CLI、SDK、CodingLoop、Verifier、Broker、Sandbox、Memory、MCP、Plugins、Scheduler、Runner、Outbox。
- 逐项标记：源码存在、调用链接线、工程测试通过、真实 Provider 通过、外部 E2E 通过、独立复验通过。
- 运行完整测试基线，保留失败；生成架构差距矩阵。

**验收标准**
- 当前 HEAD、版本、工作树可复现。
- 关键能力附源码路径与调用链。
- 测试命令、退出码、报告齐全。
- 不修改产品行为，不覆盖历史失败。

**禁止事项 / 边界**
- 不顺手修 Bug；不把 README 描述当作实现证据；不继承旧 Commit 的验收结论。

### GA01 — GeneralTaskSpec 与 Outcome Contract
**优先级：P0**  |  **依赖：GA00**

**目标**
建立领域无关任务协议，让 Coding、Research、Data、Office 共用任务入口。

**工作内容**
- 定义 goal、context、constraints、deliverables、success_criteria、risk_level、allowed_capabilities、budget、deadline、approval_policy、source_refs。
- 定义 draft/ready/running/waiting/blocked/verifying/completed/failed/cancelled 状态。
- 用户修改目标时产生 task revision，旧计划不得覆盖新版本。
- 定义 OutcomeContract：成功必须绑定可核验交付物和证据。
- 为现有五种 Coding Task Policy 建兼容 Adapter。
- 模糊目标进入 clarification_required，不得臆造关键约束。

**验收标准**
- 同一协议能表达四个核心领域。
- 目标、计划、执行状态、最终结果彼此区分。
- 目标修订有版本保护。
- 现有 Coding 回归不下降。
- Schema 有版本和负向测试。

**禁止事项 / 边界**
- Planner 输出不构成权限；不破坏现有 Coding API 语义。

### GA02 — General Planner 与动态 TaskGraph
**优先级：P0**  |  **依赖：GA01**

**目标**
将 Coding 专用规划升级为跨领域动态任务规划。

**工作内容**
- 从 GeneralTaskSpec 生成 TaskGraph。
- 支持 DAG 依赖、并行组、条件分支、等待事件、审批节点。
- 节点声明输入、输出、能力需求、预算、超时、重试和完成条件。
- 新证据、失败、用户新指令或外部事件触发 Replan。
- 通过 Graph Revision/CAS 防止旧计划覆盖新计划。
- 复用 CodingLoop，不复制 Coding 专用执行状态机。

**验收标准**
- 跨领域研究→数据→报告任务生成正确依赖。
- 前置失败时下游不启动。
- 独立节点可并行且受预算/并发约束。
- Replan 只影响必要节点。
- 非幂等外部动作不会重复执行。
- 包含真实模型规划评测和故障注入。

**禁止事项 / 边界**
- Planner 不得直接执行工具或扩大权限。

### GA03 — Capability Registry 与 Domain Router
**优先级：P0**  |  **依赖：GA01**

**目标**
统一登记 Native Tool、MCP Tool 和 Domain Executor，并按任务选择能力。

**工作内容**
- CapabilityDescriptor 包含 ID/版本、领域、描述、Schema、Provider、权限、风险、副作用、幂等、成本、超时、健康和 Evidence Contract。
- 实现静态注册、受控动态发现、按任务筛选。
- Router 根据 Goal、TaskGraph 节点和 Descriptor 选择能力。
- 模型调用前按 Schema、权限和上下文预算过滤工具。
- 能力不可用时支持替代或明确降级；记录路由理由。
- 远程 MCP Server 必须可信、认证、授权并版本锁定。

**验收标准**
- Coding/Research/Data/Office 可在同一 Registry 注册。
- 未授权能力不会因发现而自动可用。
- 模型只看到当前任务允许且相关的工具。
- 路由理由可审计。
- 服务不可用、超时、版本不兼容有明确处理。

**禁止事项 / 边界**
- MCP Discovery 不等于授权；不把所有工具 Schema 暴露给每次调用。

### GA04 — Universal ActionContract 与副作用治理
**优先级：P0**  |  **依赖：GA01、GA03**

**目标**
统一治理文件修改、邮件发送、日历创建、网页提交、PR 发布等副作用。

**工作内容**
- 定义 ActionIntent：Action ID、Task Revision、Capability、Actor、Target、Operation、Expected Effect、Idempotency Key、Risk、Approval、Preconditions、Compensation。
- 区分 read、local_write、external_write、publish、delete、financial 等等级。
- 所有外部写操作先创建 ActionIntent，再经 Broker 检查 Grant、Lease、Epoch、预算、资源范围和 Approval。
- 支持幂等键；超时/断连结果未知时先对账，不盲目重试。
- 通过 Outbox/Dispatcher 执行获批动作并保存外部回执。

**验收标准**
- 未批准的外部写操作不会执行。
- 支持幂等的服务不会因重复投递产生重复副作用。
- 未知结果进入对账流程。
- 权限撤销后未执行动作不能继续。
- 外部回执可追溯。
- 真实测试只使用专用账户。

**禁止事项 / 边界**
- MCP 不得绕过 Broker；不可补偿动作提高审批等级。

### GA05 — 通用 VerificationPlan / Evidence Contract
**优先级：P0**  |  **依赖：GA01、GA04**

**目标**
将 Coding Verifier 思想扩展到所有数字任务，不能以模型自述判定成功。

**工作内容**
- 定义 expected_outputs、required_evidence、verification_steps、source/version、freshness、consistency、independent_check、failure_conditions。
- Coding 验证测试/Diff/Build；Research 验证来源与引用；Data 验证 Schema/公式/样本数；Email/Calendar 验证外部回执；Browser 验证最终页面状态。
- 证据缺失时标记 unverified 或 blocked。
- 结果绑定 Evidence 版本；人工确认绑定结果摘要。

**验收标准**
- 模型说完成不能直接将任务设为 completed。
- 每个领域有最小 Evidence Contract。
- 证据、来源、版本和回执可追溯。
- 伪造成功回执的负向测试通过。
- 关键路径执行器与验证器职责分离。

**禁止事项 / 边界**
- 不以单一 LLM Judge 取代机械验证。

### GA06 — Web Research Executor
**优先级：P0**  |  **依赖：GA02、GA03、GA05**

**目标**
支持多来源公开网络研究、证据收集、交叉核验和报告交付。

**工作内容**
- 定义 Search/Open/Extract/FollowLink/Snapshot 工具契约。
- 多来源检索、去重、来源可信度和发布时间记录。
- 每条结论保存 URI、访问时间、标题、发布日期/版本、引用片段和内容 Hash。
- 研究计划包括问题拆分、来源发现、冲突核验和综合写作。
- 区分事实、推断、观点和未确认信息；生成 Markdown/PDF。

**验收标准**
- 技术调研、公司/行业调研、对比分析至少三类任务通过。
- 关键事实有可访问来源。
- 过期资料有日期提示，冲突来源不强行合并。
- 结论可追溯到 Evidence。
- Mock 与真实 Web 服务测试分开记录。

**禁止事项 / 边界**
- 不得编造来源、发布日期或引用支持关系。

### GA07 — Data Analysis Executor
**优先级：P1**  |  **依赖：GA02、GA03、GA04、GA05**

**目标**
支持数据读取、分析、可视化和文件交付。

**工作内容**
- 支持 CSV/XLSX/JSON；文件识别、Schema、缺失值和异常值概览。
- 提供受限 Python/SQL 执行环境。
- 记录输入 Hash、代码版本、参数和随机种子。
- 生成 XLSX、CSV、图表和报告。
- 执行环境限制 CPU、内存、时间、文件系统和网络；禁止不可信宏和未授权写库。

**验收标准**
- 描述性统计、分组分析、趋势分析通过。
- 输入与输出可追溯。
- 样本量、过滤条件、公式、缺失值处理可解释。
- 固定参数下核心数值可复现。
- 越权文件/网络访问被拒绝。
- 输出文件可打开并通过结构检查。

**禁止事项 / 边界**
- 不执行文件中的宏或不可信代码。

### GA08 — Office Automation Executor
**优先级：P1**  |  **依赖：GA03、GA04、GA05**

**目标**
在授权范围内操作邮件、日历、文档和表格。

**工作内容**
- 优先 OAuth + 官方 API/可信 MCP Connector。
- 首批支持邮件、日历、文档、表格。
- read/draft/send/create_event/update_event/share/delete 分开授权。
- 外部写操作按策略审批，默认 Draft-first。
- 保存 Message ID、Event ID、Document ID；处理时区、重复事件、收件人和权限变化。
- 凭据不得进入 Memory 或日志。

**验收标准**
- 读取不触发写操作。
- 草稿与发送分离。
- 未确认不能发送需审批邮件。
- 日历时区、参与者和时间正确。
- 外部结果可对账。
- 专用测试账户真实 E2E 通过。

**禁止事项 / 边界**
- 未经授权不得发送、分享、删除或覆盖。

### GA09 — Coding Executor 统一适配
**优先级：P1**  |  **依赖：GA01、GA03、GA05**

**目标**
把现有 Coding Agent 作为第一个 Domain Executor，避免复制第二套 Coding Runtime。

**工作内容**
- GeneralTaskSpec 转换为现有 Coding TaskSpec。
- 保留 inspect/bugfix/feature/refactor/run_app。
- 将 CodingResult、Receipt、Diff、Artifact 映射到通用 OutcomeContract。
- 复用 Workspace、Broker、Lease、Epoch、Sandbox 和预算。
- 支持 TaskGraph 中 Coding 节点暂停、恢复和失败传播。
- 保持旧 Coding API 兼容。

**验收标准**
- Planner 可生成 Coding 节点并交给现有 CodingLoop。
- 不复制第二套 CodingLoop。
- 现有 H12 回归不下降。
- Coding 结果可被通用 Verifier 消费。
- 失败正确传播到 TaskGraph。

**禁止事项 / 边界**
- 不重写现有 Coding Runtime。

### GA10 — Personal / Cross-domain Memory
**优先级：P1**  |  **依赖：GA01、GA05**

**目标**
增加用户级跨项目记忆，并区分偏好、项目事实和执行经历。

**工作内容**
- 定义 user_id/tenant_id/project_id 隔离。
- 用户级记忆有来源、创建时间、有效性和用户控制策略。
- 支持查看、修正、撤销和删除可删除的个人记忆。
- 敏感信息和凭据不得写入；冲突记忆版本化。
- 跨项目召回显式检查授权；外部 RAG 不自动变成个人事实。
- 建立记忆质量评测。

**验收标准**
- 用户间隔离通过。
- 项目记忆不自动升级为全局偏好。
- 撤销后正常检索不可返回。
- 冲突可追溯。
- 召回标注来源和可信状态。
- 兼容现有 Memory 类型。

**禁止事项 / 边界**
- 不得默认扩大跨项目可见范围。

### GA11 — Cross-domain Context & Knowledge Gateway
**优先级：P1**  |  **依赖：GA03、GA05、GA10**

**目标**
通过统一接口接入独立领域知识库，不在 Harness 固定某一种 RAG。

**工作内容**
- KnowledgeSourceDescriptor 包含 source_id、domain、version、ACL、Search/Read 工具、Citation Schema、Freshness、Trust。
- 通过 MCP 或 Native Adapter 接入知识服务。
- 按任务选择一个或多个来源；统一 SearchResult/Evidence/Citation。
- 支持 Search→Retrieve→Read→Cite。
- 知识内容作为不可信数据；各知识库保留独立 Embedding、Chunking、ACL 和 Reranker。
- 服务不可用时明确降级。

**验收标准**
- 至少接入一个技术文档 RAG 和一个项目知识源。
- 两个来源可使用不同 Embedding。
- ACL 在检索前生效。
- 结果包含 URI、版本、时间和定位。
- 服务失效有可解释错误。
- 新增知识源不修改 Planner 核心逻辑。

**禁止事项 / 边界**
- 不得把检索内容升级为系统指令或权限。

### GA12 — General Agent Benchmark Suite
**优先级：P0**  |  **依赖：GA01–GA09**

**目标**
建立跨领域评测，不能用 Coding Benchmark 推断 General Agent 能力。

**工作内容**
- 冻结 Coding、Research、Data、Office、Autonomy、Cross-domain 六类任务集。
- 每题定义初始状态、授权、工具环境、期望结果和独立评分。
- 记录 Provider、模型、调用数、Token、费用、延迟和失败原因。
- 记录 Tool Selection、Plan Quality、Evidence Quality、Task Success、Policy Violation。
- Mock、真实 Provider、真实外部系统分开。
- 建立无 Planner/无 Memory/无 Reranker/单领域/多领域消融。
- 保留失败原始日志和可复现环境。

**验收标准**
- 每个领域至少 10 个开发用例和独立持出用例。
- 六类任务分别报告。
- 权限违规和外部副作用错误单独统计。
- 真实与 Mock 分开。
- 开发集调参后不得冒充独立持出集。

**禁止事项 / 边界**
- 不得只报告一个综合平均分。

### GA13 — General Agent RC 集成验收
**优先级：P0**  |  **依赖：GA00–GA12**

**目标**
将各能力集成为可受控试用的 Digital General Agent Release Candidate。

**工作内容**
- 冻结 RC Commit 和依赖锁；完成迁移、升级、备份和恢复演练。
- 检查 Web/CLI/SDK 一致性。
- 完成 Coding、Research、Data、Office 和跨领域 E2E。
- 完成权限撤销、预算耗尽、超时、外部结果未知、Worker 崩溃故障注入。
- 完成专用测试账户真实 E2E 和独立持出评测。
- 输出限制、已知问题、资源需求、部署步骤和回滚方案。

**验收标准**
- P0 卡片全部 INDEPENDENT_PASS。
- 四个核心领域有真实 Provider 或服务 E2E 证据。
- 无未处理高危权限违规。
- 外部副作用有 ActionIntent、Approval（适用时）、Receipt、Reconciliation。
- 关键恢复场景通过。
- 达到冻结的真实任务集门槛。
- TODO/NOT_RUN 明确披露。

**禁止事项 / 边界**
- RC Ready 不等于企业生产级认证。

## 4. 后续阶段（不纳入第一版 RC）

### GA-Future-01：Computer Use / Desktop
- Screenshot、Accessibility Tree、OCR 与 UI Grounding。
- 点击、输入、滚动、窗口切换。
- Observe → Act → Observe → Verify。
- 桌面会话隔离和用户可见审批。

### GA-Future-02：Multimodal Input
- 图片理解与 PDF 页面视觉理解。
- 音频转写、语音输入。
- 视频关键帧和时间戳。
- 多模态 Evidence 与来源定位。

### GA-Future-03：Proactive / Event-driven Agent
- 定时、Webhook、邮件和日历事件。
- 订阅、静默、唤醒和长期目标追踪。
- 用户可控主动通知、防重复触发和事件对账。

### GA-Future-04：Agent Ecosystem
- 第三方 Agent 注册和 Capability Manifest。
- Agent-to-Agent delegation。
- 跨 Agent 身份、预算、权限、审计。
- 沙箱化 Agent Provider 和质量评级。

## 5. 跨任务通用架构约束
- 一个权威状态源：LLM 历史、Redis 缓存和外部消息不能同时成为任务事实源。
- Plan 不等于权限：Planner 不能自行扩大 Capability Grant。
- Memory 不等于事实：必须有来源、版本和可信状态。
- Tool Result 不等于成功：必须由 VerificationPlan 核验。
- 外部写操作必须可对账；未知结果不能盲目重试。
- 每个领域有自己的验证器，不能把 pytest 机械套用到 Email 或 Research。
- Planner、Executor、Verifier 不绑定单一模型供应商。
- 工具按需暴露；每个执行器有 CPU、内存、Token、时间和并发预算。
- 失败必须可恢复、可解释，回执和人工介入必须持久化。

## 6. 第一阶段最终交付定义

| 场景 | 用户任务示例 | 必要证据 |
|---|---|---|
| Coding | 修复仓库登录 Bug，运行回归并展示 Diff | 测试、Diff、Verifier Receipt |
| Research | 调研 Agent Infra 技术路线并生成报告 | 可访问来源、日期、引用核验 |
| Data | 分析 CSV 销售趋势并生成 Excel/图表 | 输入 Hash、公式、样本数、文件检查 |
| Office | 根据会议材料生成纪要，先创建邮件草稿 | 草稿 ID；未经批准不发送 |
| Cross-domain | 调研公司、整理数据并生成 Excel 和报告 | TaskGraph、跨源 Evidence、一致性检查 |

## 7. 推荐执行顺序

第一轮只批准 GA00。完成并独立复验后批准 GA01；随后 GA02、GA03、GA04、GA05、GA06、GA09、GA07、GA08、GA10、GA11、GA12、GA13。每张卡完成后停止，不自动推进下一张。

**最终目标：建立一个能够理解目标、组合能力、可靠执行、核验结果并长期积累经验的通用数字智能体，而不是一个仅仅拥有大量工具的聊天机器人。**