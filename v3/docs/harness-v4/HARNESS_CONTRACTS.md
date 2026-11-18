# Harness V4 接口契约 v1.1（HV4-H01）

- 契约版本：`harness-contract-v1.1`，状态 **FROZEN**
- 机器可读清单：`experiments/harness-v4/harness-contract-v1.json`
- 代码位置：`src/mgar_v3/harness/`
- 选型 ADR：[HV4-H01-deepseek-harness.md](adr/HV4-H01-deepseek-harness.md)
- 版本说明：v1.1 修复了 v1（`e1b3214`）评审中的默认放行缺口，并把事件映射到
  `app.run_events` 的真实列；v1 的失败探针结果保留在
  `super-agent-v1/SAV1-00/20261110T131512Z-h01-review/result.json`。

本契约只定义组合层端口。Thread、Run、`execution_epoch`、预算、Finding、Approval、
Action/Outbox 与 Review Bundle 继续由现有 Engine/PostgreSQL 持有。

## 1. 接口与冻结状态

### 1.1 冻结接口

| 接口 | 方法 | 职责 | 执行边界 |
| --- | --- | --- | --- |
| `TaskBinding` | — | 绑定 task/thread/run/epoch、actor、repository、base commit、workspace、acceptance revision 与目标 | 只读快照 |
| `CapabilityGrant` | `can_read` / `can_write` / `can_execute` | 工作区/路径/命令/网络/时限授权 | runtime 预检 + Broker 复核 |
| `ModelAdapter` | `complete` | 一次模型调用，返回文字、工具调用、finish reason、usage | 调用前预占，调用后结算 |
| `ToolRegistry` | `specs` / `invoke` | 声明并执行内建或受控扩展工具 | runtime 只调度已声明名称；未声明默认拒绝 |
| `WorkspaceProvider` | `snapshot` / `read` / `list_dir` / `apply` / `diff` | 版本化文件工作区 | 编辑需 pre-image/tree 校验 |
| `ExecutionBroker` | `execute` | 一次性命令/测试执行 | 校验 grant、cwd、argv、timeout |
| `ContextManager` | `build` | 生成上下文与 revision | 不授予权限 |
| `Verifier` | `verify` | 根据执行回执判定验收项 | 只认可匹配 tree 的回执 |
| `EventSink` | `emit` / `events` | 持久事件信封与游标 | 事件不替代业务状态 |
| `BudgetLedger` | `reserve` / `settle` / `mark_unknown` / `release` / `snapshot` | 预占优先的预算账本 | 模型/工具调用前强制 |

工具默认拒绝规则（v1.1）：

- 模型只能调用 `ToolRegistry.specs()` 当次声明的名称；未声明名称在 handler 之前
  被 runtime 拒绝，返回 `denied`。
- `read` 工具必须提供 `path`（或 `edits[].path`），每个路径都要通过
  `CapabilityGrant.can_read`。
- `write` 工具必须提供 `path`（或 `edits[].path`），每个路径都要通过
  `CapabilityGrant.can_write`；没有可识别路径时默认拒绝。
- `execute` 工具必须提供 `argv`，并通过 `CapabilityGrant.can_execute`。
- 通用扩展工具在 H14 之前必须使用上述能力声明；不能仅凭工具名信任 handler。

### 1.2 建议接口（尚未冻结）

| 能力 | 计划卡 | H01 状态 |
| --- | --- | --- |
| 流式模型 delta 与分片 JSON | H02 | 未实现 |
| 命令 Job 的 run/poll/cancel | H06 | 未实现；H01 只有一次性执行 |
| 持久 inbox、ask_user、安全点重计划 | H09 | 未实现 |
| 上下文压缩与分页工具 | H10 | 未实现 |
| 崩溃恢复、Worker fencing | H11 | 未实现 |
| 远程 Runner、多用户隔离 | H15–H16 | 未实现 |
| 插件/MCP 扩展接口 | H14 | 未实现 |

### 1.3 H02 模型端口加法扩展

H02 在 `harness-contract-v1.1` 上新增 `harness-model-v1`，不移除 H01 已接收字段：

- `StreamingModelAdapter.stream()` 产出 `ModelStreamEvent`：文字增量、工具调用增量、
  usage、finish。
- `ModelUsage.known`：缺 usage 时为 `false`，runtime 保留预占并返回 `unknown`，
  不按 0 费用结算。
- `ModelResponse` 新增 `provider`、`model_id`、`price_version`、`error_code`、
  `cached`。
- 同一请求冻结 provider/model/price；已知 429/5xx 同供应商有界重试一次，缺 usage/
  流中断/工具参数不完整不重发、不切供应商。
- HTTP 200 已收到后的读取超时、畸形 SSE 或生成后取消：不重复 POST，保留预占并
  进入 `unknown` 以便对账；只有请求发出前的取消才释放预占。
- 多轮工具历史保留协议原生形状：DeepSeek `tool_calls`/`tool_call_id`，
  Anthropic `tool_use`/`tool_result` block。
- SSE 解码跨 chunk 处理 CRLF 边界；`data: ...\r\n\r\n` 按单字节分片仍得到 1 个事件。
- 缓存只保存已知成功响应；命中时 `cached=true`、费用 0。

细节与价格表见 [MODEL_ADAPTERS.md](MODEL_ADAPTERS.md)。

## 2. 状态所有权映射

| Harness 概念 | 现有权威 | 说明 |
| --- | --- | --- |
| 会话 | `app.threads` | Harness 不新建 Session 数据库 |
| 轮次/任务 | `app.runs` | 一次明确任务或追问绑定一个 Run |
| 步骤 | `app.run_events.safe_payload.operation_id` | Harness 事件信封内的版本化字段 |
| 执行代 | `app.runs.execution_epoch` | 旧 epoch 不得继续编辑/执行 |
| 预算预占/结算 | Engine 模型/工具账本 | Harness 只调用入口，不自行记费用 |
| Finding | 现有 finding store | Harness 不改变生命周期 |
| Approval | 现有 approval record | 批准/拒绝不由 Harness 决定 |
| 任务状态 | Engine task transaction | Harness 结果不是任务完成 |
| 最终完成 | Engine 事务 + Verifier 回执 | 二者缺一不可 |
| 事件游标 | `app.run_events.event_id`（bigserial） | Harness `EventEnvelope.cursor` 投影到该列 |
| 工作区版本 | `WorkspaceSnapshot.tree_hash` | 模型/工具/验证共享同一版本 |
| Review Bundle | 现有 Review Bundle 服务 | Harness 不发布 |

Harness **不拥有**：任务状态、Finding 生命周期、审批决定、预算结算、发布、最终
业务完成。

### 2.1 事件信封与 `app.run_events` 的真实映射

现有迁移 `migrations/0001_coordination.sql` 的 `app.run_events` 列是：

```text
event_id bigserial primary key
run_id uuid not null
kind text not null
safe_payload jsonb not null
created_at timestamptz not null default now()
```

因此 v1.1 明确：

- `EventEnvelope.cursor` = 持久化后的 `app.run_events.event_id`；`Engine.events()`
  使用 `event_id > after` 读取。
- `EventEnvelope.envelope_id`、`operation_id`、`execution_epoch`、`revision`、
  `payload` 不是表列，统一放入 `safe_payload`，schema 为 `harness-event-v1`。
- `safe_payload` 的结构由 `EventEnvelope.to_run_event_safe_payload()` 生成；存入时
  只取该对象，不把 `run_id`/`kind`/`created_at` 重复写入 payload。
- H01 不修改迁移、不新建事件表；持久适配与历史回放由 H09 前向迁移交付。

## 3. 执行顺序（不可绕过）

```text
run_started
  -> BudgetLedger.reserve(model)
  -> ModelAdapter.complete
  -> BudgetLedger.settle | mark_unknown
  -> CapabilityGrant 预检（runtime）
  -> BudgetLedger.reserve(tool)
  -> ToolRegistry.invoke
       -> WorkspaceProvider 或 ExecutionBroker 边界复核
  -> BudgetLedger.settle(tool)
  -> Verifier.verify（匹配 tree 的执行回执）
  -> run_finished
```

约束：

1. 模型适配器只能拿到声明后的 `ToolSpec`，不能拿到 `ToolRegistry` 实例。
2. runtime 只调度 `specs()` 中已声明的工具名；未声明名称在 handler 之前拒绝。
3. read/write/execute 工具必须按 `ToolSpec.capability` 提供可校验的路径或 argv，
   不能仅凭工具名信任 handler；没有可校验参数时默认拒绝。
4. 工具适配器不能直接写业务账本；预算和权限只在 runtime/Broker 入口发生。
5. 未知模型结果保留预占，标记 `HARNESS_MODEL_UNKNOWN`，不得自动重试。
6. `verified` 只表示 Verifier 接受当前 `tree_hash`；不能直接改成 Engine 的任务完成。

## 4. Super-Agent Mandate 映射

Super-Agent 的 Source/策略生成 Mandate 后，按下表映射为现有 Task/Run 字段；H01
只冻结字段与语义，不实现 SAV1 的 Router/订阅。

| Mandate/任务输入 | Harness/Engine 字段 | 说明 |
| --- | --- | --- |
| actor / owner | `TaskBinding.actor`、`CapabilityGrant.actor` | 受信内部 Engine principal；大小写敏感，必须精确一致，否则 403 |
| scope / project | `TaskBinding.repository`、`WorkspaceSnapshot.workspace_id` | repository 与 workspace 绑定 |
| repository | `TaskBinding.repository` | 小写规范化 |
| base SHA | `TaskBinding.base_commit` | 40 位 commit；工作区快照必须与之对应 |
| task/run | `TaskBinding.task_id`、`thread_id`、`run_id` | 由现有 Task/Run 生成 |
| input revision | `TaskBinding.acceptance_revision`、`ContextView.revision` | 目标变化必须递增 |
| execution epoch | `TaskBinding.execution_epoch` | 旧 epoch 的 Worker 不得写回 |
| budget | `BudgetLimits` / `BudgetLedger` | 预占先于调用，结算按真实 usage |
| unknown outcome | `BudgetLedger.mark_unknown` | 保留预占，禁止盲目重试 |
| workspace | `WorkspaceSnapshot.tree_hash` | 编辑/执行/验证共用 |
| verification | `VerificationReceipt` | 只认可匹配 tree 的回执 |

`actor` 语义：它是 SA02 身份映射产出的受信内部 Engine principal，不是飞书/平台
opaque ID。原始 `SourceActorRef` 由身份服务单独保存，保留原大小写与安装/租户范围；
Harness 不对原 ID 调用 `lower()`，也不从消息正文或模型输出推断 actor。若未来 v1
需要直接承载原始平台 ID，必须另立字段与校验规则并升契约版本。

## 5. 与旧只读分析和修复器的兼容

| 适配器 | 路径 | 边界 |
| --- | --- | --- |
| `LegacyAnalysisAdapter` | `src/mgar_v3/harness/legacy.py` | 只读；没有 apply/execute/approve 方法 |
| `LegacyRepairAdapter` | `src/mgar_v3/harness/legacy.py` | 只委托 `RepairAgent.run`，不新增发布或审批权 |

旧分析/修复的完整行为仍由既有 V32/V34/V35 测试覆盖；H01 只验证签名与委托边界。

## 6. 兼容边界（H01 完成时）

- H01 未修改 Engine、API、预算表、数据库迁移或现有修复/发布语义。
- v1.1 相对 v1 的兼容变化：`EventEnvelope.event_id` 更名为 `envelope_id`；
  `cursor` 明确投影到 `app.run_events.event_id`；actor 不再 `lower()`。
  v1（`e1b3214`）的独立探针失败结果保留，不覆盖为 PASS。
- 未新增 Model 供应商调用；`live_model_gate`、`live_agent_gate`、
  `live_publication_gate` 均为 `NOT_RUN`。
- 未提供 H03 的本机/GitHub 工作区保护、H04 的真实容器 Broker、H05 的增量编辑、
  H06 的命令 Job、H09 的持久 inbox、H11 的恢复/fencing。
- DSH 后端未接入；ADR 记录 `NOT_RUN_NO_NODE_RUNTIME`，不得在后续文档改写成 PASS。

## 7. 工作区树哈希与运行期产物

当前已复验规则（`096300e`，2026-11-11，最终独立复验 **PASS**）：树哈希只有一份
排除规则 `contracts.is_workspace_artifact()`；`workspace._scan`、工作区复制与搜索、
`broker.workspace_tree_hash` 共用该函数，不各自维护跳过列表。

- 保留的元数据/缓存目录约定：`.git`、`.mgar`、`.harness-tmp`、`__pycache__`、
  `.pytest_cache`、`.mypy_cache`、`.ruff_cache`、`.venv`、`node_modules`、`.tox`；
  字节码文件后缀为 `.pyc` / `.pyo`。
- 覆盖率数据只排除精确 `.coverage`，或完整匹配
  `.coverage.<host>.<数字 pid>.<nonce>` 的并行数据文件名；不再使用 `.coverage*` 前缀规则。
- `.coveragerc`、`.coverage.conf`、`pyproject.toml` 等项目配置参与工作区复制、扫描与树哈希。
  `coverage.xml` 与 `htmlcov` 不再全局排除，报告或夹具的真实变化也参与哈希。
- 如需生成不应改变任务代码树的临时报告，显式放入保留目录 `.harness-tmp`，并在
  命令/产物回执记录。排除是工作区保留命名约定，不证明任意项目同名路径都不含源码。
- 任一普通源码、配置或受跟踪报告变化必须使树版本变化；编辑同时校验目标文件
  `expected_sha256` / pre-image。旧树不能沿用新树的测试、补丁或审批结论。

旧规则的工作区可能已经在复制时遗漏配置，不能靠重算现有 manifest 恢复遗漏内容。
切换规则后应从已注册源重新建立任务工作区，重新取得基线、测试与审批回执；遇到
旧 manifest/tree 不一致时失败关闭，不静默重签或复用旧通过记录。
原 `98d3042` 的配置遗漏 FAIL 与候选复验追踪见
[记录对齐摘要](RECORD_RECONCILIATION_20261111.json) 的 `hash_boundary_recheck`，
最终独立结果归档于 [哈希复验记录](accept/HV4-H09/workspace-hash-recheck-20261111.json)。

### 7.1 历史规则快照（`98d3042`，保留原文）

以下是旧规则的设计说明；其中 `.coverage*`、`coverage.xml`、`htmlcov` 的广泛排除
已被配置遗漏探针判为不充分，下面的“都不是源码路径”不能作为当前保证。


树哈希只有一份排除规则：`contracts.is_workspace_artifact()`。
`workspace._scan` / 工作区复制与搜索、`broker.workspace_tree_hash` 共用该函数，
不得各自维护一份跳过列表。

排除范围（相对路径任一段命中即排除）：

- VCS/工具元数据：`.git`、`.mgar`、`.harness-tmp`
- 解释器字节码：`__pycache__`、`*.pyc`、`*.pyo`
- 测试/类型/格式缓存：`.pytest_cache`、`.mypy_cache`、`.ruff_cache`
- 依赖与隔离环境：`.venv`、`node_modules`、`.tox`
- 覆盖率产物：`htmlcov`、`coverage.xml`、`.coverage*`

取舍：选择让哈希对运行期产物免疫，而不是把产物移出工作区。容器内
`python -m unittest`/`pytest` 天然在挂载点写 `__pycache__`；要移出需改
`PYTHONPYCACHEPREFIX` 或额外挂载，会改变子进程环境，且仍可能漏掉覆盖率与
测试缓存产物。代价是这些路径下的改动不进入树哈希；但它们都不是源码路径，
`TaskWorkspace.apply` 仍按目标文件的 `expected_sha256`/pre-image 校验，真实
源码编辑仍会改变树哈希并触发 `PATCH_STALE`（见 H06 回归测试）。


## 8. 已知限制（H09 交付时）

- API 驱动的任务在 `service.py` 中以 `TaskBinding(run_id=UUID(int=0))` 建立，
  事件 `run_id` 恒为零 UUID，不能用于事件归属或关联。隔离仍由每任务独立
  `events.jsonl` 与严格 `task_id`（`^h08-[0-9a-f]{12}$`）校验保证；本卡不扩大
  改动范围，在此如实记录。
