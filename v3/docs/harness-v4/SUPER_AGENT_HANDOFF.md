# HV4-H01 → Super-Agent 协作交接

- 交接日期：2026-11-10
- 交接方：Harness（`/home/developer/magr-harness`）
- 接收方：Super-Agent（`/home/developer/magr-super-agent`，只读同步）
- 对应请求：`super-agent-v1/COORDINATION.md`「向 Harness 请求的 H01 交付」
- 本轮触发：`super-agent-v1/H01_REVIEW_FEEDBACK.md` 四项反馈
- 当前状态：**修复已交付，等待 Super-Agent 复验接收**；旧提交 `e1b3214` 的独立探针
  失败记录保留，不改写为 PASS。

## 1. 阅读与确认

已完整重读 H01 任务书（`TASKS.md#hv4-h01` 及 ROADMAP/ACCEPTANCE/plan/sources）和
本轮评审反馈。指纹是读取时的版本；Harness 状态更新会改变工作树文件。

| 文件 | 读取时间（UTC） | sha256 |
| --- | --- | --- |
| `docs/harness-v4/TASKS.md` | 2026-11-10T13:02:17Z | `335c7ff215db54c567e1b2eef2f395db850868fb4013897fa859f3c3d8d41c2a` |
| `docs/harness-v4/ROADMAP.md` | 2026-11-10T13:02:17Z | `d6696778d6983fe70ade6f2109f58748903b281b40d70f940ef349e9fff69f3a` |
| `docs/harness-v4/ACCEPTANCE.md` | 2026-11-10T13:02:17Z | `e1035c8825171ac1ac36c628ed5da7e12620eb761a152e28061d4aeaadb5d28a` |
| `docs/harness-v4/plan.json` | 2026-11-10T13:02:17Z | `58ac3aed5661e8f6efa187b16a62c889e0ffe12c03765ff956dda90d04695653` |
| `docs/harness-v4/sources.json` | 2026-11-10T13:02:17Z | `2fc881ba53204f8b7c93943d2f06068472d4b9a3d5f1fcb9f80d20f0dbcb34b6` |
| `super-agent-v1/COORDINATION.md` | 2026-11-10T13:02:17Z | `49cb00968a10380fc4456c5315756ee6f6429968911242b5bd54321b6060ec26` |
| `super-agent-v1/H01_REVIEW_FEEDBACK.md` | 2026-11-10T13:29:48Z | `b01443e546625b86faf8448ad66ac26c6b6a46daaf702f65eb8dbd233c1b2740` |
| 外部探针 `probe.py` | 2026-11-10T13:29:48Z | `b12d91dc92d3c1bcab8798d253582ca7467fa68083b94e7037356194333eddc1` |
| 外部探针 `result.json` | 2026-11-10T13:29:48Z | `8108b93e4ecfe08e86d8c73deb70cc9e23a8506d6a7e7f23aafb5e163d63cfd0` |

确认协作约定：双方不改对方活动工作树；共享 Engine/API/预算/迁移变更先协调；
每卡实施前完整阅读任务书；历史 FAIL/NOT_RUN 原样保留。本条为实际对话确认，
只实施一次；此前 CLI 返回 0 但未见送达的内容不作为已交付依据。

## 2. 交付状态

| 交付项 | 状态 | 路径 |
| --- | --- | --- |
| 固定修复提交 | `418d0c248df5764de7028470311c8e8479851b1f`（本地 `main`，未推送） | 见 `accept/HV4-H01/accept.json` |
| 旧提交 | `e1b3214`：独立探针 2 项 FAIL，**已被取代，不撤回历史记录** | 外部 `result.json` |
| 选型 ADR | DONE | `docs/harness-v4/adr/HV4-H01-deepseek-harness.md` |
| 版本化接口 | **`harness-contract-v1.1`**，FROZEN | `src/mgar_v3/harness/contracts.py` |
| 机器可读接口清单 | DONE | `experiments/harness-v4/harness-contract-v1.json` |
| 状态所有权映射 | DONE，已对齐真实 `run_events` schema | `docs/harness-v4/HARNESS_CONTRACTS.md` §2 |
| 受控接线样例 | DONE（测试适配器 + 真实本地测试） | `src/mgar_v3/harness/runtime.py`、`tests/test_harness_v4_h01.py` |
| 失败分支记录 | DSH `NOT_RUN_NO_NODE_RUNTIME`；旧 H01 探针 FAIL 保留 | ADR §3.2、accept.json |
| 真实验收证据 | 18 项 H01 测试、10 项 H00、226 项核心回归、复跑外部探针 PASS | `evidence/harness-v4/HV4-H01/` |

工程门与 live 门分开：H01 只声明契约与确定性参考实现；`live_model_gate`、
`live_agent_gate`、`live_publication_gate` 均为 `NOT_RUN`。

### 2.1 H01 接收结论（已收到）

Super-Agent 已在独立工作树接收固定交付 `38a65ba8889267aee4f1f3f29799d6c79ff2d49b`
（本地 merge `782f35c`），并给出以下独立复验结果：

- 同一原始无 I/O 权限探针 2/2 PASS，handler 均 0 次且 `denied`。
- 本副本全库 ruff/mypy PASS。
- H01 注册验收 18 项 PASS。
- `test_v32_repository_analysis_db.py` 5 项真实 PG 回归全部 PASS，无失败/错误/跳过。
- 证据：`v3/evidence/super-agent-v1/integration/HV4-H01/20261110T135105Z/`、
  `v3/evidence/super-agent-v1/SAV1-00/20261110T135100Z-h01-correction-review/`。

结论：按 `harness-contract-v1.1` 接收 H00/H01 的契约/基线范围；真实修复门仍未开放。
SA00 冻结包已提交；Sources `0030` 由 Codex 负责；ADR 分轨命名空间已确认。

## 3. 评审反馈逐项回应

### 3.1 事件游标与步骤字段映射到真实 schema（已修复）

反馈：`EventEnvelope`/文档曾写 `app.run_events.seq` 和 `operation_id` 列，但真实列是
`event_id bigserial`、`run_id`、`kind`、`safe_payload`、`created_at`。

修复：

- `EventEnvelope.cursor` 明确映射 `app.run_events.event_id`（`Engine.events()` 按
  `event_id > after` 读取）。
- `envelope_id`、`operation_id`、`execution_epoch`、`revision`、`payload` 统一放入
  `safe_payload`，schema `harness-event-v1`，由
  `EventEnvelope.to_run_event_safe_payload()` 生成。
- 字段 `event_id` 更名为 `envelope_id`，避免与数据库 bigserial 列混淆。
- `STATE_OWNERSHIP` 与 `harness-contract-v1.json` 已同步；H01 不修改迁移、不新建
  事件表，持久适配在 H09 前向迁移交付。
- 新增 `test_event_envelope_maps_to_the_real_run_events_schema`，直接断言
  `0001_coordination.sql` 的 `event_id bigserial`/`safe_payload jsonb` 与安全载荷结构。

### 3.2 SA00 与真实自动修复的前置分开（已修正）

反馈：旧交接把 SA00 限制到 H02–H07/H11 之后，扩大了 SAV1-00 的 DAG。

修正：**SA00 只依赖 H00/H01**。H01 修复复验后，SA00 可完成来源、范围、复用审计、
ADR 与验收协议冻结。真实自动修复不在 SA00 范围内，等待 H11/H12 及所需工作区、
Broker、命令与编码能力验收；这不作为 SA00 的完成前置，也不提前开放修复。

### 3.3 actor 是受信内部主体，不直接承载平台 opaque ID（已修复）

反馈：`TaskBinding.actor` 曾使用 ASCII 正则并 `lower()`；飞书 ActorRef 可能区分
大小写且需要安装/租户范围。

修复：

- `actor` 不再 `lower()`，`_authorize` 使用精确比较；大小写不同即
  `HARNESS_ACTOR_MISMATCH`。
- `actor` 定义为 SA02 身份映射产出的受信内部 Engine principal，不是原始平台 ID。
- 原始 `SourceActorRef` 由身份服务单独保存，保留原大小写与安装/租户范围；禁止从
  正文/模型推断映射。
- 新增 `test_actor_is_a_case_sensitive_internal_principal`。
- 若未来 v1 需要直接承载原始平台 ID，必须另立字段与校验规则并升契约版本。

### 3.4 只读许可与未声明工具的默认拒绝缺口（已修复）

反馈：对 `e1b3214` 的隔离探针发现：

- Registry 声明 `custom_write` 为 write、Grant 的 `write_paths` 为空，handler 仍被调用 1 次。
- Model 返回 Registry 未声明的 `custom_write`，handler 同样被调用 1 次。

根因：`_precheck` 只校验四个固定工具名，其他名称返回 `None`；随后直接调用
可替换的 `Registry.invoke`。

修复：

- runtime 在每次 Run 开始时快照 `ToolRegistry.specs()`，建立已声明工具表。
- 未声明工具在 handler 之前直接 `denied`，事件带
  `HARNESS_TOOL_UNDECLARED`。
- 已声明工具按 `ToolSpec.capability` 做通用校验：
  - read：必须有 `path` 或 `edits[].path`，逐路径 `grant.can_read`。
  - write：必须有 `path` 或 `edits[].path`，逐路径 `grant.can_write`；无可识别
    路径时默认拒绝。
  - execute：必须有 `argv`，并通过 `grant.can_execute`。
- 不再仅凭固定工具名放行；H14 的通用扩展也必须声明 capability 与可校验参数。
- 新增 3 项真正负向回归：
  - `test_undeclared_tool_is_denied_before_the_handler` → handler 0 次；
  - `test_declared_write_tool_is_denied_under_a_read_only_grant` → handler 0 次；
  - `test_declared_write_tool_is_dispatched_only_with_a_matching_grant` → 授权匹配时
    handler 1 次，证明扩展工具仍可安全接入。
- 使用外部 `probe.py` 在 Harness 工作树复跑：两个 case 均为
  `handler_invocations=0`、`runtime_status=denied`、`conformance=PASS`。

### 3.5 核心回归旧竞态：`finish_success` 两事务窗口（补充定位，已修复）

反馈补充定位：`test_agent_page.py` 的 upgraded 用例在 run 已 `succeeded` 后仍看到
`stop_reason=None`。根因是 `repository_analysis/store.py::finish_success` 先由
`engine.commit_report()` 提交 run 终态，再用第二个事务写
`repo_analysis_runs.stop_reason/report_hash`；读者可能落在两个事务之间。

修复（最小原子性改动）：

- `Engine.commit_report()` / `commit_prepared_report()` 新增可选
  `repo_analysis_stop_reason`；在同一个 Engine 事务内写入
  `app.repo_analysis_runs.stop_reason` 与 `report_hash`。
- `RepositoryAnalysisStore.finish_success()` 只调用一次 `commit_report`，删除第二个
  更新事务；返回值仍从 `spec()` 读取。
- 未改变 run 状态机、迁移、预算或发布语义；其他 `commit_report` 调用不传该参数，
  行为不变。
- 新增确定性回归
  `tests/test_v32_repository_analysis_db.py::test_finish_success_commits_stop_reason_with_run_status`：
  在 `finish_success` 提交后、`spec()` 读取前用事件阻塞，再从独立连接读取
  `app.runs` 与 `app.repo_analysis_runs`；断言 `status=succeeded` 时
  `stop_reason=complete` 且 `report_hash` 已存在。修复前该断言会稳定看到
  `stop_reason=None`。
- 旧失败日志保留在 `evidence/harness-v4/HV4-H01/h01-review-fix-attempt1/`；
  修复后核心回归 226 passed（含新增 DB 原子性用例）。

## 4. 接口路径、版本与冻结状态

- 契约版本：**`harness-contract-v1.1`**（v1 在 `e1b3214` 的评审失败保留）
- 包路径：`src/mgar_v3/harness/`
- 入口：`mgar_v3.harness`（`__init__.py` 导出冻结类型与参考实现）
- 契约模块：`src/mgar_v3/harness/contracts.py`
- 参考组合：`src/mgar_v3/harness/runtime.py` 的 `HarnessRuntime`
- 本地适配器：`src/mgar_v3/harness/adapters.py`
- 旧服务适配：`src/mgar_v3/harness/legacy.py`

### 4.1 冻结接口

`TaskBinding`、`CapabilityGrant`、`ModelAdapter`、`ToolRegistry`、
`WorkspaceProvider`、`ExecutionBroker`、`ContextManager`、`Verifier`、
`EventSink`、`BudgetLedger`。

方法签名与执行顺序见 `HARNESS_CONTRACTS.md` §1/§3。v1.1 的破坏性变化：
`EventEnvelope.event_id` → `envelope_id`；`cursor` 的数据库含义明确化；actor 精确
比较。Super-Agent 应以 v1.1 重新校验，不接受 v1 的旧断言。

### 4.2 建议接口（未冻结）

流式模型 delta、命令 Job 的 run/poll/cancel、持久 inbox/安全点、上下文压缩与
分页、崩溃恢复/fencing、远程 Runner/多用户、插件/MCP 扩展。计划卡分别为
H02、H06、H09、H10、H11、H15–H16、H14。

## 5. Mandate → Task/Run 映射

字段级映射见 `HARNESS_CONTRACTS.md` §4。要点：

- actor 是受信内部 Engine principal，大小写敏感，必须与 Grant 精确一致；原始
  `SourceActorRef` 单独保存，不 lower、不从正文/模型推断。
- repository、workspace 必须与 Grant 一致，否则 `403`；repository 小写规范化。
- `base_commit` 为 40 位 SHA；工作区快照必须与该 commit 对应。
- `acceptance_revision` 与 `ContextView.revision` 绑定目标版本；目标变化必须递增。
- `execution_epoch` 用于旧 Worker fencing；H01 只定义字段，真正 fencing 在 H11。
- 预算：`BudgetLedger.reserve` 先于模型/工具调用；`settle` 按真实 usage；
  `mark_unknown` 保留预占并禁止盲重试。
- 未知模型结果：`UnknownModelOutcome` → `HarnessResult.status = "unknown"`。
- 最终完成：Verifier 回执 + Engine 事务；Harness `verified` 不是任务完成。

## 6. 能力边界与依赖

| 能力 | H01 状态 | 说明 |
| --- | --- | --- |
| 模型/工具/工作区/执行/上下文/验证/事件契约 | 提供 | v1.1 冻结 |
| 确定性参考回路（读→改→真实测试） | 提供 | 本地文件 + 真实 Python 子进程 |
| 旧只读分析适配 | 提供薄适配 | `LegacyAnalysisAdapter`，无写/执行方法 |
| 旧修复器适配 | 提供薄委托 | `LegacyRepairAdapter` → `RepairAgent.run` |
| 流式模型协议/第二模型供应商 | NOT_AVAILABLE | H02 |
| 本机/GitHub 工作区保护、dirty 处理 | NOT_AVAILABLE | H03 |
| 统一能力 Broker 与容器隔离 | NOT_AVAILABLE | H04；H01 只有本地一次性 Broker |
| 增量编辑/多文件原子补丁 | NOT_AVAILABLE | H05；H01 只有带 pre-image 的整文件编辑 |
| 命令 Job 启动/查询/取消 | NOT_AVAILABLE | H06；H01 只有一次性 `execute` |
| 统一编码循环/多类型验收 | NOT_AVAILABLE | H07；H01 是契约参考回路 |
| 持久 inbox/运行中交互 | NOT_AVAILABLE | H09 |
| 崩溃恢复/fencing | NOT_AVAILABLE | H11 |
| 真实模型/Agent/发布门 | NOT_RUN | H12 才有真实评测 |

**SA00 依赖**：仅依赖 H00/H01。H01 修复复验后，SA00 可完成范围、复用审计、ADR
与验收协议冻结；真实自动修复等待 H11/H12 及所需工作区/Broker/编码能力验收，
不属于 SA00 的 DAG，也不应提前开放。

## 7. Diff/tree、验证回执与 Review Bundle 版本绑定

- `WorkspaceSnapshot.tree_hash` 是模型输入、工具执行与验证共用的版本。
- `ExecutionReceipt` 记录 `tree_hash`、argv、cwd、退出码、测试数、日志 hash 和
  环境 hash；Verifier 只接受与当前 tree 匹配的回执。
- 真实 Diff 由 `WorkspaceProvider.diff(tree_before)` 生成；H01 适配器已实现，
  生产级增量 Diff/原子多文件回滚由 H05 强化。
- Review Bundle 继续由现有服务生成与封存；H01 不创建、不批准、不发布。
  审批必须绑定 Diff/tree/目标版本；该失效语义在 H07/H11 验证。

## 8. 协作、迁移与命名空间

- 未修改 `/home/developer/magr-super-agent` 的任何文件；仅只读读取反馈、探针与结果。
- 按协调修改了共享 Engine/store 的 `finish_success` 原子性（两事务窗口），
  未修改 API、预算表、数据库迁移、审批或发布语义；其他 `commit_report` 调用行为不变。
- 未新增业务写者；`mgar_v3.harness` 只做组合与端口。
- 未新增付费模型调用、GitHub 写入或网络发布。
- 历史 V35-E02 `FAIL`、E01/E03/publication `NOT_RUN` 未改写。
- 迁移编号：Sources 迁移由 Codex 负责 `0030`；Harness 若需共享迁移，先与 Codex
  对齐编号，不直接占用 0030。H01 未新增迁移。
- ADR 命名空间：Harness 使用 `docs/harness-v4/adr/HV4-Hxx-*.md`；Super-Agent 使用
  其轨道内命名空间，避免文件名碰撞。

## 9. 集成复验要求（Super-Agent 侧）

1. 以 `harness-contract-v1.json` 的 **v1.1** 校验接口名/方法；不要接受 v1 的旧
   `event_id` 字段或 lower 化 actor 断言。
2. 在 Super-Agent 自己的数据库/Redis/临时目录运行集成验证；H01 的证据只覆盖
   Harness 工作树。
3. 复跑外部 `probe.py`，两个 case 应为 handler 0 次、status `denied`。
4. 若需要新增字段或改变状态语义，先提出契约变更（版本、受影响卡、兼容方法），
   不能在 Super-Agent 内私建第二个 Harness 状态机。
5. 只有复验通过后才接收 H01；本交接不代替 Super-Agent 的独立复验。

## 10. 下一步

- Harness：H01 已接收；H02 模型适配器已实施并等待外部复验，H03 尚未启动。
- Super-Agent：按 H00/H01 前置完成 SA00 冻结，不等待 H11/H12。
- DSH 后端：保持 `NOT_ADOPTED_H01` / `NOT_RUN_NO_NODE_RUNTIME`，在 H12/H14 条件
  具备时重新评估。

## 11. H02 模型适配器交付（2026-11-10）

- 状态：DONE；已通过独立复验并按记录范围接收；真实 DeepSeek Flash 实验
  PASS，Anthropic 真实调用 `NOT_RUN_NOT_AUTHORIZED`。
- 独立复验：基线 `169925f`，交付 `cfa1019`，工作树干净；lint、mypy 197 files、
  H02 30 项、H00/H01 28 项、注册 verify（`evidence/HV4-H02/20261110T152734Z-ff20bae6/`）
  与 P1/P2/原五项探针全通过；source sha256 `2060b380…` 与原验收一致。
- 初始提交：`505dede45ed0cac0c96276d9a6587bb4a4d7ac34`（旧范围 PASS 保留）。
- 第一次修复提交：`fbb9ff1a304aaa134dac0901283e8a1c05f259f2`（五项外部复验）。
- 第二次修复提交：`169925f22b770bbcdb1294f079d5d8c76d05b1b4`（本地 `main`，未推送）。
- 协议版本：`harness-model-v1`；默认 DeepSeek，第二协议 Anthropic Messages。
- 代码：`src/mgar_v3/harness/model.py`。
- 能力表：`experiments/harness-v4/model-capabilities-v1.json`。
- 文档：`docs/harness-v4/MODEL_ADAPTERS.md`、ADR `adr/HV4-H02-model-protocols.md`。
- 对 H01 v1.1 为加法扩展：新增 `ModelStreamEvent`、`StreamingModelAdapter`、
  `ModelUsage.known` 与 `ModelResponse` 的 provider/model/price/cached/error_code；
  未移除已接收字段。
- 受控协议：30 项 H02 测试通过，H01/H00 28 项回归通过，核心回归 226 项通过，
  覆盖两个适配器同一工具契约、分片 UTF-8/JSON、工具参数中断、缺 usage、上下文
  超限、取消、429 同供应商重试、401 不重试、缓存不重复计费。
- 第一次外部复验修复（fbb9ff1）：HTTP 200 后 read timeout 不重发；非法/缺失
  usage 保持 unknown；发送后取消与畸形 SSE 保留预占；两协议原生工具历史；
  CRLF 跨 chunk 解码。
- 第二次外部复验修复：
  - `error`/`length` 响应不再分派任何工具提案；适配器只保留权威
    `finish_reason=tool_calls` 的工具调用，runtime 在分派前先处理失败/截断。
  - 无最终 finish 的传输中断即使已缓冲完整工具 JSON 与 usage，也不执行工具、
    不重发；权威 usage 仍可据实结算。
  - Anthropic 负数 `input_tokens`/`output_tokens` 映射为 unknown，runtime 返回
    `unknown` 并保留 `unknown` 预占，不再抛 `ValidationError`。
  - 新增 6 项回归：content_filter、refusal、length+并行工具、无 finish 超时、
    负数 input/output；H02 共 30 项。
- 真实 DeepSeek Flash 实验（用户授权：仅 `deepseek-flash`，单卡累计 2 USD）：
  - 调用前 `GET https://api.deepseek.com/models` 返回 `deepseek-flash`、
    `deepseek-v4-pro`；官方价格页确认 `deepseek-flash` =
    `DeepSeek-V4.1-Flash-1017`，峰值 cache-miss $0.3/M、output $1.2/M。
  - 固定提交 `169925f`；2 次真实 POST、无重试；第 1 轮 `tool_calls` 并执行
    `read_file` 1 次，第 2 轮 `stop`；tokens 407/45 与 476/74；按峰值价格上界
    计算 177+232=409 micros（$0.000409），不是供应商账单实际扣费，低于 2 USD
    上限 0.03%。
  - 硬预算：预检最坏 1884 micros；每次预占 100000 micros；账本上限
    2000000 micros；无未知结果、无重试。
  - 证据边界：只保存本地 `operation_id`、请求路径与归一化 usage；未保存供应商
    原始 request ID、原始回执或账单，不声称独立账单核验。runtime `incomplete`
    仅因合成任务 verifier 条件未满足，不代表编码任务完成。
  - 实验使用显式 Flash capability 并关闭 thinking，不等于生产 frozen
    `deepseek-chat` manifest 已完成切换。
  - Anthropic 真实调用未授权：`NOT_RUN_NOT_AUTHORIZED`，不宣称双供应商真实通过；
    整体 `live_model_gate` 记 `PARTIAL`。
  - 记录：`experiments/harness-v4/h02-live-probe.py`、
    `experiments/harness-v4/h02-live-results.json`。
- 修复证据：`evidence/harness-v4/HV4-H02/h02-second-review-fix/`（原始 P1/P2
  失败、修复后复跑、30 项测试、核心 226）；
  真实证据：`evidence/harness-v4/HV4-H02/live-flash-20261110T151946Z/`。
- 真实供应商：DeepSeek Flash 已实际调用并记录；Anthropic 仅受控；
  `live_agent_gate` / `live_publication_gate` 仍为 `NOT_RUN`。
- 未修改数据库迁移；Sources `0030` 归 Codex。
- Super-Agent 可依赖模型端口契约，但真实自动修复仍需 H03/H04/H06/H07/H11/H12。

## 12. SAV1-01 交接确认（2026-11-10）

已按非紧急交接要求，在 H02 修复完成后只读阅读：

| 文件 | 读取时间（UTC） | sha256 |
| --- | --- | --- |
| `super-agent-v1/HARNESS_SA01_HANDOFF.md` | 2026-11-10T14:45:23Z | `c0d0605e28d9eb9e0896bbc03daabf3613b5b7ebde3cd5b752a87b6794ff2dea` |
| `super-agent-v1/SA01_CONTRACTS.md` | 2026-11-10T14:45:23Z | `0a6832be36e10865decbecb4808bc157a63b0c39af641502af0c47d10cf420db` |
| `super-agent-v1/accept/SAV1-01/accept.json` | 2026-11-10T14:45:23Z | `681dcf9867eb14286502ace0eacb3119581af738cb877298f031906f2d1b9d5e` |

确认内容：

- SAV1-01 实施提交 `8dc641bbdf1fe7d0f057e11ec66e951f2874f2f8`，验收文档提交
  `5fda5443a9e2753ee4d4492d30da23a5e09c6d2a`。
- 接口版本 `source-contract-v1` / `source-event-v1`；`0030_source_contracts.sql`
  由 Codex 按约定落地，包含 source 安装/版本/未关联收件箱/业务链接及既有
  Thread/Ticket/Run 复合 FK；未改 Harness Engine/API/旧 domain/模型/预算/0001–0029。
- 注册验收 79 PASS（0 失败/错误/跳过），定向 51 PASS（与整卡有重叠），全库
  lint/mypy PASS 201 files，前后 source sha256
  `5f3c9f216739cee829ebe25fe62be63455917875e70586fecbdb555c8e86738e`。
- `ActorRef` 保留原始 ID 大小写和安装/租户范围，但**不是** Harness 内部 actor；
  可信映射由 SA02 产生，当前不得直接 lower、放入 Harness grant 或当作 Engine
  principal。
- 没有真实来源监听、付费调用、Worker、通知或发布；SA01 的
  `policy_security_gate`、`live_source_channel_gate`、`live_coding_gate`、
  `live_publication_gate`、`continuous_operation_gate` 均为 `NOT_RUN`。
- 当前不要求合入；若后续集成，按固定提交评审，共享迁移编号继续先对齐。
  Harness 未修改 `/home/developer/magr-super-agent` 活动树。

对 Harness 的影响：H03 仍需按自己的任务书实施；SA01 的 Source/ActorRef 不能替代
H03 的 workspace/变更保护，也不能绕过 H01 的 actor/权限入口。H02 仍等待外部复验，
H03 未启动。

## 13. SA02 开工协调确认（2026-11-10）

确认时间：**2026-11-10T14:50:28Z**。
Harness 当前 HEAD：`6109ce5cb552e65ad4b4111dfd9b2e97fc2d353f`，工作树干净。

### 13.1 迁移编号

- Harness 当前迁移目录止于 `0029_analysis_token_reservations.sql`；`0030_source_contracts.sql`
  由 Codex 在其副本落地，Harness 不复制、不占用。
- **下一共享迁移 `0031` 确认由 Codex 的 SA02 平台身份/角色/审计使用；Harness 不占用
  0031。** 本确认不预占 0032，也不改变 Harness 后续卡的迁移计划。
- 后续任何 Harness 共享迁移仍先与 Codex 对齐编号，再写入迁移文件。

### 13.2 API 范围

- Harness `src/mgar_v3/api.py` 当前没有 `/api/v3/source-identities/` 路由，
  也没有 source identity 相关端点。
- 本轮 **Codex 拥有新的 `/api/v3/source-identities/` 管理路由与 `api.py` 最小接线**；
  复用现有登录/CSRF；既有 Harness 端点保持兼容，不改路径、鉴权或语义。
- Harness 的 H03/H08 不会新增该路由族；若未来需要改动共享 API，先提出文件/路径
  范围并确认。
- 无 API 范围冲突。

### 13.3 共享实现边界

- Codex 的 SA02 不改 Harness Engine、模型、预算或 Harness 实现；身份服务只使用
  现有 Engine 持锁连接。
- 新来源角色/任务控制关联不创建第二业务状态机；Harness 的 Task/Run/epoch/预算
  /审批入口保持唯一。
- 双方不修改、不导入对方活动树；需要集成时按固定提交评审。
- 本消息不构成 H02 接收结论。H02 仍等待外部复验；H03 状态仍由其原前置决定。

## 14. H02 接收与 H03 交付（2026-11-10）

- H02 已通过独立复验并按记录范围接收：实现 `169925f`、交付 `cfa1019`，工作树
  干净；lint、mypy 197 files、H02 30 项、H00/H01 28 项、注册 verify
  （`evidence/HV4-H02/20261110T152734Z-ff20bae6/`）与 P1/P2/原五项探针全通过；
  source sha256 `2060b380…` 与原验收一致。整体 `live_model_gate` 保持 `PARTIAL`：
  仅 DeepSeek Flash 最小往返已记录，Anthropic 真实调用未授权，agent/publication
  未通过。409 micros 是按峰值价格上界计算，不是供应商账单；未保存原始 request ID、
  原始回执或账单，不声称独立账单核验。
- H03 已实施：提交 `4ba2854`，`harness-workspace-v1`。操作者显式注册本机绝对根或
  固定 Commit 的 GitHub URL；每任务独立副本；dirty/untracked 记录并复制；回写前
  校验注册路径、基线与当前 pre-image，冲突零写入；路径穿越、symlink、`.git`、
  hooks、filters、submodule 均拒绝或排除；六类工作区工具；manifest/tree hash
  重开恢复。
- H03 独立复验修复：`fa3dd70` 的三项真实文件负例（apply 后 manifest 未持久化、
  search 跟随 symlink 根、篡改 baseline 后可覆盖用户新改动）由 `133deee` 修复；
  原始 probes 与失败结果保留，同一探针复跑 3/3 PASS；新增 3 项正式回归。
- H03 第二轮独立复验修复：`133deee` 后又发现两项（并发 apply_back 丢失更新、
  本机 dirty inventory 触发 Git clean filter）由 `eb1d267` 修复。apply_back 在源
  目录 inode 的跨进程 `flock` 内重新校验 inode/pre-image 后才写入；dirty 清单改用
  `ls-files` + 直接 blob 哈希，不再调用 `git status`。原始 concurrent/filter 探针与
  失败结果保留，复跑均 PASS；新增 2 项正式回归。
- H03 第三轮独立复验修复：`ec2712d` 的两项回归（staged-only 修改漏记、本机
  `core.fsmonitor` 脚本被执行）由 `85874ff` 修复。dirty inventory 同时比较
  HEAD/index/worktree，覆盖 staged、unstaged、mixed、added/deleted/renamed/
  mode/type/untracked；所有本机 Git 调用集中加 hooks/fsmonitor/untrackedCache/
  submodule/external-diff 禁用项。原始 dirty-hooks 探针与失败结果保留，复跑
  2/2 PASS；新增 2 项正式回归。
- H03 第四轮独立复验修复：`5527ba5` 的两项 dirty 矩阵边界（`git rm --cached`
  后的 D + ?? 被漏报、普通未暂存删除误标 modified）由 `93705cd` 修复；前者保留
  deleted 与 untracked 双事实，后者标 deleted 且 sha256=null。原始 dirty-matrix
  探针与失败结果保留，5 个场景复跑均 PASS；新增 2 项正式回归。
- H03 第五轮独立复验修复：`318cd6e` 的 symlink 父目录逃逸（复制层已排除 nested，
  但 dirty inventory 仍读取 nested/data.txt 的外部哨兵）由 `2a70547` 修复；所有
  dirty 工作树读取走逐级安全检查，父目录 symlink 返回 typechange。原始探针与
  失败结果保留，复跑 PASS；新增 1 项正式回归。
- H03 第六轮独立复验修复：`2a70547` 的 diff symlink 泄露（任务副本 app.py 被替换
  为外部 symlink 后 diff 输出外部哨兵）由 `5f09e04` 修复；diff 当前/基线读取与
  `.mgar`/baseline 元数据路径全部走逐级安全检查。原始探针与失败结果保留，复跑
  PASS；新增 2 项正式回归。同轮双进程 apply_back 探针 PASS（一个 applied、一个
  conflict）。
- H03 第七轮独立复验修复：`3ef11db` 的固定 `manifest.tmp` symlink 写越界由
  `a803784` 修复；所有元数据 JSON 使用同目录随机 `mkstemp`（O_EXCL）+ 原子替换，
  读取使用 NOFOLLOW 安全路径。原始探针与失败结果保留，复跑 PASS（外部文件未变、
  manifest 非 symlink）；新增 1 项正式回归。
- H03 验收（第七轮修复后）：25 项 H03、58 项 H00–H02 通过；ruff、mypy 198 files
  通过；注册 verify `evidence/HV4-H03/20261110T172433Z-4ee1323a/` PASS，source
  sha256 `e0f2ced7…`。本卡无模型调用要求，费用 0；GitHub 验收使用本地 `file://`
  固定提交仓库，真实 GitHub 抓取与 `live_publication_gate` 仍为 `NOT_RUN`。
- H03 边界：本机采用副本而非 Git 分支；重启恢复是逻辑重开（SIGKILL/fencing 属
  H11）；`apply_back` 只覆盖文件新增/修改/删除；manifest/baseline 自 hash 只做
  篡改检测，不是签名存储；并发锁只覆盖同一主机源目录 inode 的 flock，不声称
  NFS/跨主机。未改 Codex 活动树、Engine/迁移/预算或 `0030/0031` 所有权。

## 15. SA02 收讫与 SA03 所有权确认（2026-11-10）

已收到并只读审阅 Codex 的 SAV1-02 固定交付（用户已独立验收通过；本节是 Harness
侧收讫与 SA03 范围确认，不代替 peer 复验）：

| 文件 | 读取时间（UTC） | sha256 |
| --- | --- | --- |
| `super-agent-v1/SA02_IDENTITIES.md` | 2026-11-10T15:57:00Z | `714034a3bd2cd92b5b93348fd587396b6ac9db858a40a3c49b5b0321dfaca7d5` |
| `super-agent-v1/HARNESS_SA02_HANDOFF.md` | 2026-11-10T15:57:00Z | `03519b6b241486f9fe5482f6f933f0dc4d5fbd5d3c3f37c517e2ca2350079729` |
| `super-agent-v1/accept/SAV1-02/accept.json` | 2026-11-10T15:57:00Z | `3efd8077044f22e8bd894dfa78085ba96040e5c4a7095222232274a3ceea9fcf` |

确认内容：

- SA02 实施提交 `3bcfb0234789e000ed26597bc8a6af378675791d`；验收/HEAD
  `bdb4e640faf44fe2eb698cab27630c1b164574d7`；132 tests PASS，lint/typecheck
  204 files PASS，源码 SHA
  `0be627fdeaf1e731d4d9f023bcbeceb72279a9a173b1a37415bc475373c9beb5`。
  `source-identity-v1` 的内部 `sa_<uuidhex>` 只是身份映射，不是旧
  `allowed_actors`、Harness `actor` 或 `CapabilityGrant`；不得直接放入 Harness
  grant 或当作 Engine principal。
- SA03 共享迁移：**0032 明确归 Codex**，用于四层策略版本/签署/撤权事实及与现有
  Run/租约/Outbox 的关联。Harness 不占用 0032、不复制 0030/0031；Harness 迁移
  目录当前止于 `0029_analysis_token_reservations.sql`。Harness 下一共享迁移从
  0033 起另行对齐。
- SA03 API：**Codex 拥有新的 `/api/v3/source-policies/` 路径及 `api.py` 最小注册
  接线**。Harness 当前没有 `source-identities` 或 `source-policies` 路由，不会新增；
  既有 Harness API 路径、鉴权与语义保持兼容。
- SA03 Engine 边界：**Codex 负责仅针对已关联 source-policy 的 Run、待执行工具、
  租约与待发 Action 的当前版本检查与撤权 fencing**。必须保持唯一 Engine/PG，不建
  第二业务状态机；不得改动模型调用、费用结算/预算算法、Harness 文件
  （`src/mgar_v3/harness/**`、`tests/test_harness_v4_h0*`、`docs/harness-v4/**`、
  `experiments/harness-v4/**`），不得改变未关联旧 Run/旧许可的现有行为。异步
  工具/Action 派发前仍需即时检查 policy 与 epoch，不能缓存授权。
- Harness 侧当前无 Engine/API/迁移写入在途：H03 独立复验修复已提交、等待复验；
  H04 尚未启动。若 H04 需要共享 Engine/API/预算或迁移变更，将先提出文件/路径
  范围并等待确认。
- 0031 已按 `0ca7606` §13 由 Codex 落地；Harness 未导入该文件，未编辑 Codex
  活动树。无生产 verifier、平台监听、付费或外发；live 门保持 `NOT_RUN`。
- 结论：**0032、`/api/v3/source-policies/` 与上述受限 Engine 接线归 Codex；无
  所有权冲突。** 本节记录时的 Harness HEAD 为
  `59e4d3a1c7b3cfe077ec91bec109f8ce383e0178`，UTC
  `2026-11-10T15:57:00Z`。

## 16. SA03 共享工具入口补充确认（2026-11-10）

已收到并读取 Codex 对 SA03 文件范围的补充。`4ad86c1` §15 的 0032、API 与受限
Engine 确认继续有效；本节只追加 `agents/tools.py` 的最小接线所有权。

- **共享入口**：`src/mgar_v3/agents/tools.py` 的
  `ToolAuthority._grant(...)` 中新增一条
  `SourcePolicies.check_run(..., capability=tool)` 委托检查，归 Codex 负责。
  该文件不在 `src/mgar_v3/harness/**`，但属于共享工具执行入口。
- **行为边界**：
  - 未关联 source-policy 的旧 Run 在新增检查处立即返回，保持原有 grant、
    role、预算与工具行为不变。
  - 已关联 Run 的策略拒绝必须 fail-closed；不得以工具名、角色或上下文文本
    扩大能力。
  - 预算 charge 表达式与额度语义保持原样；策略拒绝应在 charge 之前返回，
    不为被拒绝的调用消耗工具预算。
  - `check_run` 需要当前 policy 版本/撤权事实；不得缓存授权。不得改动模型调用、
    费用结算/预算算法、Harness 文件或无关旧 Run。
- **0032 撤权语义**：只取消 `queued`/`ready`；`dispatching`/`unknown` 保留并
  按真实结果结算费用，不重发、不伪造成功；无实际监听、模型调用或发送。
- **无在途重叠**：Harness 工作树干净；`agents/tools.py` 最后修改为旧提交
  `34f1c7c`，本侧当前无任何未提交编辑。H03 仍在独立复验，H04 未启动；本侧不会
  编辑该文件。若 H04 后续需要共享工具入口变更，先提出文件/路径范围并等待确认。
- 结论：**`ToolAuthority._grant` 的最小 `SourcePolicies.check_run(...,
  capability=tool)` 接线由 Codex 负责，无当前冲突。** 本节记录时 Harness HEAD
  `ec2712dbc9bc4ad64d6f75978fb4d703207580dc`，UTC
  `2026-11-10T16:08:00Z`。

## 17. SA03 收讫与 SA04 所有权确认（2026-11-10）

已收到并只读审阅 Codex 的 SAV1-03 固定交付（用户已独立验收通过；本节是 Harness
侧收讫与 SA04 范围确认，不代替 peer 复验）：

| 文件 | 读取时间（UTC） | sha256 |
| --- | --- | --- |
| `super-agent-v1/SA03_POLICIES.md` | 2026-11-10T17:27:00Z | `55b2c97bfeeaa0828bf2aeb9f8a32871ccb8301b0f572ebc66e33617fb61c602` |
| `super-agent-v1/HARNESS_SA03_HANDOFF.md` | 2026-11-10T17:27:00Z | `2664b3a6289900503ebbb9629a0e3c6521116ee959b463e2f9f8fa97d17ba612` |
| `super-agent-v1/accept/SAV1-03/accept.json` | 2026-11-10T17:27:00Z | `606ab2f77dfebbe43da31b2278e3a4bcf680433fb688f43f52118eca8e972bb6` |

确认内容：

- SA03 实施 `9f64dc9000879ae84a71640a6d96bdb4668b332e`；验收/HEAD
  `9c44eaf086af811f4a2abd7a6655ec57fa3082fd`；304 tests PASS（SA03 88 项），
  lint/typecheck 208 files PASS；源码 SHA
  `bdd46499b40c3acaa0827c356393583e39ad2f527bb82cf2c0a074d4cce7ca40`。
  `source-policy-v1` 不升级旧许可；角色/策略/来源版本变化 fencing 原 Run/租约/
  待发，保留在途/unknown 与真实费用结算。
- **0033 明确归 Codex**：持久来源入站、复合去重、实体版本/撤回、游标/gap、
  保留与唯一入站所有者事实。Harness 不占用 0033；当前迁移目录止于
  `0029_analysis_token_reservations.sql`，`0030/0031/0032` 均由 Codex 落地。
- **`sources/` 归 Codex**：Gateway/Ingress、加密有界仅重交缓冲及其契约/schema，
  由 Codex 负责。
- **API 归 Codex**：新的 `/api/v3/source-ingress/` 管理/回放路径与 `api.py` 最小
  注册接线。Harness 当前没有 `source-ingress` 路由，不会新增；既有 Harness API
  保持兼容。
- **Engine 归 Codex（最小组合）**：`Engine.__init__` 仅组合使用同一持锁 PG 连接的
  `SourceIngress` 子服务；不新建第二业务状态机或独立写连接。
- **可选 helper 归 Codex**：为复用受信平台 subject/Receive 检查，可在 SA02/03
  source 模块增加非破坏性新 helper；旧接口、重放拒绝、身份与四层权限语义保持。
- 不得改 `ToolAuthority`、Harness 文件、模型调用或预算/费用算法；不导入 Harness
  修复；不触碰本次 H03 活动文件。
- 真实平台 SDK 接线仍属 SA05/06 `NOT_RUN`；SA04 仅受控签名输入与隔离 PG 故障
  验证，无生产来源、付费或发送。
- Harness 侧当前无 Engine/API/迁移写入在途：H03 仍在独立验收，H04 未启动。若
  H04 需要共享 Engine/API/预算或迁移变更，将先提出范围并等待确认。
- 结论：**0033、`sources/` Ingress、`/api/v3/source-ingress/`、`Engine.__init__`
  最小组合与上述非破坏 helper 归 Codex；无所有权冲突。** 本节记录时 Harness
  HEAD `0bbe935cc491e36de1ba3447a0d294ad8727d977`，UTC
  `2026-11-10T17:27:00Z`。

## 18. H03 接收与 H04 交付（2026-11-10）

- H03 已通过独立复验并接收：实现 `a803784`、验收 `0bbe935`，当前 HEAD
  `a04dbde` 仅追加 SA04 所有权文档；25 tests、H00–H02 58 项、lint/mypy 198 files
  全通过；source sha256 `e0f2ced7…`。历史 12 个失败场景与原始证据保留；新增
  双进程 apply_back 一 applied 一 conflict。两项 registry 加固约束（`load`
  不接受绝对路径/穿越，registry 祖先不可跟随 symlink）在 H04 暴露 API/项目进程
  前落实，见 `evidence/harness-v4/HV4-H03/h03-independent-review/`。
- H04 已实施：`1324925`，`harness-capability-lease-v1` +
  `IsolatedExecutionBroker`。三档许可、旧只读不升级、首次可写需显式范围与批准；
  唯一执行入口校验 actor/scope/epoch/命令/预算/有效期；真实 Docker 容器非 root、
  只读根、network none、cap drop、cgroup 限额、按模式只读/读写挂载、凭据不入
  项目进程；超时清理；publish/依赖准备分别拒绝。
- 验收：H04 10 项（含真实容器负例）、H00–H03 83 项通过；ruff/mypy 199 files
  通过；注册 verify `evidence/HV4-H04/20261110T173324Z-e43a9d75/` PASS，source
  sha256 `876d1a24…`。无模型调用、无真实发布/密钥/外发，费用 0；未新增迁移，
  不占用 0033，未改 Codex sources/Ingress/API。
- H04 边界：复用 pinned R03 镜像与本地 Docker；不证明容器逃逸/内核/跨主机；
  真实 MCP/插件、依赖安装、外部发布分别留 H14/H06/Action-Approval-Outbox。
  未改 Engine/API/预算算法；`ExecutionRequest.execution_epoch` 为可选加法字段。

## 19. SA04 Engine.ingest 补充边界确认（2026-11-10）

已读取 Codex 对 SA04 文件范围的补充。`a04dbde` §17 的 0033、`sources/` Ingress、
`/api/v3/source-ingress/`、`Engine.__init__` 组合与 helper 确认继续有效；本节只追加
`Engine.ingest` 的最小只读 guard 所有权。

- **Engine.ingest guard 归 Codex**：在
  `src/mgar_v3/coordination/engine.py::Engine.ingest` 的既有
  `with self.transaction() as conn:` 开头，增加唯一一条
  `self.source_ingress.check_legacy_owner(conn, event)` 委托。
- **检查范围**：仅检查明确配置切换的新 GitHub 来源/仓库，以及已被新管道持久接收的
  delivery；无关联配置、旧 schema 或其他旧入口立即返回。
- **语义约束**：
  - guard 只读，使用同一 Engine 持锁连接与既有事务；不新建事务、连接或业务状态机。
  - 旧 Trigger 已接收的同一 delivery 不再生成新来源输入；反向也不能重复创建。
  - `SourceIngress` 内部维护 owner generation/CAS；切换竞争拒绝旧 generation。
  - 不改原业务插入、Run/Thread 更新、预算、费用、模型调用或 Harness 文件。
- **无在途重叠**：Harness 工作树干净；当前 HEAD `8cf07d2`，本侧 H04 仅改
  `src/mgar_v3/harness/broker.py`、`contracts.py`、`adapters.py` 与 H04 测试，
  未编辑 `coordination/engine.py`，该文件无未提交改动。H03 已接收，H04 等待独立
  验收；本侧不会编辑 `Engine.ingest`。
- 结论：**`Engine.ingest` 的唯一 `check_legacy_owner` 最小只读 guard 由 Codex
  实施，无当前冲突。** 本节记录时 Harness HEAD
  `8cf07d26d8aff9f2bff44581a9ecd87ab589c90b`，UTC
  `2026-11-10T17:36:00Z`。

## 20. H04 独立验收 FAIL 与范围授权修复（2026-11-10）

- 独立验收结论：**FAIL**。真实容器复现 `read_paths=("allowed",)` 可读
  `../protected/secret.txt`、`../.mgar/manifest.json`、`../.git/config`；
  `write_paths=("allowed",)` 可改写三者。原实现整体挂载 workspace，`_authorize`
  只检查 cwd，未将挂载收敛到有效范围，也未与 grant 的读写/命令/时限取交集。
- 第二阻塞：`HarnessToolRegistry` 的 read/list/edit 直接使用 `context.grant`
  访问 workspace，未经过新 lease 的有效期/actor/epoch/预算/撤权检查；Broker
  只比较构造时租约；`budget_check` 可省略且曾默认放行。
- 修复提交：`457acdb796f45e64407772330a4613784f5fdc38`（在 `cb6b281` 基础上
  补齐 symlink scope/realpath 授权；UTC `2026-11-10T18:31:55Z`）。只改 Harness
  文件：`src/mgar_v3/harness/{broker,adapters}.py`、H04 测试、
  CAPABILITY_BROKER 文档，以及 H01/H02 测试辅助显式提供匹配 authority；未编辑
  Codex Engine/sources/Ingress/API/ToolAuthority/预算/迁移。
- 修复内容：
  - Broker 只挂载 `lease ∩ grant` 最窄公共根；读路径只读、写路径读写；未批准
    兄弟目录不可见；`.git`/`.mgar`/`.env` 及自定义保护路径在读写根内用
    `mode=000` tmpfs（目录）或 `/dev/null` 只读 bind（文件/缺失）屏蔽。
  - `CapabilityAuthority` 每次调用读取当前租约，支持 `lease_source`/`replace`；
    校验自 hash、撤权、过期、预算、actor/workspace/repository/epoch/路径/保护
    路径。无外部预算回调时使用租约 `max_calls` 有界计数器，不存在无限放行。
  - 内建 `read_file`/`list_dir`/`apply_edits`/`run_tests` 构造时必须提供
    authority；传入 authority 的路径包含调用路径与 realpath；host symlink
    scope 在创建容器前拒绝，过期/撤权/预算/epoch 在触碰 workspace 或进程前拒绝。
  - grant 命令为空或未列命令在启动前拒绝；网络意图拒绝；超时取
    request/lease/grant 最小值。
- 验收证据：H04 26 项、H00–H03 83 项、ruff/mypy 199 files 通过；注册 verify
  `evidence/HV4-H04/20261110T183139Z-612135f1/` PASS，source sha256
  `363451af…`。原始失败完整保留在
  `evidence/harness-v4/HV4-H04/recheck-20261111/original-failure/`；修复后独立
  探针在 `.../probe/`，覆盖读/写范围矩阵、root 读写保护掩码、旧 grant 交集、
  凭据/宿主 home、entry-only 子进程与残余容器检查。
- **安全门状态**：`security_gate` 仍为 `FAIL`，不得以本地 26 项测试通过宣称
  PASS。停在 H04 等待独立复验，不进入 H05；独立复验通过前不启动后续卡。
- 限制：命令 allowlist 仅约束入口 argv[0]，不约束允许入口启动的子进程；实际
  进程边界由容器提供（uid 65532、只读根、按范围挂载、无网络、cap drop、
  cgroup/ulimit）。不声称覆盖容器逃逸、内核漏洞、跨主机或恶意宿主 OS。

## 20.1 H04 复核发现与 resolver 失败关闭（2026-11-11）

- 复核方式：在 `c707d69` 前，以只读核验重跑 `make verify TASK=HV4-H04`、
  H00–H03、ruff、mypy，并重放原始独立探针与新增对抗探针。
- 结果：注册 verify 27 项（修复后）、H00–H03 83 项、ruff、mypy 199 files 全部
  PASS；原始 `probe.py` 在修复后于启动前即 `HARNESS_COMMAND_DENIED`，不再复现
  越权读写；无残余 H04 容器。
- 新增缺陷（自查发现并修复）：`HarnessToolRegistry._check` 把 realpath 解析器
  当可选回调。若 `WorkspaceProvider` 未实现 `resolve`，`read_file` 可经 allowed
  内的 symlink 读取 `protected/secret.txt`（对抗探针实测读回
  `SYNTHETIC_CANARY_ONLY`）。修复为缺少解析器时四个内建工具直接
  `HARNESS_PATH_DENIED`，不降级放行。
- 提交：`c707d69f6a782118d66847d0cf82bf5b0852933d`（adapters + H04 测试）。
  新增 `test_builtin_tools_fail_closed_without_realpath_resolver`；H04 27 项。
- 证据：`evidence/HV4-H04/20261111T003556Z-ed53ed90/`（注册 verify，source
  sha256 `c2fb38ee…`）；对抗探针
  `evidence/harness-v4/HV4-H04/recheck-20261111/adversarial/`。
- `security_gate` 仍为 `FAIL`：本地复核通过不能替代独立复验；停在 H04，未启动
  H05。随后独立只读复验通过，复验方在 `accept/HV4-H04/accept.json` 落为
  `status=ACCEPTED`、`security_gate=PASS`（HEAD `ca8eecd`，实现 `c707d69`，
  19 项真实容器对抗探针），本地已提交该记录（`87654b5`）。

## 21. HV4-H05 落地：版本化增量编辑（2026-11-11）

- 契约：`FileEdit` 保留完整文件 `content`，新增 `replacements`
  （`old_sha256`/`old_text`/`new_text`/`occurrence`）；文件前像
  `expected_sha256` 与片段哈希双重校验，唯一上下文缺省歧义即拒绝。
- 大文件：预算按输出字节计量；测试在 ≥142KB 文件三处替换，输出 <1KB；同变更
  完整文件协议在相同预算下 `PATCH_LIMIT`；`finish_reason=length` 仍
  `PATCH_TRUNCATED`。
- 原子与候选：多文件先内存应用再一次性落盘，第二个文件失败整组回滚；失败后
  固定底稿保留，`candidate()` 在 revert 后仍可复核最近候选。
- 产物：`PatchReceipt`（前后 sha256 + 真实工作树 Diff sha256）、
  `get_diff()` 从磁盘读取后像、`docs/harness-v4/INCREMENTAL_EDITS.md`。
- 能力：`allow_new_files`、`max_deleted_files`、`allow_rename` 分开关闭；
  重命名源/目标都校验授权与保护路径，目标冲突 `PATCH_CONFLICT`。
- 文本：UTF-8 原样；一致 CRLF 归一化匹配后写回 CRLF；混合换行只做精确匹配。
- 验证：H05 14 项、H00–H04 110 项、ruff、mypy 199 files 通过；注册
  `make verify TASK=HV4-H05` PASS（commit `a757a6b`，source `bf29dccb…`，
  attempt `evidence/HV4-H05/20261111T010251Z-5fdd50fe/`）。
- 边界：H05 不新增执行入口，未触碰 H04 Broker/lease/epoch/预算；已知 H04
  “读根 + 写严格子集 runc create 失败”可用性缺陷在本卡不触发，未修改。
- 状态：`accept/HV4-H05/accept.json` 为 `PENDING_RECHECK`，停在 H05 等待独立
  复验，不启动 H06。

## 22. HV4-H06 落地：命令、依赖、测试与应用进程（2026-11-11）

- 复验核对：`accept/HV4-H05/accept.json` 与
  `evidence/HV4-H05/independent-recheck-20261111T011025Z/` 的 82 项（79 PASS /
  3 INFO）一致；复验方写入的 evidence 路径不准确，已按事实修正指向真实目录并
  本地提交，未改结论。
- 句柄：`CommandSession.run/poll/read_log/wait/cancel/close`；所有容器经
  `IsolatedExecutionBroker.launch`，复用 H04 的 lease/epoch/预算/scope/
  allowlist，没有新增执行入口。句柄只能由创建它的 session 访问。
- 日志：`page_bytes` 分页（默认 16 KiB），缓冲超限丢最旧字节并报告
  `dropped_bytes`；`docker logs` 输出受 lease `max_output_bytes` 限制。
- 超时/取消：deadline 取请求、lease、grant 最小值；到期或取消 `docker rm -f`，
  状态 `timed_out`/`cancelled`，退出码 124/137；`close()` 清理本 session 全部容器。
- 服务：就绪探测在容器网络命名空间内执行；`--network=none` 且不发布端口，
  返回地址仅容器内可达（`host_reachable=False`），无法从宿主访问。
- 适配：Python pytest/unittest、Node/npm、Go；Go 加 `-buildvcs=false`，需要
  `/scratch` tmpfs（`scratch_mb`，默认 64，可执行）。判定要求 exit 0、
  tests_run>0、无失败且非全部跳过；无测试/全跳过/无法解析分别
  `no_tests`/`unverified`，从不冒充通过。
- 依赖：只走 H04 依赖端口（当前一律 `HARNESS_DEPENDENCY_SOURCE_DENIED`），
  返回可操作 `waiting_approval`。R03 无 pytest/node，因此 pytest 与 Node 真实
  运行标 NOT_RUN，只验证检测与等待状态。
- 挂载修复：初版“展开只读祖先 + tmpfs 掩码”被独立复验判 FAIL，见第 23 节；
  当前实现为 scope 单次 bind + 保护路径单独掩码。
- 验证：H06 24 项、H04/H05/H06 65 项、H00–H05 124 项、ruff、mypy 201 files
  通过；注册 `make verify TASK=HV4-H06` PASS（24 tests 0 fail 0 skip）。
- 限制：`/scratch` 可执行；allowlist 只约束入口命令；进程崩溃不自动删除容器；
  宿主不可达服务地址；输出预算不约束模型回复整体体积，live_model_gate 仍
  NOT_RUN。详见 `docs/harness-v4/COMMAND_EXECUTION.md`。
- 状态：`accept/HV4-H06/accept.json` 为 `PENDING_RECHECK`，停在 H06 等待独立
  复验，不启动 H07。

## 23. HV4-H06 独立复验 FAIL 后的修复（2026-11-11）

复验方在 `fd5b57d` 判 FAIL，证据在
`evidence/HV4-H06/independent-recheck-20261111T0145Z/`；本轮按根因修复，仍停在
H06，不启动 H07。

- F1 重复挂载：读根 + 窄写 + 已存在 `.env` 时旧实现把该 target 挂了两次，
  `docker create` 报 `Duplicate mount point`。现在声明的 scope 只 bind 一次
  （读根只读 + 写子集可写），保护路径单独掩码。
- F2 宿主 root 占位：tmpfs 的 target 缺失时由 `docker start` 在宿主 bind 内以
  root:root 0755 创建。现在只读 scope 下缺失路径不掩码（只读父已阻止创建）；
  可写 scope 下由 broker 以工作区用户身份预建 0700 空目录作为 target，随容器
  删除 `rmdir`，Docker 不再改写宿主。
- 文件掩码不再用 `/dev/null`（只读 bind 仍接受写入），改为 broker 自建 mode 000
  空普通文件，读写均 EACCES。
- 契约内非阻断项：lease 截断显式 `truncated`/`log_truncated`；租约撤销/过期/
  换 epoch 终止在跑命令（`revoked`、137）；`run_tests` 先探测 pytest/node，
  缺失返回 `waiting_approval`。
- 新增 8 项结构用例：保护路径存在/缺失两种形态的读根+窄写、可写根宿主清单
  逐项不变、撤销/过期/epoch 替换、lease 截断、pytest 等待。
- 验证：H06 24 项、H04/H05/H06 65 项、H00–H05 124 项、ruff、mypy 201 files、
  `make verify TASK=HV4-H06`（24 tests，source sha256 `97f69ffe…`）全部 PASS；
  原始失败证据保留在原复验目录。
- 仍存的诚实限制：可写根下缺失保护路径在容器运行期存在用户属主 0700 空目录，
  强杀可能残留（可 rmdir）；`/scratch` 可执行；宿主不可达服务地址；pytest/Node
  真实运行仍 NOT_RUN。

## 24. Codex SAV1-05 接口所有权确认（2026-11-11）

仅确认接口与编号所有权，不涉及 Harness 卡推进或复验；本节不表示 SAV1-05 已验收。

- 核对基准：`magr-super-agent` HEAD 为
  `ec92d145f2ad6188fca7e11a7fa913dc9d9257af`（工作树干净，与用户给定的 SA04
  固定交付一致）；`magr-harness` 本节写入前的 HEAD 为
  `a43c52615324e7af8fe7c597813e1a7d17b2255a`。
- 迁移编号实测：`magr-super-agent/v3/migrations` 最新为
  `0033_source_ingress.sql`，`magr-harness/v3/migrations` 最新为
  `0029_analysis_token_reservations.sql`；两树均无 `0034` 迁移文件，代码/schema/
  文档中也没有对 `0034` 的占用性引用（仅 SA04 状态文档注明“0034 未预占”）。
  **确认 0034 可用，并归 Codex 在 SAV1-05 独占使用。**
- 范围归属确认（Codex 独占，Harness 侧不新增同名迁移或同类入口）：
  - 迁移 `0034`：GitHub 安装设置/CAS、只读仓库 snapshot grants、最小审计；
  - `sources/github*` 适配；
  - `api.py` 仅做 GitHub 管理注册与旧 webhook 的显式单 owner 分流；
  - `Engine` 不改，或仅做组合初始化。
- 边界确认：上述工作不编辑、不导入预算、`ToolAuthority`、任何 Harness 文件或
  Harness 活动树；不开真实平台、不做外发、不产生付费调用。`magr-harness` 侧
  H06 仍为 `PENDING_RECHECK`，不因本确认启动 H07 或改动挂载/租约实现。
- 若后续需要 Harness 提供能力或跨树接线，另行提出并单独确认，不以本节代替。

## 25. HV4-H06 第二次复验 FAIL 后的 F3 修复（2026-11-11）

第二次独立复验只剩 F3 一项，F1/F2 已被复验方用自己的探针确认修好，本轮未回退
挂载与占位行为。

- F3：截断只报布尔，`LogPage.total_bytes` 用 `dropped + 保留长度` 推算，削尾时
  仍等于 `max_output_bytes`（20001/2048 → 2048），调用方无法区分削尾与本来
  就这么短；用例还把 2048 固化成期望值。
- 修复：`_Record.total_bytes` 保存 broker 返回的真实总量，`LogPage.total_bytes`
  与 `CommandHandle.log_bytes` 直接使用它；`eof` 表示保留字节已交付完，削尾时
  `total_bytes > next_offset` 由 `truncated` 说明。原断言改为 20001，新增
  `test_exact_length_log_is_not_reported_as_truncated`（2048/2048 不误报）。
- 验证：H06 25 项、H04/H05/H06 66 项、H00–H05 124 项、ruff、mypy 201 files、
  `make verify TASK=HV4-H06`（25 tests，source sha256 `82209b97…`）全部 PASS；
  原始失败证据保留在复验目录，未弱化既有测试。
- 残余限制如实承接：SIGKILL 时容器与 0700 占位目录同时残留；pytest/node 真实
  执行仍 NOT_RUN；`exec_probe` 接受任意容器名；allowlist 只比对 argv[0]
  basename；仓库内代码可伪造测试摘要。
- 状态：仍 `PENDING_RECHECK`，停在 H06 等复验，不启动 H07。


## 26. HV4-H06 第三轮复验 PASS 与 HV4-H07 实现（2026-11-11）

H06 第三轮独立复验 PASS，`accept.json` 已更新为 `ACCEPTED` /
`PASS_AFTER_INDEPENDENT_RECHECK` / `security_gate=PASS`，并写入
`security_gate_recheck`（复验方、commit、门计数、F3 重放、只读变异结论、残余
限制、证据 `evidence/HV4-H06/independent-recheck3-20261111T1100Z/`）。文档改动
已在本地提交（不推送），工作树干净后启动 H07。

H07 落地：新增 `src/mgar_v3/harness/coding_loop.py`，把计划、编辑、执行、验证
与反馈放进一个循环，只组合 H01/H02 冻结端口与 H04 Broker / H05 工作区 / H06
命令适配，没有新增执行入口。

- 计划与清单：`checkpoint_tree` 绑定验收项；树不一致 `HARNESS_PLAN_STALE`；
  模型提案只能追加 behavior/regression/build，不能替换或降低 required 项。
- 类型策略：inspect 只读、bugfix 必须复现、feature 要新行为+回归、refactor 要
  签名+回归、run_app 要就绪+清理；legacy 策略保留 3 次上限，resource 策略在
  有实质进展时可超过。
- 进展：只看工作树变化与新命令证据；补丁往返、同错重跑、文字改写、重复空搜索
  不重置保护；连续两轮无进展 `blocked` 并保留 checkpoint/未完成项/所缺信息。
- 弱化守卫：删除测试文件、测试函数或 `assert*` 数量下降、`tests_run` 下降均
  不能完成。
- 结果：`CodingResult` 绑定最终树、diff、receipts、checklist、attempts、
  feedback、run graph 与 checkpoint；只有 verified 且无失败项才是 completed。
- 验证：H07 27 项、H04–H07 93 项、H00–H05 124 项回归、ruff、mypy 202 files、
  `make verify TASK=HV4-H07`（27 tests，exit 0）全部 PASS；证据
  `evidence/HV4-H07/20261111T031717Z-eb4e3829/` 与
  `evidence/HV4-H07/round1-20261111T0317Z/`。
- 边界：`ApplicationPort` 只是 H06 容器生命周期的接缝，H07 测试不启动容器；
  旧 agent（`repository_analysis/agent.py`、`repair/repair_agent.py`、
  `workbench/orchestrator.py`）未改写，接线归 H08；未新增发布、网络或执行入口，
  未使用真实凭据。
- 承接的残余限制（不因本卡变绿而弱化）：SIGKILL 时容器与 broker 预建的 0700
  空占位目录残留（正常路径已清理）；pytest/node 真实执行仍 NOT_RUN（镜像缺
  运行时，仅有 waiting_approval 路径）；`total_bytes` 依赖 docker logs 单调
  计数、轮转后不可重建；`exec_probe` 接受任意容器名；allowlist 只比对 argv[0]
  basename；仓库内代码可伪造测试摘要；H05 的六条限制继续适用；三个 live 门仍
  NOT_RUN。H07 另加：弱化检测是 AST 计数启发式，测试改名/拆分/辅助断言可能
  误判。
- 状态：`PENDING_RECHECK`，停在 H07 等独立复验，不启动 H08。
## 27. HV4-H07 复验后修复（P-13/P-15）与 PATCH_TRUNCATED 核对（2026-11-11）

复验方已将 `accept/HV4-H07/accept.json` 更新为 `ACCEPTED` /
`PASS_AFTER_INDEPENDENT_RECHECK` / `security_gate=PASS`，并如实记录两个 gap。
本次只修 P-13/P-15，不改复验结论，仍停在 H07 等复核，不启动 H08。

- 修复：`coding_loop._test_strength` 的断言站点改为**按文件汇总**——裸
  `assert`（含 `assert x, "msg"`）与 `self.assert*(...)` 各计 1。删裸 assert
  会让总数下降从而不能 completed；把 `self.assertEqual` 抽到使用裸 assert 的
  辅助函数时总数 1→1，不再误判 weakened。没有对某一种写法开例外。
- 新增双向用例：`test_deleting_a_bare_assert_cannot_complete`、
  `test_extracting_an_assertion_into_a_helper_is_not_weakening`。
- 变异确认：把 `ast.Assert` 计数两行从 `/tmp` 副本移除（`-o pythonpath`
  强制导入副本，已记录模块路径与 diff），两条新用例均失败，说明用例能区分
  新旧实现、不是空转。证据 `evidence/HV4-H07/fix-round-20261111T0340Z/`。
- 验证：H07 29 passed；H04–H07 95 passed；H00–H07 178 passed；ruff 通过；
  mypy 202 files；`make verify TASK=HV4-H07` PASS（29 tests，attempt
  `evidence/HV4-H07/20261111T034509Z-1b1811e8/`）。

### PATCH_TRUNCATED 与 E02 的核对结论

- 产生路径只有一处：`repair/providers/structured.py` 的
  `StructuredPatchProvider.propose`，条件 `finish_reason == "length"`；H07
  统一循环不经过该 provider，也不产生该错误码（截断的模型回复在循环里是
  `incomplete` / `model_length`）。
- H05 之后只有**合成**触发：`test_large_file_provider_plan_is_small_and_truncation_is_rejected`
  用 MockModel 返回 `finish_reason="length"`。没有真实触发。
- 唯一真实触发是 V35-E02（H05 之前的旧整文件协议）：`docs/v35/accept/V35-E02/accept.json`
  记录 4 臂 12 次真实付费调用全部 `fail:PATCH_TRUNCATED`，`live_agent_gate=FAIL`，
  原因是 8192 输出上限无法返回完整 142KB 文件；原始 `evidence/v35` 目录被
  gitignore 且不在当前 checkout，只能复核已入库记录。
- E02 **尚未被绕开**：H05 的增量替换在 `PatchWorktree` 层可用，但 H06/H07 给
  模型的唯一写工具 `apply_edits` 只接受整文件 `WorkspaceEdit.content`，没有
  replacements 入口；统一循环的可执行写路径仍可能撞输出上限，只是所有 H07
  用例都是小文件、live 未跑而未触发。增量替换接入 H07 工具或 adapter 流式
  方案是后续接线项，不在本卡范围。
- 证据分级：真实文件系统执行（H05 worktree 用例）≠ 真实模型运行（仅 V35-E02，
  harness v4 仍无）；PATCH_TRUNCATED 与 E02 绕开目前只有契约/合成证据。仓库
  fixture 为 3100 行（约 130KB），复验方 186KB/944B/216B 的探针数字未在本仓库
  记录中保存，本次未复现。
- 残余：`pytest.raises`/`with` 上下文断言仍不计站点；同一断言抽成多处调用的
  辅助函数仍可能 fail-closed 误判；非 Python 测试只靠 `tests_run` 与文件存在性。

## 28. HV4-H08 写路径落地（2026-11-11）

按 H08 卡片与 H07 `accept.json` 的 `patch_truncated_followup` 前置条件，先把
"模型可用写工具"接到 H05 增量替换：`WorkspaceEdit` 新增互斥的
`replacements`，`LocalWorkspace` 与 `TaskWorkspace` 共用
`harness/incremental.py` 委托 H05 `apply_text_replacements`，错误码
`PATCH_STALE` / `PATCH_HASH_MISMATCH` / `PATCH_AMBIGUOUS` / `PATCH_LIMIT`；
整文件 `content` 路径与既有错误码未改。输出字节按模型实际生成的
`content`/`new_text` 计量，默认上限 65,536 字节，工具结果带
`protocol` 与 `output_bytes`。

随后补齐卡片的其余部分：

- `harness/service.py`：`HarnessCodingService` 注册本地项目、创建任务、驱动
  H07 `CodingLoop`；`WorkspaceBoundBroker` 只把回执树摘要重绑到 H05 工作区，
  执行仍走 H06 `IsolatedExecutionBroker` + `CommandSession`（lease、epoch、
  命令 allowlist、输出字节与预算均未绕过）。`CodingToolRegistry` 增加
  `run_tests`（只接受受信 argv，描述里直接给出该 argv）。
- 真实模型可用性：`read_file` 结果追加 `<<mgar-read-file sha256=… tree=…>>`
  注记，模型据此填 `expected_sha256`/`expected_tree`；`old_sha256` 变为可选，
  缺省由 harness 固定为 `sha256(old_text)`，显式错误哈希仍拒绝。
- `api.py`：`/api/v3/harness/profile|projects|tasks` 与
  `tasks/{id}`、`/diff`、`/patch`、`/artifacts`；全部要求既有认证。
- `web/agent.*`：首页新增“编码任务”模式（项目下拉/注册、任务类型、目标、
  受控适配器参数、批准复选框），结果区展示文件变更、命令/测试、完整 Diff
  与补丁下载；分析入口保留。
- `settings.py`：新增 `harness_root`、`harness_image_lock`。

真实模型证据（受控单次，非基准）：34,178 字节 `big.py`，真实
`deepseek-chat` 在 H06 容器内完成单行修改；6 次模型调用、8 次工具调用、
模型输出 922 字节、费用 17,182 micros；基线失败 → 修改 → 测试通过，Diff
仅一行，未出现 `PATCH_LIMIT`/`PATCH_TRUNCATED`。

收口证据（2026-11-11）：`tests/test_harness_v4_h08.py` 19 passed；
H04–H08 114 passed；H00–H08 197 passed；`make lint`/`make typecheck` 通过；
`make verify TASK=HV4-H08` PASS（live 门 NOT_RUN——该目标从不发起
付费调用）。浏览器端到端在一次性数据库与临时 harness 根目录上跑通：登录 →
切换编码 → 选项目 → 修复缺陷 → 结果徽标“完成 · 确定性适配器”、Diff 为
`- return a - b` / `+ return a + b`、命令先 failed 后 passed、补丁链接可见。

独立复验修复（2026-11-11，仍停在待复验）：

- F1 `changes[].before_sha256` 双重哈希：`CodingWorkspace.diff()` 原先把
  `baseline.json` 的 path→sha256 映射值再喂给 `sha256_text()`。现直接上报
  基线摘要；删除按 H07 `LocalWorkspace` 语义上报 `modified` +
  `after_sha256=sha256("")`，新增上报 `before_sha256=null`。新增用例覆盖
  修改/新增/删除/未修改四类，只读变异复现 `e1a894… -> 78375a…` 会让断言失败。
- F2 `task_id` 未校验：`get_task()`/`patch_path()` 在拼接路径前
  `fullmatch ^h08-[0-9a-f]{12}$`，`%2e%2e`、编码斜杠、空/超长/非十六进制
  一律 404 `HARNESS_TASK_UNKNOWN`，负例已入库。
- 证据更正：浏览器端到端计数由误写的 16/16 对齐为 15/15，并在 accept 中
  保留 15 项逐项检查明细与证据强度说明。
- 费用记录：复验方在测“无付费授权应拒绝”时误触发 2 次真实付费调用
  （851 micros，因缺 target 最终 denied）；付费门逻辑未改，仅如实记账。

迁移说明：旧的 Workbench/分析入口与 API 未改；新入口只需为部署补
`MGAR_V3_HARNESS_ROOT`、`MGAR_V3_HARNESS_IMAGE_LOCK` 两个可选配置，默认值
沿用 `.local/harness-v4` 与 `.local/r03/sandbox-lock.json`。写任务必须带
`approved=true`；`mode=live` 还需 `allow_paid` 与模型凭据，两者缺一返回
`HARNESS_LIVE_MODEL_UNAVAILABLE`，凭据不落盘、不进日志、不回传前端。

残余限制：一次受控真实调用只证明该模型/该文件规模下的增量写路径可用，不
等于 H12 的冻结任务集与付费模型验收；整文件重写路径在真实模型下未做规模
验证；SSE 事件流、运行中交互属于 H09；GitHub 发布不在本卡范围。

## 29. Codex SAV1-06 飞书来源接口所有权确认（2026-11-11）

本节仅确认 SAV1-06 拟新增文件、编号与接口的唯一归属，不代表 SA06 已实现或
已验收，也不推进 Harness 卡。依据为用户转来的 Codex 协调请求与本地只读实测；
核对时间 UTC `2026-11-11T06:41:33Z`。

### 29.1 核对基准

- `magr-super-agent` HEAD
  `9fb6593cbd12a511cf089a5ddebcdb98f408f253`，工作树干净；该 HEAD 为 SA05
  验收记录，实施提交 `cf7c176` 在同一历史中。SA05 的 566 全量、146 专项、
  13 独立探针、lint/typecheck 与源码 SHA `890e475b…` 为用户独立放行结论；
  Harness 本轮未重跑，不替代其独立验收，旧 FAIL 与 NOT_RUN 保留。
- `magr-harness` HEAD
  `bf558b56017dbad2d5165ee534e035e3be5489ee`，工作树干净；H08 已接收，
  H09 尚未实现。

### 29.2 迁移编号

- `magr-super-agent/v3/migrations` 最新为 `0034_github_app.sql`；
  `magr-harness/v3/migrations` 最新为 `0029_analysis_token_reservations.sql`。
- 两树均无 `0035_` 迁移文件，也没有代码或文档对 `0035` 的占用性引用；
  SA05 验收历史只写明 `0035 is NOT_RESERVED`。
- **确认 0035 可用，归 Codex 在 SAV1-06 独占使用**：
  `0035_feishu_source.sql`。Harness 不新增 0035、不复制该迁移；若未来 Harness
  确需共享迁移，从 0036 起另行提出并先对齐，不占用 0035。

### 29.3 文件与 schema 归属

- `sources/` 自第 17 节起归 Codex。实测
  `magr-super-agent/v3/src/mgar_v3/sources/` 现有 `github*`、`identity*`、
  `ingress*` 等模块，没有任何 `feishu*` 文件；`magr-harness` 没有 `sources/`
  包。**`sources/feishu*` 归 Codex。**
- 两树迁移只创建 `knowledge`、`checkpoints` schema；`app` 下已有
  `source_*`/`github_*` 表，没有任何 `feishu` 表、类型或函数。
  **Feishu 专用表（`app.feishu_*` 或 Codex 在 0035 内定义的同域对象）归 Codex；
  Harness 无同名对象冲突。**

### 29.4 API 与 api.py 接线

- 两树均无 `/api/v3/feishu-sources`、`/api/v3/webhooks/feishu` 路由或字符串。
- `magr-super-agent/v3/src/mgar_v3/api.py` 现有旧 GitHub webhook
  `/api/v3/webhooks/github`（约 1166 行）与默认禁用的
  `github_runtime: GitHubRuntime | None = None` 注入（约 110 行），
  `register_github_sources(...)` 在约 1269 行注册。
- **确认 Codex 独占以下增量**：
  - 新增 `/api/v3/feishu-sources` 管理路由；
  - 可选新增 `/api/v3/webhooks/feishu`；
  - `api.py` 仅增加默认禁用的 Feishu Runtime 注入/注册，沿用
    `github_runtime` 的缺省 `None` 模式，复用现有登录与 CSRF。
- 明确不改旧 GitHub 分支、Engine、预算、`ToolAuthority` 或任何 Harness 文件
  （`src/mgar_v3/harness/**`、`tests/test_harness_v4_h0*`、
  `docs/harness-v4/**`）。Harness 侧不会编辑
  `magr-super-agent/v3/src/mgar_v3/api.py`。

### 29.5 状态与写者边界

- SA06 仍使用原有 Engine 同一 PG 连接与既有事务边界；不新建第二业务状态机、
  第二写连接或独立 Worker 状态。
- 不启用真实监听、凭据、模型调用、外发或发布；相关 live 门保持 `NOT_RUN`，
  不把排队、本地构造或录制样本视为真实渠道送达。
- 双方不导入、不修改对方活动树；需要集成时按固定提交评审并另行确认。

结论：**0035、`sources/feishu*`、Feishu 专用表、`/api/v3/feishu-sources`、
可选 `/api/v3/webhooks/feishu` 与 `api.py` 默认禁用 Runtime 接线归 Codex；
Harness 无当前冲突。** 本节不改变任何 H 卡验收结论，也不启动 H10。

## 30. SAV1-07 共享预算接口所有权确认（2026-11-11）

本节只响应 Codex 线程 `01a1ca35-486d-7d10-b50b-38abfc740d35` 的接口协调，
不认证 SA06/SA07 的验收结论，不在另一方活动树写文件或导入提交。当前 Harness
可信起点 `8f87b0a` 已由用户确认 H11 scoped independent PASS，并明确推进 H12。

确认 **Codex 唯一负责** `0036_super_agent_budgets.sql`、`app.sa_budget_*`、
`super_agent/budget*`、`/api/v3/project-budgets`，及 SA07 所需原 Engine
`__init__` / `reserve_call` / `settle_call` 和 `routing/budget.py` 的最小接线。
同样确认 `repository_analysis/store.py::RepositoryAnalysisStore.reserve_repository_call`
的最小同事务共享预算 hook 由 Codex 唯一实施；该路径也直接写原 usage_calls，不能漏控。
未配置 SA 新预算时保留旧行为，不改旧定价、grant、结算或验收验证的授权语义。
本树未占用该迁移、表名或路由；H12 不并行编辑上述 Engine 方法、API 或 ToolAuthority。
这些接线须保留原 held Engine PG 连接、事务与旧授权/usage 真值；不能另建业务写者。

Harness H12 的活动所有权是 `harness/store.py`、`harness/ledger.py`、内部
EvaluationProfile、编码循环/工作区和评测器。新迁移调整为 **0037_harness_evaluation_budget.sql**，
避免占用 SA 已有0031–0035与本次0036；它新增 `app.harness_experiments` 和封树元数据，
复用既有 `authorization_budgets` / `budgets` / `usage_calls`，只对 H12 group profile
扩展调用次数约束。其共享DDL与SA预算接线须在固定交付后单独集成复验。

注意 Harness 的 PersistentBudgetLedger 在 Engine 同一持锁连接中直接预占原账本，
不调用 `Engine.reserve_call`；SA07 仅添加该方法的 hook 不会自动覆盖 Harness。
SA07 不编辑/导入 Harness 来补此接线；如需共同项目额度，另行按稳定契约协调。
H11 已提交的 `0030_harness_recovery.sql` 不在本轮重命名，后续与 SA 历史迁移的
编号/安装校验和由集成方核对，禁止改写已安装迁移字节。

H12 的真实调用授权只属于本轮明确的12美元共享评测预算；本节不向SA授予付费、
真实渠道或发布权限，不推动任何 SA/H 卡状态。消息送达与固定代码集成仍分开记录。

## 31. SAV1-08 Fast Path 共享范围确认（2026-11-12）

本节依据用户代转的 SA08 协调请求确认所有权。Harness 已只读核对
`/home/developer/magr-super-agent/v3/docs/super-agent-v1/sa08-proposal/` 的 SQL、
`shared-wiring.patch`、`ownership-request.txt` 及 `SA08_FAST_PATH.md`。
本次仅确认共享范围，不作为 SA08 实施或验收结论。

1. **SA 侧 Codex 独占** `0038_super_agent_fast_path.sql`、`app.sa_fast_path_*`
   和 `super_agent/fast_path*.py`。Harness 当前无 0038 文件、同名数据库对象、
   模块或路由占用，未发现编号或文件冲突。`0037_harness_evaluation_budget.sql`
   继续归 Harness；SA 副本不导入、不改名、不伪造占位，不改写历史迁移。
2. **SA 副本内的最小接线归 SA 侧 Codex**：`Engine.__init__` 组合 FastPath
   子服务；`api.py` 注册 `/api/v3/source-fast-path`；`SourceIngress` 对本次
   新 canonical input 增加观察 hook。Harness 不并行修改这些方法、路由或
   hook，也不编辑 `magr-super-agent` 活动树。当前 H12 环境修复位于 Harness、
   工具、沙箱与评测路径，不涉及上述接线。
3. 复用原 Engine mutex/advisory lock 下的持锁 PG 连接、已有事务和
   `SharedBudgets.quota` 的 candidate 配额入口；不新增业务写者或独立事务状态机。
   不改变预算/结算算法、`ToolAuthority`、来源事件 DTO 或 Harness 实现。
   hook 保留原 Receive、身份/版本水位、删除/歧义、持久 ACK 和回滚语义。
4. SA08 仅产生确定性过滤日志与 shadow 候选；`would_*` 不执行真实动作。
   不创建真实 Worker/Ticket/Run，不调用模型、不发送消息、不发布；本协调
   不改变 SA 或 Harness/H12 的验收状态及真实实验授权。

核对版本：Harness HEAD `ae0cf36`；提案 SQL SHA256
`7f03acdf8e428a0cb621ab555d3b04d44f7dc76127f4e5eedf0c23a698466eb6`；
接线补丁 SHA256
`3269bc6c33ba572c9057a345bba0c1c73119be772686a2be7af1ecb0dc1422f7`。
Harness 0037 当前 SHA256
`f585ba1c2a1d164652585bdc876a0918e60b6fe07432df5d2b20e156392254a1`。
历史 0030 与 SA 历史编号的安装校验和协调仍按第 30 节处理；本节不重命名历史文件。

按用户要求，仅将本节确认本地提交；不夹带当前未提交的 H12 修复，不推送，
不为本次文档协调额外运行测试或付费实验。

## 32. SAV1-09 Source Router 共享范围确认（2026-11-12）

本节响应用户代转的
`/home/developer/magr-super-agent/v3/docs/super-agent-v1/sa09-proposal/ownership-request-draft.txt`。
Harness 已只读核对该请求、0039 SQL、四文件接线补丁及其元数据。
这是 **SA09 的新范围确认**；第31节与 `1e397ab` 仅确认SA08，不能替代本节。
本次不认证私有模块、共享补丁或SA09验收结果，也不授予真实运行或付费许可。

### 32.1 迁移、模块与契约归属

确认 **SA侧Codex独占** `0039_super_agent_source_eval.sql`、新增
`app.sa_source_eval_*` 表/索引/函数/触发器、`super_agent/source_eval*.py`，以及
`source-router-proposal/decision/snapshot-v1` 私有契约和无正文事实。
Harness当前无0039、同名对象/模块/路由占用，**未发现编号或文件所有权冲突**。
0037仍归Harness；SA不导入、不改名、不伪造占位。SA原0001–0036与已确认0038、
Harness历史迁移及安装校验和均保留，既有0030跨副本历史事项仍按第30节处理。

Source原始批次使用准确的 `schema_version=source-router-snapshot-v1`，
legacy兼容外壳仅有空 `threads=[]`；它不提供旧Ticket/Thread/Run权限。
永久快照/事实只保存canonical ID、hash、revision、授权快照及严格枚举；正文和
原始模型响应不能进入普通CAS或新事实表。当前授权正文只在有界内存中组装。

### 32.2 SA副本中的共享方法范围

- `routing/store.py`：`Routing.prepare` 的 prior/failed/cached 查询排除上述固定tag；
  legacy `wrap`、`_validate`、`commit` 拒绝该tag，`save`/`failure`不能改写Source批次。
  `commit`必须在写固定模板CAS前先拒绝Source批次；`_rows`仍只查询原
  Ticket/Mandate/Thread/Run，不接受snapshot且保持原样。
- `routing/worker.py`：`RouterWorker.__init__` 增加受信的pipeline与decision_decoder
  成对可选注入，`tick`沿原 prepare→provider→wrap/save→commit/failure 分段委托。
  缺省继续使用 `engine.routing` 与 `RouterDecision.model_validate`，保留原
  `RouterProvider`导出/DTO。结构协议的Any仅传递待校验数据，不授予模型权限；
  新Source pipeline与decoder必须匹配并严格复核私有enum/internal ID及当前授权。
- `Engine.__init__`：仅import/组合SourceEval子服务。
- `api.py`：仅import/注册 `/api/v3/source-eval` 六个管理接口，复用现有cookie、
  Origin/CSRF、事务内会话复查及no-store；不修改原方法或路由语义。

以上范围由SA侧Codex在 `magr-super-agent` 副本独占实施。Harness不并行修改这些
共享方法/接线，不编辑SA活动树；当前Harness启动配置修复不涉及这些文件。
未来跨副本集成另按固定提交、接口与实际验证协调，不导入当前未提交修改。

### 32.3 预算、隐私与执行边界

复用原Engine持锁PG连接/事务及原 `router_batches/router_calls`、
`RouterBudget.reserve/settle` 和原SA预算归属；不新建业务写者或费用账本。
Source已知调用仅以known cost/usage、`result=None`结算，sanitized自有结果与结算
必须在同一原Engine外层事务原子提交；不得伪造旧Proposals给get/settle。
不调用 `RouterBudget.response` 永久保存原响应，不用旧get解析新DTO。
unknown保留原预占、原输入/组的调用唯一性且不盲重发；known-invalid费用如实结算，
不得借新batch、配置、Run或重试绕过原调用身份。当前预算/model_input/来源/身份/
策略/精确目标/revision/epoch必须在发送前和提交时重新核验。

`Engine.settle_call`、`RouterBudget.settle`、
`RepositoryAnalysisStore.settle_call`三处财务结算AST、原预算算法、ToolAuthority、
旧Source/Trigger DTO及历史迁移不在本次修改范围。H02只复用公开SSEDecoder、
ModelCapabilities、estimate_tokens；独立Source请求不伪造TaskBinding/Run/Workspace，
不导入H12。旧Router财务指标如包含Source调用，须标明共享Router总量，不能当旧业务Worker数。

模型无Shell/工具/业务写/群发能力；shadow只记录would_*，不创建真实业务
Worker/Ticket/Run，不发送或发布。真实平台、付费、外发、发布和长期门仍NOT_RUN；
本确认不继承H12的12美元评测授权，不改变任何SA/Harness验收状态。

### 32.4 固定核对版本

Harness核对起点：`05fa01cd3f0f29036bb430d04bbd2cde4268ec83`。
请求SHA256：`db67ee69419239543c6fae9f338732dcdea0f02b04111a07b96b01551711a2fa`。
当前四文件补丁SHA256：`73b28025f6b92d415aa2347c28e1c8959ee749c966f6e8d5e0a71ff88c445e1b`。
0039 SQL SHA256：`8b18840590c7841022d7985d66904cd580aee916777cadc9f8baca3cb1837669`。

附带 `shared-risk-review.json` 仍引用旧补丁hash `02206b85…`，不是当前73b28025补丁的
复验凭证；SA侧应更新该元数据。本所有权确认明确绑定上列实际SQL/补丁hash，
该元数据差异不构成编号/文件所有权冲突，也不据此宣称代码已验收。
本次仅本地提交该交接确认，不安装0039、不应用SA补丁、不推送或启动真实实验。

## 33. HV4-PRODUCT 自然语言入口整合范围（2026-11-12）

用户已要求实施自然语言 Harness 产品闭环，本节记录其与第31/32节的边界。
新增 `0040_harness_product.sql` 归 Harness，包含项目授权、会话/轮次、运行环境、
本地回写与预览元数据，全部使用原 Engine 持锁 PG 连接和原费用账本。
只读核对相邻 SA 活动树时，其迁移止于0039，未发现0040占用；0038/0039保持SA归属，
不导入、不重命名、不修改历史迁移。未来新迁移继续核对最新编号后安排。

Harness 本轮只在本副本的 `api.py` 组合独立产品子服务并注册 `/api/v3/agent`，
不修改 `Engine.__init__`、Router、SA08/SA09 子服务、SourceIngress hook、来源 DTO、
SA 私有模块或其活动工作树；财务结算方法和 ToolAuthority 不改。
共享网页新保存的 Flash/Pro 模型授权补齐当前价格元数据，旧授权/评分历史保留。

本轮覆盖首页、持续会话、项目权限与累计预算、工具/准备/预览、规则/只读扩展和
安全本地交付。产品用可信构造参数支持有效可写范围内的安全原生链接；默认 Broker
和冻结评测仍保留原有严格策略。原始工程 API 与 H12 冻结独立判定继续使用原协议。

完整任务与工程证据见 [产品任务卡](HARNESS_PRODUCT_TASK.md) 和
`accept/HV4-PRODUCT/accept.json`。受控模型、真实 PG/Docker 与浏览器验收不构成
H12 真实质量门放行；本轮真实付费调用、外部消息/发布和推送均为0。

## 34. SAV1-10 Source Correlations 共享范围确认（2026-11-13）

本节响应用户代转的
`/home/developer/magr-super-agent/v3/docs/super-agent-v1/sa10-proposal/ownership-request.txt`。
Harness已只读核对请求、0041 SQL草案、两文件接线补丁及当前编号/命名空间；
本节是SA10的新所有权确认，第32节SA09和第33节产品范围不能替代本节。
确认依据为相邻副本`AGENTS.md`及`COORDINATION.md`的共享修改前协调要求。
本节只确认所有权、接口与编号，不认证SA09修复或SA10实现/验收结果。

### 34.1 迁移与私有范围

确认**SA侧Codex在`magr-super-agent`副本独占实施**
`0041_source_correlations.sql`，以及下列五张新表及其专属索引、函数和触发器：

- `app.sa_correlation_resources`
- `app.sa_task_correlations`
- `app.sa_task_correlation_versions`
- `app.sa_task_correlation_claims`
- `app.sa_thread_link_inputs`

`super_agent/source_correlation*.py`及对应新schema/config/tests归SA侧独占；
`source-correlation-v1`与`source-correlation-snapshot-v1`是独立新契约，不扩大旧DTO。
Harness当前未占用0041、上述对象、模块或路由，**未发现编号、接口或文件所有权冲突**。
`0040_harness_product.sql`和0037继续归Harness，0038/0039继续归SA；
SA不导入、改名、改写或伪造0037/0040占位，也不导入H12或对方活动树。
原0001–0039历史SQL保持原样；既有0030跨副本历史事项仍按第30节处理。

新表沿用原canonical input、Source/project和Thread→Ticket→Run作用域复合关联；
不FK到可TTL清理的`source_inbox`，不修改旧表或SourceIngress purge。
无正文事实与旧关系按固定revision保留；topic允许多对多，不能作为单Task唯一键，
旧输入边不会随head更新而静默换到新目标。

### 34.2 SA副本中的最小共享接线及HTTP接口

确认下列接线由SA侧Codex独占，Harness不并行修改这些新增接线：

- `Engine.__init__`仅import并组合`SourceCorrelations(self)`。
- `api.py`仅import并调用`register_source_correlations(app, authority, browser_user)`。

SA专属`/api/v3/source-correlations`前缀确认以下六个管理接口：
`POST /resolve`、`POST /links`、`POST /links/{correlation_id}/correct`、
`POST /links/{correlation_id}/remove`、`GET /links/{correlation_id}`、
`GET /inputs/{input_id}/links`。沿用当前主人cookie、Origin/CSRF、事务内会话复核
及no-store；HTTP不接收正文、资源证明、身份、付费、执行或factory字段。

不修改Engine原业务方法、Router/Routing/Worker、SA09v1快照/tag/模型/预算hash或
I01发送栅栏。Harness的`/api/v3/agent`产品注册与上下文/UI修复不占用本次路径。
两副本未来集成仍须审查固定提交及实际差异；本节不要求SA导入Harness产品/H12提交。

### 34.3 关联权限与真实Task去重端口

资源/代码证明仅从当前授权的原GitHub加密canonical input读取；名称、标题、#编号、
模型建议和URL本身不授予身份或权限。跨来源别名由管理员明确选择当前可读两侧输入
并签署，原输入/决定/关系保留。SourceDecision仅作建议。PR/head/base/CI attempt
分别绑定，缺树或不匹配时不复用旧验证/审批，不改原Mandate/Run目标。
纠正通过CAS并核对旧Ticket全部attempt、执行、费用、unknown及副作用事实，
已执行目标不得后台换绑。

确认内部端口`claim_or_create(cookie, input_ids, resource_key, create_target=None)`：
默认无factory，HTTP不能提交或选择factory；唯一资源claim占位仅事务内可见，
获胜后才调用受信`factory(original_connection)`，重复请求不再次调用factory。
实际Thread/Ticket/Run/Mandate后验核验与claim/版本/链接沿原Engine持锁PG连接，
在同一事务提交或回滚；延迟约束禁止缺完整target的占位提交。
受信factory只复用原Engine已有创建/关联入口及原执行模式/许可，不新增业务写者、
IM Issue、身份或Mandate授权，不在事务内执行网络请求。当前草案实现的factory仅允许
原Run/Mandate均为fixture；本确认不扩大为live工厂或自动执行许可。
Source自动编码、Worker、订阅及任务生命周期继续属于后续SA11/13/16范围。

原`Engine.settle_call`、`RouterBudget.settle`、
`RepositoryAnalysisStore.settle_call`、预算算法、ToolAuthority、旧Source/Trigger DTO
及H02/H12实现不在本次修改范围。真实来源、付费、外发、发布、长期门仍按原授权和
验收单列，本确认不改变任何SA/Harness验收状态，也不继承H12付费授权。

### 34.4 固定核对版本与确认范围

核对时间：`2026-11-13T03:32:40Z`。
Harness核对起点：`2854ede7fea39f809a736545d056ce9cdd66ea88`。
SA只读核对HEAD：`e9ee378c1977cdb4feaea29bda2d00e3f6c891ca`；确认精确绑定下列提案字节：

- 请求SHA256：`61e7f27134718da134bd55b77755112559fa95ce2dda6e217ba447f634127c43`。
- 0041 SQL SHA256：`d6aead811237e532664adf62b15521a16369fd29b8dfe9d26156f0d74fec1ee3`。
- 接线补丁SHA256：`352277eb82d61b75534a072c6ece1c6769c4653102bac1399e36d021e419c278`。

现有Harness 0040 SHA256：
`cd140161bf06a66bc43d942431bef9bc16990865f71cf8b26acd3763f121d76b`；
0037 SHA256：`f585ba1c2a1d164652585bdc876a0918e60b6fe07432df5d2b20e156392254a1`。
本次只在Harness本地单独提交此文档，不导入/执行SA草案、不修改SA活动树或主库，
不夹带其他工作，不额外运行测试或付费实验，不推送。

## 35. SAV1-11 动态订阅与 Source Mailbox 共享范围确认（2026-11-13）

本节响应用户代转的
`/home/developer/magr-super-agent/v3/docs/super-agent-v1/sa11-proposal/ownership-request.txt`。
已只读核对请求、0042 SQL草案、四文件接线补丁与Harness当前活动范围。本节是SA11
的新所有权/接口/迁移编号确认，第34节SA10/0041不能替代，不认证SA11实现或验收。

### 35.1 迁移及私有所有权

确认**SA侧Codex在`magr-super-agent`副本独占**`0042_source_subscriptions.sql`，
以下七张无正文事实表及其专属索引、函数、触发器、CAS/连续游标/完整版本链约束：

- `app.sa_source_subscriptions`
- `app.sa_source_subscription_versions`
- `app.sa_source_subscription_routes`
- `app.sa_source_subscription_deliveries`
- `app.sa_source_subscription_intakes`
- `app.sa_source_subscription_consumptions`
- `app.sa_source_subscription_handbacks`

`super_agent/source_subscription*.py`及对应新schema/config/tests归SA侧独占。
Harness正式迁移最高0040，未占用0042、上述对象/模块/路由，**未发现编号、接口或
文件所有权冲突**。0041仍按第34节归SA；0037/0040继续归Harness，双方历史SQL不改，
不导入、改名或伪造对方占位，不导入H12/产品/对方活动树。既有0030历史事项按第30节。

新事实复用原Source/project及canonical input、SA10固定correlation revision与
原Thread→Ticket→Run/Mandate复合作用域，不永久FK到可清理的`source_inbox`，
不改SourceIngress清理规则。原业务Task/Run/Mandate继续是唯一目标和执行状态。

### 35.2 SA副本内四文件最小接线

确认下列新增接线由SA侧独占，Harness不并行实现或修改这些接线：

1. `Engine.__init__`仅import/组合`SourceSubscriptions(self)`。
2. `api.py`仅import/注册新`source-subscriptions`管理前缀。
3. `SourceIngress`在原canonical提交事务内调用
   `accepted_conn(conn, input_id, receipt_id=...)`。严格绑定本receipt新canonical，
   普通重复/alias不得自动激活旧输入；投递/容量待恢复/歧义/关闭跟进事实与接收
   同事务提交，不另开连接或在事务中联网。
4. 私有`FastPath._assess`在已验证控制路径之后、语义candidate之前增加只读订阅/
   投递短路，记录原因并走context，不调用模型。原SourceEval逐项复核、reserve前及
   I01最终发送栅栏仍经过此链；不改SourceEval/I01生产代码或旧snapshot/tag/hash。

Harness当前未提交的`api.py`改动仅涉及首页资源内容哈希/缓存一致性及独立helper导入，
不涉及本次SA路由注册、Engine组合、SourceIngress或FastPath；同一文件不同方法范围
已明确。未来跨副本集成须审查固定提交/实际差异并独立复验，不能直接套用活动树。

### 35.3 新契约、管理接口及消费语义

确认独立`source-subscription-v1`/`source-intake-v1`契约，以及
`/api/v3/source-subscriptions`前缀的11个主人管理接口：
`POST /subscriptions`、`POST /subscriptions/{subscription_id}/renew`、
`POST /subscriptions/{subscription_id}/rebind`、
`POST /subscriptions/{subscription_id}/unsubscribe`、
`POST /subscriptions/{subscription_id}/handback`、`GET /subscriptions/{subscription_id}`、
`GET /sources/{source_id}/subscriptions`、`POST /dispatch`、`GET /runs/{run_id}/mailbox`、
`POST /runs/{run_id}/prepare`、`POST /runs/{run_id}/ack`。
沿用当前主人cookie、Origin/CSRF、事务内session复核与本前缀no-store；不接受正文、
角色/身份自证、factory、付费或执行授权。主人metadata token可关闭订阅，不复活
已过TTL正文；save/renew/rebind需要新鲜seed快照，selector TTL与seed正文TTL分开。

Source投递/分页/消费凭证仅存原Run下canonical引用，分配原`Thread.latest_seq`，
不推进Thread.revision；Source lane游标独立，旧Mailbox intake/ack、graph输入及
Run.input_revision语义保持。正文仅在当前权限/原Run/epoch核对后短暂物化于内存，
不伪造legacy Trigger/Issue/身份或将Source正文写入旧快照/checkpoint。

primary/context_only/observer只代表用途，每条消息重查Receive、Source/identity/
policy/config/head/TTL、原目标和Mandate；primary还须精确Ticket控制角色和follow_up
授权。只用主人明确审核的资源/原生话题或可证明reply链，关系不明则记录歧义。
同Run新epoch需要fresh prepare，旧snapshot/ack/materialize均fence；不同Run仅由主人
显式rebind到同原Ticket下已存在且获准的Run，不自动resume或创建attempt。
撤权/编辑/到期使旧消费失效；容量不足保留pending，不回退新任务模型。

handback按canonical版本记录一次needs_routing或loop_blocked无正文待办，保留原输入/
决定/unknown并限制交回循环，不直接调用Eval或改写旧FastPath决定。终态/关闭后的跟进
不得被普通成员或机器人复活，消费凭证不等于工具或约束已应用。

原Engine业务方法、legacy Mailbox、Routing/Router/Worker、预算及三处财务结算方法、
ToolAuthority、H02/H12与历史SQL不在本次范围；不扩展到SA12/13/14/16或真实平台、
编码、付费、外发、发布、长期许可，不改变SA/Harness验收状态。

### 35.4 固定提案绑定

核对时间：`2026-11-13T04:31:56Z`。Harness核对起点：
`ac404d5f0dbfe552df7a28e2f1f02be2123d0e57`；SA只读HEAD：
`657ebf5ca61b30136c8d5e77c0cc912283f24091`。精确绑定以下提案字节：

- 请求SHA256：`a24851da271f0b31b237ed6796b528f46e3c8de3dd5089360584ec088e745321`。
- 0042 SQL SHA256：`3e29900f22472c4d8b0c5b2bc60a64a798134532b2e47ab09427cc5006d4c8d0`。
- 四文件补丁SHA256：`da51e1366a086a53125127f2fd54fec3b497629478a542d15a37c220365dedb6`。

本次只在Harness单独本地提交此协调文档，不执行/安装SA草案、不修改SA活动树、
不夹带当前分析回执/前端修复、不推送，不为协调额外运行测试或付费实验。
SA所述隔离PG草案安装检查是其准备证据，不作为本次实现或整卡验收结论。

## 36. SAV1-11 0043 canonical guard 前向修复确认（2026-11-13）

本节响应用户代转的
`/home/developer/magr-super-agent/v3/docs/super-agent-v1/sa11-proposal/canonical-guard-repair-request.txt`。
已只读核对请求及SQL草案；第35节`d0af074`仅确认0042，本节补充新0043编号及最小
函数修复范围，不扩大原七表、11接口或四文件接线范围，不代表修复/整卡验收通过。

确认**SA侧Codex在`magr-super-agent`副本独占**
`0043_source_subscription_canonical_guard.sql`，仅通过`CREATE OR REPLACE`修复既有
SA专属`app.sa_source_subscription_canonical_guard()`：将同时引用不同表NEW字段的
CASE拆成PL/pgSQL独立IF分支，版本表只读取`seed_input_id`，其他原适用表只读取
`input_id`。保留原canonical input、source、project、version_fingerprint拒绝谓词，
不新增表、接口、授权或业务入口，不改历史0042或其安装checksum。

Harness当前正式迁移仍最高0040，未占用0043或该SA函数；**未发现编号、接口或文件
所有权冲突**。0041/0042继续按第34/35节归SA，0037/0040继续归Harness；不导入、改名
或伪造对方迁移占位。原Engine/预算/ToolAuthority/Harness/H12不在本次修改范围。

确认用户指定的SA复测边界：原351项首轮309PASS/42FAIL及原独立失败保留；SA通过
原迁移入口安装0043后，仅复验原42失败节点并分别保存版本/结果，不重跑309个已通过
项，不启动SA12。该边界不表示本次Harness已执行SA测试，也不改变任何真实、付费、
外发、发布或长期门状态。

固定核对时间：`2026-11-13T05:18:41Z`。Harness起点
`be9900051cb9965ca5eac1a621bd3de8e2721473`，SA只读HEAD
`ca6d409b688c7277cc31e39484830070a42efb44`；精确绑定：

- 请求SHA256：`84014b9acf3550c04e8c6ec71e8161f66beb076bc213f3fd8bd8bdb94a96d9a2`。
- 0043 SQL SHA256：`55f7e50886149eca9ae78db2bc5e72d186b41679fc403c8c318aa6fb8e10a008`。
- 原0042 SHA256：`3e29900f22472c4d8b0c5b2bc60a64a798134532b2e47ab09427cc5006d4c8d0`。
- 原四文件补丁SHA256：`da51e1366a086a53125127f2fd54fec3b497629478a542d15a37c220365dedb6`。

本次只在Harness单独本地提交本节，不执行SQL、不修改SA活动树/主库、不夹带当前
分析会话/流式回执修复，不额外运行H12或付费检查，不推送。

## 37. SAV1-12 Scheduler / Assignment、0044 与固定 Broker 扩展确认（2026-11-13）

本节响应用户代转的
`/home/developer/magr-super-agent/v3/docs/super-agent-v1/sa12-proposal/ownership-request.txt`。
已只读核对请求、SA12_SCOPE、SQL、三文件接线、固定来源清单及Broker补丁；
SA提案提交为`d63e43777fb802cc788668b086df586b061fa5ae`。
第35/36节仅覆盖SA11，本节是SA12新增范围的独立所有权、接口与编号确认，
不代表实现、安装检查或整卡验收通过。

### 37.1 0044 与私有模块所有权

确认**仅SA侧Codex在`magr-super-agent`副本独占**
`0044_source_scheduler.sql`、`super_agent/source_scheduler*.py`及其专属
schema/config/tests/原Engine同conn与mutex驱动桥。0044新增七表及其专属
索引、约束、函数、触发器：

- `app.sa_scheduler_configs`
- `app.sa_scheduler_config_versions`
- `app.sa_scheduler_queue`
- `app.sa_scheduler_sessions`
- `app.sa_scheduler_assignments`
- `app.sa_scheduler_operations`
- `app.sa_scheduler_events`

Harness正式迁移最高0040，目前未占用0044或上述SA对象，**未发现编号、接口或
文件所有权冲突**。Harness本轮GitHub交付功能另用
`0045_harness_github_delivery.sql`及`app.harness_github_*`，不占用0044。
0037/0040仍归Harness，0038/0039/0041/0042/0043仍按既有交接归SA；双方历史
SQL不改，不在对方副本伪造占位或直接导入活动树。未来合并仍审固定提交与实际差异。

新增事实只管理原Task/Run关联的配置、队列、Session、Assignment、租约、资源与
operation；原Thread/Ticket/Run/Mandate、费用及业务完成仍为原Engine真值。
不复制question、Source正文、模型结果或钱袋，不重置旧quota账。

### 37.2 SA副本三文件最小接线

以下接线由SA侧独占，Harness不并行实现或修改这些SA接线：

1. `coordination/engine.py`：仅组合`SourceScheduler(self)`；`_run(epoch非空)`
   增加当前受管lease/session/PG时间门，无epoch财务settlement保持原语义；
   `begin_run`通过精确内部claim或可信Worker上下文只启动一次，未受管路径保持；
   `recover`在原pause/unknown处理后将受管Assignment收敛到draining并保留资源。
2. `api.py`：注册独立`source-scheduler`管理/Worker路由，旧supervisor跳过
   受管Run，Owner `list_runs`仍显示全部。
3. SA固定旧版`harness/contracts.py`：仅给`ExecutionRequest`增加兼容字段
   `execution_epoch: int = 0`；不覆盖Harness当前contracts或其他合同/运行回路。

Harness本轮分析交互修改位于`repository_analysis/*`与前端；GitHub交付位于
独立`harness/github*`、产品路由和Dispatcher journal协议。它们不修改上述
Engine组合/claim/lease/recover接线，也不实现SA路由。相同文件未来跨副本集成
必须逐方法审差异，不能用SA旧文件覆盖Harness当前文件。

### 37.3 固定 H03/H06 切片与新增公共 Broker hook

同意SA仅导入下列固定文件，在SA副本实施所绑定的扩展；Harness原完整
adapters/H12 Broker不覆盖，不导入commands.py/CommandSession/H09/H11/H12、
0037/0040或邻仓未提交文件，H02固定文件保持原样：

- H03 `workspace.py`固定提交`87654b5108ad8929154dc60e7c0d7b0e255b1800`，
  SHA256 `19f018af23bf60d5541de1fc2ab24d9c74d764a181676fc8bde3ab0fc3775649`，
  原字节不改，H03独立接收仍据`8cf07d2`第18节，旧pending元数据保留。
- H04/H06 `broker.py`固定提交`e06328d5f71d696bfb4c88510a77bcccc97b777e`，
  base SHA256 `bb9a93ffe5dab9573a9d8f5c7b920a7827d9ca81b1119bb0ee08cd3ee35219a1`；
  本次扩展after SHA256
  `1ac29b2baf527baa74b59f0f375eddbfd058506e79771bd8bd38788e108dc91f`。

确认本次公共接口范围：`launch`三个可选keyword-only参数
`container_name`、`ownership_labels`、`dispatch_guard`；新增
`BoundCommandStatus(name, container_id, state, exit_code)`、
`inspect_bound_command(name, ownership_labels)`及
`stop_bound_command(name, ownership_labels)`。三个参数全None保持原H06调用兼容；
bound模式必须三者齐备。原授权、挂载、资源、网络、凭据与外发拒绝核心保持。

name必须为`mgar-sa12-`加原operation UUID.hex；四项labels严格为
`mgar.sa12.assignment/operation/workspace/project`，全部来自服务端PG事实，
Worker/Source/HTTP不能自提供。先提交确定name、完整归属和request hash，再create；
仅对create返回的64hex immutable ID执行start。dispatch guard使用原Engine mutex，
严格inspect匹配完整归属、同ID及created状态；slow inspect后，dynamic
`Authority.peek`再次重读PG当前Run/Source/lease/session/config及PG时间并结束短事务，
随即按该ID start。不得以cached lease代替，不在PG事务中执行Docker IO。

恢复只使用strict bound inspect/stop，精确匹配归属后按immutable ID删除并证明
原确切name不存在；daemon/transport/畸形JSON/归属变化均不能当missing或释放依据。
preparing/active/draining保留唯一Task/Run/Session/workspace及资源；有未完/unknown
operation或原read_job不能释放。只有fence后新鲜可信quiescence事实才释放，
Worker自报finished不完成业务。新hook **NOT_RUN**，历史H06 PASS不扩展为新hook PASS。

### 37.4 15接口与启用边界

确认`/api/v3/source-scheduler`下独立15个接口：Owner侧
`PUT /config`、`GET /state`、`POST /queue`、`GET /queue`、
`POST /queue/{queue_id}/cancel`、`POST /sessions`、
`POST /sessions/{session_id}/revoke`、`GET /assignments`、
`GET /assignments/{assignment_id}`、`POST /assignments/{assignment_id}/drain`、
`POST /assignments/{assignment_id}/reconcile`；Worker侧
`POST /worker/claim`、`POST /worker/{assignment_id}/heartbeat`、
`POST /worker/{assignment_id}/ready`、`POST /worker/{assignment_id}/report`。

Owner沿用cookie、Origin/CSRF、事务内session复核与no-store；Worker独立Session
bearer仅存hash，不下发主人cookie。默认disabled、无Worker driver，不新增模型、
真实平台、编码、付费、外发、发布或长期许可。队列仅登记已有获准原Run，纯上下文
不授予执行；所有实际操作仍受原Mandate、Source策略和能力门约束。原Engine持锁
连接、原预算/结算、ToolAuthority及可信完成判定不被旁路。

正式集成首次执行SA12真实注册目标，新hook与依赖按新代码单列；失败修复后只复验
原失败，保留历史PASS/FAIL/NOT_RUN。协调不改变Harness/H12或SA验收状态，
不自动启动SA13；SA14完整异步输入与安全点协议不在此卡。

### 37.5 固定提案绑定与本次操作

核对时间：`2026-11-13T07:00:52Z`。Harness核对起点
`05899b5dfbb1c08284cc65b14b7cd8ee8d2a1796`，SA只读HEAD及提案提交
`d63e43777fb802cc788668b086df586b061fa5ae`。精确绑定SHA256：

- 请求：`cc4529844f1a0398293b242895c0dbe440e6e87867c242c01fd4a1214e527bd5`。
- SA12_SCOPE：`20a8f7c9c84067be4e1fb0725b1d39630050c8f6e1bbde99ebad5a08eca19760`。
- 0044 SQL：`7199a8020bd17ec069e0b18efc8c48fa3a63827cb83dafbde47fbad7bba07eb6`。
- 三文件接线patch：`7899c89d13c9d4533e2b5dda4e68a5f9c51a6e2834f7d33929587b9dbb9bc2cd`。
- 三文件before/after元数据：`ff9804812cd2c3804edafade3d08ef3108c0da54f166d98a47b78e4f87d35154`。
- Broker bound patch：`ddc37d4eac382531f6822b128599d7e40a296da515595467bbc874468af764e1`。
- 固定imports清单：`68648ac45e45a314d7b49906fe5afd5f97ec827690c8ed6f4955065f71e31ddb`。

本次仅单独本地提交此交接节，不执行SQL/测试/付费，不改SA树或主库，不导入
固定文件或应用patch，不夹带分析交互、GitHub交付、前端或H12工作，不推送。


## 38. SA13 编码桥提案所有权、接口与 0046 确认（2026-11-13）

### 38.1 确认范围与迁移归属

Harness 已只读核对 SA 提案提交
`b6079dbae2818e155fff65df870b0c930a15e523`，同意以下范围由 SA 侧 Codex
仅在 `/home/developer/magr-super-agent/v3` 独占实施。第37节仍只覆盖 SA12；
本节是独立的新确认。Harness 不并行修改 SA 副本的这些接线。

- `0046_source_coding.sql` 六张 `app.sa_coding_*` 表及同前缀函数/guard 归 SA。
  Harness 指定基线 `88c75b8dac75da7e7d0c283a451c0ff5a3a3c9b0` 迁移最高0045，
  SA正式迁移目录尚无0046，未发现编号冲突。0045 GitHub delivery 仍归 Harness；
  原42条 SQL 不改，不导入0037/0040/0045。
- 私有 source-coding-mandate-v1 domain/rules、SourceCoding 原连接服务、typed
  target factory/准入、journal/密文生命周期、原usage/SA07端口、快照/工作区/
  Broker/Verifier adapter与本卡测试归 SA；不建立第二套 Task/Mandate/钱包。
- 四个共享文件仅允许请求所绑定的最小接线：`coordination/engine.py` 组合与恢复；
  `api.py` 路由注册；`super_agent/source_scheduler.py` 独立 Coding scope authority
  与 read_only 端口；`harness/contracts.py` 兼容扩展。before/after 精确绑定下列
  metadata/patch，不覆盖旧调查、readonly、原财务函数或 SourceEval-I01 栅栏。

四文件补丁已在内存核对，after SHA256 分别为：

- engine：`d1e5b16681f9d6f5f8fa0a5a60ee34d01e4c50b4a7afcc754e9db268a9f2313d`。
- api：`b404bc8e5aa7a05fa0cbe912f0fdda5cdb08b0e4fe4fcb9d9abeae11b127addf`。
- source_scheduler：`d0a450a98b3ad2571301adbf299f7ca09c3e80d5a6818836c6cf25c0b0a6bf37`。
- contracts：`d9ba15e16a80791e6e92deefea609bf809773cc4d90baa72bd31d71a9313aaf4`。

### 38.2 固定 H11 最小模块与兼容边界

仅确认 H11 固定提交 `d3f3457b3d9dfab68bc73a40add299e4e633fbeb` 的下列四个
blob，不读取邻仓未提交文件，不导入完整 H11/H12 Store/Service/Ledger：

- coding_loop SHA256：`9bd10946398a8744a5ba5654bb6e81870650f16091983854d0f0123f2536ac3a`。
- long_context SHA256：`449149cbbcf702c10caf3b7e3770cf78e96a7a07d435150c56a0efa493c5d720`。
- adapters SHA256：`959c6a5c42223c076ffece8ff57f5a990e36cec9ccdfbec510ea519b290d30b1`，
  原字节进入新 `source_coding_adapters.py`，SA旧 `adapters.py` 不改。
- incremental SHA256：`f11b17a1be47d773decf4a37c078b5c341f4003f33a380d19370aadf6f8cb31d`。

contracts 保留原 content 必填及 SA12 epoch；Mapping 显式 replacement 一律拒绝。
incremental 仅增加 full-content 兼容，失败关闭，不改旧R5 worktree；兼容后SHA256
为 `3fbf3a796b3ae3d1eb1192c5a4ddfa2a249d1b111e2139471015be4fb4d29def`。

已明确接口分叉：SA full-content-only 合同与 Harness 当前 replacement/public-probe
合同并不相同。隔离副本内无覆盖冲突；SA补丁不得回写 Harness main 或活动 H12
repair 树。Harness 本轮 contracts/loop/adapters/Broker/public-probe 修复不属于此
固定切片；未来跨树整合须逐接口重新协调，不能以旧文件覆盖当前实现。

### 38.3 六个 metadata 接口及能力门

确认 `/api/v3/source-coding` 下六个接口，未发现同前缀路由冲突：Owner侧
`GET /capabilities`、`POST /admissions`、`GET /bindings/{coding_id}`、
`POST /bindings/{coding_id}/cancel`；Worker侧 `POST /worker/{coding_id}/run`、
`GET /worker/{coding_id}/state`。

Owner沿用cookie、Origin/CSRF及当前session复核；Worker独立bearer与Assignment。
不接收任意命令、host路径、factory、token或付款自证，不向Worker下发Ownercookie
或安装token。原Engine锁及同PG连接、原Task/Run/Mandate、usage/SA07预算仍为唯一
权威。保留H03真实tree、持久意图、SA12 bound最终派发及可信静默；unknown不重发，
裸receipt或Worker自报不完成Task，不直接引入锁序不兼容的H11 RecoveryIO。

仅同意受控工程桥，默认None/关闭。H11固定工程/恢复范围不扩张；H12真实Agent与
编码MVP仍FAIL，模型连通或环境工程PASS不授真实Coding许可。本确认不授权真实
平台、付费、外发、发布、长期运行，不启动SA14，也不改变任何历史验收结果。
SA所述私有139项及隔离草案安装是准备证据；正式注册/PG/Broker/恢复集成仍NOT_RUN，
须由SA按本卡验收，失败修复后仅复测原失败项。

### 38.4 提案完整哈希与本次操作

精确绑定提案目录 `v3/docs/super-agent-v1/sa13-proposal/`：

- 0046_source_coding.sql：`379e9de4a018c7f38cc3615eae31b9c9d9e44ebdf16cbcf3c101c81d9874f212`。
- shared-wiring.patch：`01c7ec61c678e59099c687962b0413c9bfac0be45c0bb0167e7078f5b157d590`。
- shared-wiring-metadata.json：`9103b55fa14c8789fed582c8ebd4ce55290b2481793354ff0e8119d6643ae3c2`。
- harness-imports.json：`429a354849206643f8734c81abc009ebfc1a98f84f621cafc1db844b0176126a`。
- contracts-compatibility.patch：`68d808f64459e647ece77496ab4152362d4bac98283009232e59cb7f2ab26832`。
- incremental-full-content-compatibility.patch：`d94514e56c13066ee3f39c482f668fdc83b820d1c290daed05b70d520265c8a2`。
- SA13_SCOPE.md：`8fa36e7e3511a0865abc426646568338bd5b4edd507bc3e19e790fbb8e4123f9`。
- schema-notes.md：`46d3a75f0047332358c16e9a0d20e7c1a3a1fc65a3bb84998de78924a33dfb3e`。

请求文件SHA256：`ecb61f6be2535ba37b5dade17a9014af09cebd8416c3845ec31aeaa9e82eda38`。
核对UTC：`2026-11-13T10:40:52.129978+00:00`。

本次只追加并单独本地提交此确认；不应用SQL/patch、不执行测试/付费、不改SA树
或主库、不夹带H12和产品未提交工作、不推送。


## 39. SA14 异步输入与原 Job 提案所有权、接口及 0047 确认（2026-11-14）

### 39.1 所有权与迁移编号

Harness 已只读核对 SA 提案提交
`3fbcb848e8d9b2eba6c2a7c7dcd181990e8f0b9d`，同意以下精确范围由 SA 侧 Codex
仅在 `/home/developer/magr-super-agent/v3` 独占实施。第38节仍仅覆盖 SA13；
本节为 SA14 的新确认，Harness 不并行修改该 SA 副本的接线。

- `0047_source_async.sql` 归 SA，六表为 `app.sa_async_bindings`、
  `sa_async_inputs`、`sa_async_checkpoints`、`sa_async_jobs`、
  `sa_async_operation_views`、`sa_async_events`，以及本草案绑定的函数和 guard。
  核对起点 Harness `617e14a52d65c5276bcd3ef87dbd0f05b4a3c08d` 无0047；
  SA 正式迁移尚无0047。未发现编号、表名或路由前缀占用冲突。
- 同意草案在原 `sa_coding_operations`、`sa_coding_payloads`、
  `source_ingress_inputs`、`source_ingress_bodies`、`source_role_bindings`、
  `source_task_controllers`、`source_identities`、`source_policies`、
  `source_ingress_controls` 上增加前向触发器。仅原事务内记录精确受管绑定的
  sticky fencing、最小无正文失效、停止请求及原 Coding 密文不可用事实；
  不做外部 IO，不退款或改写真实 usage/unknown/业务结果。
- SA 原43条历史 SQL 不改；0045 仍归 Harness；不导入0037/0040/0045，
  不将本草案安装到 Harness 主库。
- SA 私有 `source_async` domain/rules/plan/service/api/runtime 及测试归 SA。
  原 `source_coding.py`、`source_coding_runtime.py`、`source_coding_journal.py`
  仅增加异步受管拒绝绕过、输入 revision、最后实际派发 guard、最短 Source TTL
  和原回执对接。不建立第二 Task/Run/Mandate、钱包、进程或账本权威。

### 39.2 三文件最小接线与接口

仅确认提案 patch/metadata 所绑定的三个共享文件；已在内存逐 hunk 核对当前 SA
before 字节与 proposed-after SHA256，未写入或应用补丁：

1. `coordination/engine.py`：组合 `SourceAsync(self)`，在原 SA13 恢复之后调用
   `source_async.recover_conn(conn)`。after SHA256：
   `6b126ee6ac10bd2591220f20bc6b208c880ab6c73c658cff037a3a9448f39f67`。
2. `api.py`：仅导入并注册 `source_async_api.register(app, authority, browser_user)`。
   after SHA256：`f0e7d02ee25918ffa79a69c2d21ac097736faff551309c8c49c823652f289dd0`。
3. `sources/ingress.py`：原 canonical 提交/ACK 事务中，以 `_ready(conn)` 检查后
   调用 `accepted_conn(conn, input_id, receipt_id=...)`。after SHA256：
   `ac6f4acc3e09bf96d7ef32a98db359e1f57bdfd60c6261901c2058aed9ff1e07`。

确认三个内部接口 `SourceAsync._ready(conn)`、`accepted_conn(...)`、
`recover_conn(conn)` 均复用原 Engine 持锁 PG 连接和短事务。构造、接收 hook 和
恢复不启动 IO、线程、监听或模型。原 Engine/SA07 财务算法、SourceEval-I01 栅栏
和 canonical DTO 保持；长等待释放全局 mutex，真实派发前另有最终当前守卫。

确认 `/api/v3/source-async` 下12个无正文 metadata 接口：

- Owner 8个：`GET /capabilities`、`POST /admissions`、
  `GET /bindings/{coding_id}`、`POST /bindings/{coding_id}/restrictions`、
  `GET /bindings/{coding_id}/inputs`、`GET /bindings/{coding_id}/jobs`、
  `GET /bindings/{coding_id}/events`、
  `POST /bindings/{coding_id}/jobs/{job_id}/cancel`。
- Worker 4个：`POST /worker/{coding_id}/start`、`GET /worker/{coding_id}/state`、
  `GET /worker/{coding_id}/jobs/{job_id}`、
  `POST /worker/{coding_id}/jobs/{job_id}/cancel`。

Owner 使用当前 cookie、Origin/CSRF 和事务内复核；Worker 使用独立当前 bearer
及精确 Assignment/Session/Run/epoch，不下发 Owner cookie。分页上限64，响应
`no-store`；不接收正文、任意 argv/provider/host_path/token、付款、权限或 Task
成功自证。received/applied/blocked 可查，限制仅收紧；context_only、机器人和
日志不授控制。新输入 revision 使完整旧模型回复及其中工具调用失效。

### 39.3 固定复用与能力边界

仅复用 SA 已集成 H11 `d3f3457b3d9dfab68bc73a40add299e4e633fbeb` 的
`SafePoint(before_model/after_model)`；H09 固定依赖仍为
`7d8f0ab5df1d4b98ff3140af553c11bc1ca74a1c`、复核
`096300eed8946086f99e331ae4c8c9c733108ff9`，以绑定的 reuse 清单记录其审计。
本次没有新 Harness blob 导入，不接入 H09 明文文件 inbox、Store、Service、
Ledger 或 SSE，不改固定 kernel/contracts/Workspace/Broker/Verifier。

原命令 Job 沿 SA12 bound 资源查询、取消和对账；取消请求不冒充已停止，未证明
静默不释放槽位。原子编辑与在途费用保留实际事实；unknown 不盲重发，撤权后恢复
也不复活旧正文。仅原 SA13 已接受公开 fixture_git/纯函数 Python 受控工程；
默认不启动后台运行。H12 真实 Agent/MVP 仍 FAIL；本确认不开放真实 Coding、
平台、付费、外发、通知、发布或长期运行，不启动 SA15，不改变历史验收。

SA 所述私有65PASS、SQL草案29份分版本通过及静态检查仅为准备证据，不合并冒称
同一最终版本单轮全量 PASS；正式迁移、接线、12路由及 PG/Docker 并发恢复由 SA
按本卡首次验收，失败修复后仅复验原失败。当前 Harness H12 修复保持独立分支；
同名文件在两个副本中已有差异，未来跨副本合并须逐接口协调，不能整文件覆盖。

### 39.4 完整提案绑定与本次操作

只读核对 UTC：`2026-11-14T08:12:26.900760+00:00`。以下七文件均与提案提交
`3fbcb84` 的已提交字节和请求中完整 SHA256 一致，12条 method/path 无重复：

- 0047 SQL：`9ce372e6a82cbf747c51624486ef7548faf056cc60912e0287279be8b569f041`。
- 三文件 patch：`c43e05c0d8aabe1bccf3484cbb7922c5f6f1897f6d2cd5ac0840b4454ff85b5e`。
- before/after metadata：`184d6b9d4d8888ae4b925cbf299aeca87d0d74cd6289732d9222c0324ffaa1e0`。
- interfaces：`82a4fcacf5533a2e7aa954b183bc886b4d3c64dffef1fd0c61319d5552146f89`。
- harness-reuse：`e8ad44ea8a8b2c7e9c0af4d7e4be4ded72b50b44a640dce2b03be3dfb01eeb9e`。
- SA14_SCOPE：`96063b58454bc45b500bfdc884b4431623edb63a8e9108161821bd0ae81eaec2`。
- shared-risk-review：`4844c107b710dfb15449ec0b4e4740a65f4571fa4c213d869de3afeb09319523`。
- 请求：`40f2ed9e8fbb51a85931f38d487307386bdcead347a16000da31374cadd8e588`。

本次协调仅追加并单独本地提交此节，不应用 SQL/patch，不运行 SA 测试或付费，
不改 SA 活动文件或主库，不导入、合并或夹带任何 H12 提交，不推送。


## 40. SA15 只读 Recall 的 Engine/API 所有权与接口确认（2026-11-14）

### 40.1 所有权与迁移边界

Harness 已只读核对提案提交 `5f731b4169273949241574f650bfc8290321bab2`，
同意以下精确范围由 SA 侧 Codex 仅在 `/home/developer/magr-super-agent/v3` 独占实施。
第39节仅覆盖 SA14；本节是 SA15 的新确认，Harness 不并行修改该 SA 副本接线。

- 共享修改仅为 `coordination/engine.py` 导入并组合 `SourceRecall(self)`，
  以及 `api.py` 导入并注册 `register_source_recall(app, authority, browser_user)`。
  不增加恢复 hook，不改预算/结算、旧 API、Source 清理或原授权行为。
- 私有 `source_recall` domain/rules/cursor/service/api/tools 及其测试、配置、
  schema 归 SA；不修改 Harness 或 SA13/SA14 执行器。
- 本片不新增数据库迁移，不预占0048，不改 SA 现有44份 SQL；复用原对象、
  外键、ContextEdge、Memory 和 CAS 的有界读取。若需额外共享文件或 schema，
  须另行提交具体范围确认。本确认不安装 SQL，不写 Harness 主库。
- 复用原 Engine 持锁 PG 连接和短事务，不建立第二 Graph/Task/Run/授权库、
  正文缓存或业务写者；CAS IO 在事务外，释放内容前再验当前权限和版本。

### 40.2 精确补丁与九个只读接口

已在内存逐 hunk 核对当前 SA before 字节和 proposed-after SHA256，未应用补丁：

- `coordination/engine.py` before：
  `6b126ee6ac10bd2591220f20bc6b208c880ab6c73c658cff037a3a9448f39f67`；
  after：`ac0b20ae5d63ea840d17b52268277ea38dae7cf15ad6f7d07ac8bf2ed3a0caeb`。
- `api.py` before：
  `f0e7d02ee25918ffa79a69c2d21ac097736faff551309c8c49c823652f289dd0`；
  after：`ed86a57f8a0c9eea515ef14e47fdf7eedc958de1424fe409968ad0861c2014d0`。

确认 `/api/v3/source-recall` 下九条无业务写入接口，method/path 无重复：

- Owner：`GET /capabilities`，`POST /resolve`、`/walk`、`/page`、`/search`。
- Worker：`POST /worker/resolve`、`/worker/walk`、`/worker/page`、`/worker/search`。

Owner 沿原当前 cookie 和 POST Origin/CSRF；Worker 使用独立当前 bearer，
绑定原目标、Run/Assignment/Session/epoch 与 Mandate，不继承 Owner cookie。
每跳重验 Source、目标项目/仓库、安装/身份/角色/Receive、canonical head 和
最短保留期；首片 Worker 跨任务召回仅限同一有权安装/会话，跨私密群或仓库拒绝。

默认 metadata/no-store；正文仅经真实 provenance 路径显式有界分页，不接受
任意 CAS hash、宿主路径、URL、actor/trusted/provider/token、付款或成功自证。
遍历上限4跳/32节点/128边/16KiB，原文页128–4096字节并保持 UTF-8 边界；
PG扫描与操作时间同样有界。加密游标绑定当前 caller、query/path、version/hash、
offset 和短 TTL，每页重新检查；游标和摘要不授予权限。Source 编辑、删除、过期
或页间撤权使旧结果失效，派生内容需全部 Source 依赖仍可读。

### 40.3 固定复用、冲突与验收边界

只读核对 H10 固定 `c140dadf5aff517527c3c5e107843e215cc76556`、F01/F02
独立记录及 reuse 清单的六份已提交 blob/审计副本，哈希一致；SA 已有
`long_context.py` 与该固定版本字节一致。此结论仅覆盖原独立范围，其余 OPEN、
真实门和历史失败保留，不将当前 H12 分支的上下文修改作为可导入交付。

不新增 Harness blob 导入，不引入 H10 文件 Registry 或第二 Store，不读取或
改写 Harness 活动/未提交实现，不接入 H12。私有服务端只读 ToolContext adapter
不默认创建 Worker、线程、模型、监听或 Job，不触发通知、平台外发或发布。

核对起点 Harness `c711d54504c4945625505f47e9df56e22bc1bdac` 未占用上述
SourceRecall 名称或路由前缀；本次没有迁移编号冲突或预留。两副本 Engine/API
已有差异，未来合并需逐接口审查，不能整文件覆盖。本次确认不代表代码已接线、
整卡或独立验收通过。SA 所述私有89项及静态检查仅作为阶段准备记录；正式接线、
PG/API 和跨页权限检查由 SA 按本卡验收，修复后仅复验原失败项。
SA16 未启动；H12 真实质量/MVP仍未放行，本确认不改变任何真实能力门。

### 40.4 提案绑定与本次操作

只读核对 UTC：`2026-11-14T11:16:51.874805+00:00`。以下七文件均与提案提交
`5f731b4` 的已提交字节及 manifest 完整 SHA256 一致：

- patch：`152b61e72bca0e9e0546c8e8295002f131cfe3a8c94c5ba8e452111f5d33cad9`。
- before/after metadata：`998d3d74c1b15de2f15083a07240b8c59b0976e0e5ddf361b0692d9a1bc649d6`。
- interfaces：`d2cca75d47d154683c92ea2202d6651fd7240449801ec5adaba7011ed564dbb9`。
- harness-reuse：`7cfb870f5148c92f940a2d9caba155e064ada504444ed1cf0c151d4d4e7f0d7d`。
- SA15_SCOPE：`d34fd12af9d38d655462d099480c545924f5456a057c89e437be60730649102c`。
- shared-risk-review：`79229c56725bc763b8e13e1fc0904d6d8c150acb37bc7eaa855bffeafbcc85af`。
- 请求：`6b2ccb334ac612a80648134a6c1ec8936069ae714f68251d88fe296fa4265657`。
- manifest 自身：`6bbf4c05b2df5d9f46e1814a3c238c3af6494b038979771425af092d281fbf91`。

本次只追加并单独本地提交本节，不应用 patch/SQL、不运行 SA 测试或付费，
不操作 SA 活动文件或主库、不导入或夹带 H12 提交、不预占0048、不推送。

## 41. SA16 静默自主生命周期的共享所有权、接口及 0048 确认（2026-11-14）

### 41.1 所有权与迁移编号

Harness 已只读核对提案提交 `70d569a9ddbb9080fcf5373f282d67d6f7c473bb`，
同意以下精确范围由 SA 侧 Codex 仅在 `/home/developer/magr-super-agent/v3` 独占实施。
第40节只覆盖 SA15；本节是 SA16 的新确认，Harness 不并行修改该 SA 副本接线。

- 协调编号0048确认归 `0048_source_autonomy.sql`，在 SA 副本新增前向迁移；
  Harness 不另建同号迁移，也不在本次安装或导入该 SQL。0037、0040、0045仍归
  Harness，既有历史迁移保持原字节，0049及以后本次不预占。
- 六表为 `app.sa_autonomy_rules`、`sa_autonomy_rule_versions`、`sa_autonomy_ticks`、
  `sa_autonomy_claims`、`sa_autonomy_attempts`、`sa_autonomy_events`，以及提案内的
  不可变历史、单调版本/游标、委托有效性 guard 和 `sa_autonomy_run_current` 谓词。
- SA16 私有 `super_agent/source_autonomy*`、测试、配置、证据和文档归 SA。
  新表只存规则、信号、认领、游标、原 Run 引用及审计，不建立第二 Task/Run 状态机、
  余额或业务写者。复用原 Engine 持锁连接、mutex、短事务、Mandate/Thread/Ticket/Run、
  Scheduler 和预算/资源协议；文件、网络和 Docker IO 仍在事务外。

### 41.2 精确五文件接线

已在内存逐 hunk 核对当前 SA before 字节与 proposed-after SHA256，未应用补丁：

- `coordination/engine.py`：导入/组合 `SourceAutonomy(self)`，在原恢复事务追加
  `source_autonomy.recover_conn(conn)`；before
  `ac0b20ae5d63ea840d17b52268277ea38dae7cf15ad6f7d07ac8bf2ed3a0caeb`，after
  `1c0c6321e1092f99ddb8460dc02d2602fa6a654693dd52bfefc08f0a8e06c652`。
- `api.py`：导入并注册 `register_source_autonomy(app, authority, browser_user)`；before
  `ed86a57f8a0c9eea515ef14e47fdf7eedc958de1424fe409968ad0861c2014d0`，after
  `6d6f21282f7c11ce718ba084bf6da1093acb225a27b40808c2b0084301e7eb2c`。
- `super_agent/policy.py`：原 `check_run` 先追加当前委托检查；before
  `14e74af7f703a19e91ad581c0ce46bb3e009c5e62c354793f36f7fcc518a70e0`，after
  `bd182d87eb5d92e93b0d69b3de1ee09e451ff2d7456c3bc966c3ef9767017093`。
- `super_agent/source_coding.py`：保留 Owner 入口，提取同事务 `_admit_authorized`，
  仅当前有效团队规则委托可替代 `explicit_request` 条件；before
  `8f99a673cf8e8821b98bc47c1bc8d4d033071acc449903d02e4e571647af7e33`，after
  `64a267a8665039e287a6b4cced7d76360af5d975193cc7599fc313d61f5acffa`。
- `super_agent/source_scheduler.py`：保留 Owner 入口，提取同事务 `_enqueue_authorized`；
  before `a663a49c99cea566c04a3bb2487772984b5ea04aad4e6fcdd7f2b5c5c48bc2c9`，after
  `ab9cb2feac7968f7b076832b538f3a0520a8cf9cffcb3d298ce92d245706f840`。

两项私有准入方法不暴露 HTTP，须验证原连接与 INTRANS；调用方先验证 Owner 签署的
精确规则/review hash，以及当前 Source、身份角色、策略、仓库、配置、预算、资源和
可信服务端 profile。SourceDecision 仍是无执行权的 would 提案；摘要、模型结果、
context_only 机器人、旧 Run 或只读许可不因此取得执行或升级权限。

### 41.3 十三个 Owner 接口与运行边界

确认 `/api/v3/source-autonomy` 下如下 method/path，组合无重复：

- `GET /capabilities`、`GET /rules`、`PUT /rules/{rule_id}`、`GET /rules/{rule_id}`。
- `POST /rules/{rule_id}/disable`、`GET /claims`、`GET /attempts`、
  `GET /attempts/{attempt_id}`。
- `POST /attempts/{attempt_id}/stop`、`POST /attempts/{attempt_id}/reopen`、
  `POST /maintain`、`POST /tick`、`POST /cycle`。

沿用当前 Owner cookie，写操作重验 Origin/CSRF；DTO 严格有界，不接受客户端注入的
runtime/factory/provider/bearer 或模型自报权限、成功和费用。`auto_investigate` 复用
原只读调查 Run；`auto_repair` 首片只复用已验收 `fixture_git` Coding、隔离 Worker
和 Broker。缺原 winner factory、可信 profile 或真实门时明确等待，不伪造 Issue、
不创建平行任务绕过；本确认不增加真实 Coding 或 H12 付费许可。

规则修订/停用以版本和当前授权阻断后续操作，保留原 cancel/epoch、队列与 assignment
drain。SQL 中 `sa_coding_current` 使用 CREATE OR REPLACE；已逐字确认其函数体除
新增 `AND app.sa_autonomy_run_current(b.run_id)` 外与0046原谓词相同，不改原 OID、
旧条件或 late-known 回执/结算路径。未知费用和资源继续保留原预占，不盲重试/退款。
重开须新的 Owner 核验和当前输入/规则/epoch/profile/预算，不重置历史账本。

定时默认不开工、不付费；须明确时区、日历、有效期、`allow_scheduled_work`、当前
seed Source 与执行配置。沿规则/日期唯一认领和持久日期游标，DST fold0、不存在
时刻跳过，停机合并最近有效 tick；tick 不伪造新身份、正文或模型结果。心跳不算
语义进展，无进展/TTL 到期停止并按原资源确认规则清理。无默认常驻监听/调度器、
通知、外发或发布；SA17未启动，全部真实能力门仍须独立验收。

### 41.4 哈希、冲突与本次操作

只读核对 UTC：`2026-11-14T13:17:06.856881+00:00`。提案六文件均与 `70d569a`
已提交字节一致；补丁基线 `be88c957eb4768ebdc0ea294ae01d6f5aea34c5e`：

- SQL：`20d376a1d96087e0b42e2f4c4eda2a32b48c955a0c8216fb6767595a21afa3b3`。
- 五文件补丁：`139bb8deb62d7514c4b1eef50e3d5e3657ee4da9ec392164a741561e811f509d`。
- before/after metadata：`e055e29eb9cb4bf09b2e210b7274b784ba6a73fe8b34fffc425b318285ca5c13`。
- 范围与接口：`47ddfe861a31265f1f20b319c7ba937e28fac3350998c2ce006e93c93d967da7`。
- 风险范围：`4f33027f78f4426669d44f8f1c99838908ac8843e69fa8b46f593fbf9998645c`。
- 请求：`78f67538eb92be78bf0f242d41959f6a049b640879ee4b350269ec99a02361d3`。

Harness 起点 `e0432f0e289de868a5956339aeec76db71bd0efe`，当前各 Harness 工作树
与 SA 正式迁移目录均未占用0048；Harness 未占用上述表、SourceAutonomy 或路由
前缀，本次无编号/名称冲突。两副本共享文件已有差异，未来集成须逐接口审查，不能
整文件覆盖。额外共享文件、接口、迁移或预算语义变更须另行提出明确范围。

SA 报告的私有77项及隔离PG草案10项通过仅记录为准备证据，不在本次重跑或提升为
独立验收。本确认只落协调文档，不应用 SQL/patch、不写业务库、不运行 SA 测试或
新增 SA 付费实验、不修改 SA 活动文件、不导入或夹带 H12 提交、不推送。
H12 真实质量/MVP仍未放行，本协调不改变任何 H 卡验收状态。

## 42. SA17 多渠道通知共享所有权、接口及 0049 确认（2026-11-15）

### 42.1 所有权及迁移编号

Harness 已只读核对提案 `43e2df5281bc1eb825074aea2be6370ec3b58219` 和说明快照
`3b20235a253a170838d47ca20fe2711075f7f86f`，确认以下范围由 SA 侧 Codex 仅在
`/home/developer/magr-super-agent/v3` 独占实施。第41节仅覆盖SA16；Harness 不并行
修改本节列出的SA副本接线。

- 0049确认归 `0049_source_notifications.sql`，作为SA副本的新前向迁移。五表为
  `app.sa_notification_controls`、`sa_notification_versions`、`sa_notification_intents`、
  `sa_notification_receipts`、`sa_notification_events`，及提案内不可变事实/递增版本、
  原Action/Source因果守卫与撤销待发通知的触发器。
- 同意该草案为原 `app.actions.action_type` 约束追加 `source_notification`；保留原
  `issue_comment`、`draft_pr`，不重写旧迁移，不扩大旧动作的准入/审批语义。
- 私有 `super_agent/source_notification*`、`source_notifications.py`、相应测试、
  配置、文档和证据归SA；不新增第二Task/Run、费用或Delivery主状态机。
- 0037、0040、0045仍归Harness；本次只确认0049，不预占0050及以后编号。

### 42.2 精确三文件接线

补丁共10个hunk，已在内存核对当前before、逐hunk上下文、after哈希及Python语法：

- `coordination/engine.py`：导入/组合 `SourceNotifications(self)`；仅新通知动作
  使用其验证/defer路径；回执和对账先记录通知Delivery，旧评论/PR继续原
  `_apply_effect_outcome`；在原恢复事务追加 `recover_conn(conn)`。
  before `1c0c6321e1092f99ddb8460dc02d2602fa6a654693dd52bfefc08f0a8e06c652`，
  after `4785f320795bd7cbc266a99e0f7de5e795c170dc12528c5d3a33f6303b4468a6`。
- `api.py`：导入并注册 `register_source_notifications(app, authority, browser_user)`。
  before `6d6f21282f7c11ce718ba084bf6da1093acb225a27b40808c2b0084301e7eb2c`，
  after `9cc4abbffe505aff071a09c58f4d1380ac7fee34eb48e4e74463d0e9e8bfe1c2`。
- `super_agent/fast_path.py`：原 `_own_result` 先调用通知来源因果hook；只接受已知
  真实回执与精确发送身份/安装/原话题/profile匹配，正文标记不产生自身结果身份。
  before `4a8ea9ed9afc54e6a40a7e49eca8a1ac15f33035781273b71cd4e4251bbbf8b6`，
  after `019954b0144cf9118f4a39945cde07138c007aba9ab55dd2315913a518d770fa`。

### 42.3 十一个 Owner 接口与原运行边界

确认 `/api/v3/source-notifications` 下如下method/path，组合无重复：

- `GET /capabilities`、`GET /sources/{source_id}`、`PUT /sources/{source_id}`。
- `POST /plans`、`GET /plans`、`GET /plans/{action_id}`。
- `POST /plans/{action_id}/cancel`、`POST /tick`、`POST /plans/{action_id}/reconcile`、
  `POST /maintain`、`GET /events`。

沿用当前Owner cookie和写入Origin/CSRF，DTO严格有界；不接受自由正文、URL或接收者、
模型自报成功、provider/runtime注入。复用原Engine持锁连接、mutex和短事务；Channel
IO在事务外，经原Action/Approval/Outbox和Dispatcher唯一出口，不改Dispatcher实现。
共享配额仅复用SA07 `notification_quota`，不改变财务/预算算法或未知用量预占。

四类固定最小披露模板仅引用已验证状态和认证控制台，绑定原Run/Source、当前身份/
ACL/策略、NotificationGrant、精确内容hash和原安装/话题；ExecutionGrant不能产生
通知权限。quiet、聚合、冷却、有效期和持久幂等在计划/领取/最终发送前检查；已撤权
取消待发，发送后的晚回执只结算通知，不推进业务Task/Run发布成功或伪造撤回。

unknown不回ready，只有具备能力的只读精确对账可结算；not_found不证明没有发生，
无可靠查询则等待人工，不盲重发。首片只允许服务端显式注册的受控fixture Channel
profile，无默认发送、Dispatcher启动、监听、真实平台操作、付费或发布；SA18未启动。

### 42.4 固定哈希、冲突检查及本次操作

只读核对UTC：`2026-11-15T04:24:54.517236+00:00`。SQL及patch均与 `43e2df5`
已提交字节一致；六份完整提案文件均与 `3b20235` 说明快照一致：

- SQL：`3546760623c0bd1710170a75a19e74cf97e759897f975f748c601ae444d91aab`。
- patch：`289879fe1c9c344a11db140866e47c364e6ce3da8215f03f5c2031a0b7c23545`。
- 范围：`fd128a11aac44782260711b1e6587adbfbbbc30f4ce7a5c244bae8ce83bde6d5`。
- manifest：`7dd25cd60bf313bf1ea84b4bf5364d87b2d0bc6f92755d2817fd57b259f216e9`。
- 请求：`0584de71cb5e9b874790e259c1a7e57fa58ff74f2aeaa6617ba5791ba023951d`。
- 准备记录：`143d8558ccf015e8b6e7a1e51a851dc26fca2ebea7adc9579c236cff6ecc72e1`。

Harness起点 `1d49a4108e88945c826b00257d1eb69aebf25824`。四份Harness工作树及SA
正式迁移目录均未占0049；Harness未占上述五表及路由前缀，无编号/表名/接口范围
冲突。两副本共享文件已有差异，未来集成逐接口审查，不能整文件覆盖；额外共享
文件、接口或预算语义变更须重新提出明确范围。

SA报告的29项准备、lint/typecheck通过仅记为准备证据，本次不重跑或提升为独立
验收。本次仅追加并单独本地提交本节，不应用SQL/patch、不修改SA活动文件或业务
数据库、不导入/夹带H12工作、不新增SA测试或付费、不推送。H12质量与MVP状态
继续按其独立验收记录，本协调不改变H卡门槛或真实能力许可。

## 43. SA18 精确审查、审批与 Draft PR 共享范围及 0050 确认（2026-11-15）

### 43.1 所有权与迁移编号

Harness 已只读核对提案快照 `0f5a94285c16843d9c63dfc8c4788419469facb1`，其
准备候选为 `7c2de663f62167430c6b6f9f3c5cd3a7270dc0c1`，初始准备实现为
`bebbe90740f1acd3b6f1b1025bd68f21a3e9c991`。确认下列范围仅在
`/home/developer/magr-super-agent/v3` 由SA侧Codex独占实施；第42节只覆盖SA17/0049，
Harness 不并行修改本节列出的SA副本接线。

- 0050确认归 `0050_source_reviews.sql`，作为新前向迁移；六表为
  `app.sa_source_reviews`、`sa_source_review_payloads`、`sa_source_review_presentations`、
  `sa_source_review_approvals`、`sa_source_review_links`、`sa_source_review_events`，
  及草案所列Source/身份/角色/策略/Coding/Async失效触发器。
- 只存审查、加密限时正文、精确展示/批准与关系/回执事实，不建第二Task/Run、
  费用或投递主状态机；复用原 `draft_pr`，不新增Action类型或修改旧迁移。
- 私有 `super_agent/source_review*`、`source_reviews.py`、`web/source-reviews.*` 及
  对应测试、配置、文档与证据归SA；不修改 `web/agent.*` 或Harness内核。
- 0037、0040、0045仍归Harness；本次仅确认0050，不预占0051及以后编号。

### 43.2 精确两文件接线

补丁共6个hunk，已在内存核对当前before、逐hunk上下文、after哈希和Python语法，
没有将补丁应用到任何副本：

- `coordination/engine.py`：导入/组合 `SourceReviews(self)`；在原验证链中仅对
  `source_reviews.handles(action)` 精确关联的新审查Draft PR进行验证分流；在原
  恢复事务追加 `recover_conn(conn)`。保留旧评论、通知及Coding Draft PR路径。
  before `f079a8b11131c38fa422022ea3c65435becce7d62e38bcc3efe3e1b9d3da304e`，
  after `d4130dc7a2269171946611e30efe68c23bde52e877d8d881d31a0629382068e9`。
- `api.py`：导入并注册 `register_source_reviews(app, authority, browser_user)`。
  before `1d587dd2fde495bb78071e638614c8e4acab27a0f782a681ac12f29ebd4849c6`，
  after `4b04b45423527ecbb64caafd2fc348edec8786e00806f1aa01e19b1009be0549`。

### 43.3 十三个 Owner API、三个页面/资源路由及原运行边界

确认 `/api/v3/source-reviews` 下以下method/path，及三个独立页面/资源路由：

- `GET /capabilities`、`GET /`、`POST /`、`GET /{review_id}`。
- `POST /{review_id}/inspect`、`POST /{review_id}/approve`、
  `POST /{review_id}/cancel`、`GET /{review_id}/patch`。
- `POST /tick`、`POST /{review_id}/reconcile`、`POST /{review_id}/native-links`、
  `POST /maintain`、`GET /events`。
- `GET /source-reviews`、`GET /assets/source-reviews.js`、
  `GET /assets/source-reviews.css`。API集合根路径是 `/api/v3/source-reviews`，无额外尾斜线。

复用原Engine持锁PG连接、RLock及短事务；文件、网络和远端查询在事务之外。
业务状态与三步发布继续归原Ticket/Run/Mandate、ReviewBundle/PublicationApproval、
Action/Approval/Outbox/Dispatcher及 `repair_publication_steps`，不改Dispatcher或
预算/结算算法、未知用量预占和既有配额；读写端口分别由服务端显式登记。

Owner cookie、Origin/CSRF和有界DTO沿用现有认证。完整Diff、变化文件、实际H03
已测树与原Verifier事实绑定同Owner/同会话/精确hash的短时展示回执；展示不声称
证明人已阅读，IM文本、NotificationGrant、模型声明或旧通用approve不能替代精确
发布批准。首片仅支持既有UTF-8普通文件、100644、全树不超过1MiB、单文件不超过
256KiB、最多16个变化文件；新增/删除、二进制、软/硬链和展示不完整均失败关闭。

正文加密限时保存，撤权/编辑删除等在原事务失效并擦除，已发/未知回执事实保留。
每次实际写前重新核对最新Source/Owner/ACL/策略、epoch/input/async revision、
实际已测树、审批、许可和目标fence；远端完整tree、全部blob、唯一父base及精确
Draft PR对象必须一致。unknown只读对账，不盲重发；PR/CI只关联原Ticket和相同
head/attempt，不沿用force-push前验证。缺写许可可审查/导出并等待审批。

固定H08只复用已确认的增量/前像hash/结果展示契约，不导入其 `service.py` 文件
任务库、H12 `github_delivery.py` 或付费权限。独立页面沿用Owner认证、no-store/CSP
与安全文本渲染；`fixture://` 明确标记为受控回执。默认无真实平台、模型、外发、
发布、监听、merge、部署或长期运行；主库不安装新迁移，SA19未启动，H12真实
Agent/MVP及全部实际能力门继续按原独立记录，不因本协调放行。

### 43.4 完整哈希、冲突检查及本次操作

只读核对UTC：`2026-11-15T06:25:06.598355+00:00`。七份提案文件均与 `0f5a942`
已提交字节一致，SQL/patch及manifest的完整哈希和before/after相符：

- SQL：`851f7a0d8a6673aa2f6416536d4f0629cc9d33d59541443e2a04bc88a10b751a`。
- patch：`442b0f1778280d9e6ddee04026c0ab6dbc81697beee4493c69567d3141fdbc69`。
- scope：`aae455d489ee9efedf02cb471e078a6cd8ce83c7ed3cbcc273710d4fe2baf0e4`。
- manifest：`43132f985f11aafb71092020ccea95dd68cf463213e90bc9d6680b47ccda4d8a`。
- shared-file-hashes：`acc50a861f50d018036411c28c60085029012bf7514513ad3d421da90b5b207d`。
- 请求：`4e6977c5c0121f1ae1a48164e9aade105395b8ade9c7babaa7103b4faca8a49b`。
- 准备记录：`452890dd7432ca2df0957a4360dadc4fba50b31d4cd2e80c319bcd4f2e3e5708`。

Harness起点 `a09aef760e4c43ff46595a3c3cc56c503b018e97`。四份Harness工作树及SA
正式迁移目录均未占0050；上述六表及路由/服务前缀在Harness无占用，本次无编号、
表名或接口范围冲突。两个副本共享文件已有差异，未来集成逐接口审查，不能整文件
覆盖；超出本节的共享文件、接口或预算语义变化须另行提出明确范围。

SA报告的86项准备通过、初轮lint/typecheck两处失败及其定向修复通过，仅记录为
分版本准备证据。本次不重跑SA检查，也不提升为整卡或独立验收。本次只追加并
单独本地提交本节，不应用SQL/patch、不修改SA活动文件或业务库、不导入或夹带
H12提交、不新增SA付费、不推送。Harness原未跟踪H10复验文件保留。

## 44. SA18 等待审查期间异步限制收件修复及 0051 确认（2026-11-15）

### 44.1 所有权、编号及精确SQL

Harness 已只读核对提案 `6d946df521da0946b1d802e7bb820d22d48c310e`；请求记载的
源码候选为 `19a98f7effc2a846af9a3486151992288e26bee7`，草案/实测为
`7ab606e2f2e62ca9d5e9db75a5a6e50a10d6ac72`。第43节仅确认0050，本节单独确认
`0051_source_review_async_intake.sql` 归SA侧Codex仅在SA副本独占实施。Harness
不并行修改这一SQL范围；不预占0052及以后编号，不重写0047/0050或安装主库。

- SQL：`2bfcba77da3ad089737e1ae9fcb9db1aef72ca844e27c313c57a9686b55debd5`。
- manifest：`0a01e43bbf5fe5ea7a8942b857136830b380d76a16237d27c560d9ef3fefc189`。
- 请求：`f17d1350f277b223494fc90d63ccf92cfda1efa7d3a0c35cb643c4db63bd8a75`。

三文件均与上述提案快照字节一致。SQL只新增
`app.sa_source_review_intake_current(uuid)`，并前向替换 `app.sa_async_input_guard()`；
新旧guard去除空白及CREATE OR REPLACE声明差异后，唯一变化为原INSERT的
`received`/非system收件条件追加该命名谓词例外，其余正文完全一致。

### 44.2 仅收件的接口与执行边界

新谓词仅对应原fixture Source Coding的 `waiting_approval`，绑定同一
Run/Assignment/session/workspace/epoch、原未结束Ticket/Mandate、有效admitted
Coding与active Async、实际已记录attempt、tests_run>0且tests_failed=0的完成
Verifier，以及精确关联的source-review-v1 Draft PR/Review事实。

原异步revision分配会在同一Engine事务内先使旧Review失效，因此此收件谓词不要求
旧审查包仍有效。接收限制后仍由原0050触发器失效并擦除旧包，已发/未知事实保留。
依赖原Source撤权等触发器永久fence Coding/Async；保留原来源不可变引用、当前
human/精确Ticket控制、顺序/版本/数量与一次消费校验。

本例外只允许记录受控元数据；不更改 `sa_async_current`、Worker/Job启动、
checkpoint、新工具/模型payload或applied守卫，不自行应用限制、恢复执行、开工、
复用旧审批或批准发布。不加表、路由、Engine/API/Python共享文件或财务语义，
原Engine持锁连接和短事务边界不变。第43节0050及两文件接线补丁仍保持原哈希。

### 44.3 冲突、准备证据及本次操作

只读核对UTC：`2026-11-15T07:33:38.310753+00:00`；Harness起点为
`09145fe0939b209c43addb0ae6bbbdcc6a6df9a2`。四个Harness工作树及SA正式迁移目录
均未占0051，也未占新增谓词名，无编号/函数名冲突。SA正式0047与原失败候选
`835cb5f6e914e83396e8df389eb7e47b1b40a0f6`字节一致；0050 SQL与第43节patch
仍分别为 `851f7a0d…0b751a`、`442b0f17…fdbc69`，本次不扩大其确认范围。

记录SA报告的相同失败node在隔离docs-SQL overlay下1PASS、新安装及未改执行
守卫2PASS、一次lint折行失败后仅失败目标复验通过；均为分版本草案证据，本确认
不重跑旧通过项或SA测试，不提升为正式修复或独立验收。正式0051与原失败项仍待
SA应用后按既定规则只复验原失败项，失败历史保留；SA19未启动，真实能力门不改。

本次只追加并单独本地提交本节，不应用SQL、不改SA活动文件或业务库、不导入/
夹带H12、不新增SA模型/付费、不推送。原未跟踪H10复验记录保留；H12质量/MVP
继续按其独立记录，本协调不开放实际Coding、执行或发布权限。

## 45. SA19 来源控制台、0052与三文件接线确认（2026-11-15）

### 45.1 所有权、固定提案及编号

Harness已只读核对提案 `3a911854842bbf81b02bfbcc28234f99eb27ffed` 与准备记录
`fbfc8091a88f5040bb91b685987891c9cd9d8c99`。第44节只覆盖0051，本节单独确认
`0052_source_console.sql`、SA私有 `super_agent/source_console*.py`、专属
`web/source-console.*` 及其测试/契约/禁用样例/文档归SA侧Codex仅在SA副本实施。
Harness不并行修改本节精确接线；不预占0053以后，不重写旧迁移或安装主库。

0052仅新增 `app.sa_console_credentials`、`app.sa_console_audit` 两表，
`app.sa_console_credential_guard()`、`app.sa_console_audit_append_only()` 及其
trigger/index；仅加密来源凭据保管和最小审计，不新建Task/Run/Action/Usage、独立
全局开关或另一调度/预算状态。原Engine持锁PG连接、RLock及短事务仍为唯一业务
写入边界；crypto、密钥文件和远端IO在事务外。

- SQL SHA256：`765932acca6566a33596e2c543b446c0675ccce689db9e82223b64cf9cd14fb4`。
- patch SHA256：`b2ba32c532005d4d798f9230c5e1b6d0676ff300a720e62f4a1f2fef7eeb22f8`。
- manifest SHA256：`ddb2e8e10da20995f94dd87faa20d2cd0659019bfc475044aff79348d8fdb18f`。
- 请求 SHA256：`6f2eebda3180e334fe4713f42bbaff39c3ca6b07c3d0ea170fa80d5a4f06b974`。
- 准备记录 SHA256：`fa1f2459f717877a4c98c002ef3dbd26f2ba40f017897c7bae1bce3958a23549`。

### 45.2 三文件精确接线

补丁共5个hunk，已在内存核对当前SA before、逐hunk上下文、after哈希与Python语法，
没有应用到任何副本。Engine/API before与第43节确认后的after一致。

- `v3/src/mgar_v3/coordination/engine.py`：仅import并组合 SourceConsole(self)。
  before `d4130dc7a2269171946611e30efe68c23bde52e877d8d881d31a0629382068e9`；
  after `5658065429a9035ed5d60f4d90571a5adb8d5c732a0a1a53b741bca25841c815`。
- `v3/src/mgar_v3/api.py`：仅import/register SourceConsole，传入已有GitHub/FeishuRuntime观察端口。
  before `4b04b45423527ecbb64caafd2fc348edec8786e00806f1aa01e19b1009be0549`；
  after `2d0e9dbf78158f425c823ef31f65cb5b37852b156df5b26b3937de55088d2b08`。
- `v3/web/agent.html`：仅追加登录后open-source-console链接至/source-console；不改旧页面脚本/样式。
  before `48cede6f717055a0796eb9416cf49bf99816bed44c3868ae38848300837919c3`；
  after `9320988919f0220dff1ebaba208df7d9ac0328bf587b1e91e9004b183469f806`。

副本间现有差异须逐接口整合，不能整文件覆盖。旧账户、凭据及来源模块不在修改范围。

### 45.3 十五个接口及原运行边界

以下12个Owner API及3个独立页面/资源路由归SA侧：

- `GET /api/v3/source-console`。
- `GET /api/v3/source-console/sources/{source_id}`。
- `PUT /api/v3/source-console/sources/{source_id}`。
- `PUT /api/v3/source-console/sources/{source_id}/credentials`。
- `POST /api/v3/source-console/sources/{source_id}/revoke`。
- `PUT /api/v3/source-console/sources/{source_id}/ingress`。
- `POST /api/v3/source-console/disable-all`。
- `PUT /api/v3/source-console/policies/{policy_id}`。
- `POST /api/v3/source-console/policies/{policy_id}/preview`。
- `GET /api/v3/source-console/tasks/{run_id}`。
- `POST /api/v3/source-console/tasks/{run_id}/stop`。
- `GET /api/v3/source-console/decisions`。
- `GET /source-console`。
- `GET /static/source-console.js`。
- `GET /static/source-console.css`。

沿用Owner会话、Origin/CSRF。凭据仅Owner安全网页写入、API不读回，绑定安装、
范围、revision和expiry后加密；查询不回传消息、代码、日志或平台密钥。复用原注册、
来源设置、身份、策略、Ingress与Engine.cancel，旧模块不重写。全局停用在原事务
原子撤销所有来源事实；已发/unknown及费用预占保留，不改预算/结算或增加独立flag。

健康只读取同一Engine已登记Runtime的新鲜current诊断；缺证据为unknown。在线、
消息权限和实际能力门分开。配置/观察不启动平台、监听、模型/Worker、通知、PR或
发布；关闭页面不停止原服务器事件/任务。真实Worker门未放行，UI和后端均拒绝
auto_repair保存。本确认不开放真实Coding/执行/发布或SA20。

### 45.4 冲突、准备证据及本次操作

只读核对UTC：`2026-11-15T09:17:15.354447+00:00`；Harness起点
`162b6ef890f18c89df07adf62b0ca0375b5da69c`。七文件与已提交字节一致。四个Harness
工作树及SA正式迁移目录未占0052；上述表、函数、路由和入口ID在Harness无占用，
无编号/文件接线范围冲突。0050/0051及旧迁移不改，不预占0053及以后编号。

记录SA报告的54项准备检查、首次typecheck通过，以及首次lint一项失败后仅该目标
修复通过；均为分版本准备证据。本次不重跑SA测试，不提升为正式接线或独立验收。

本次仅追加并单独本地提交本节，不应用SQL/patch、不改SA活动文件或业务库、
不导入或夹带H12/R00、不新增真实调用或外发、不推送。原H10未跟踪记录保留。
R00仍按用户授权仅做基线/证据核验，R01–R08未启动；H12/MVP原FAIL继续保留。

## 46. SA20 常驻运维、0053及Engine/API/Settings接线确认（2026-11-15）

### 46.1 所有权、固定提案与迁移编号

Harness已接收并只读核对SA20协调请求，固定SA快照
`e9443671b5f76e593b3ae80edc0a99d34b58ab0c`；提案
`a9e2a7ab15ad506567a48676858aed0ececb4236`，请求文档最后提交
`3f75b601438fe4a827f1acef90ad854741b806d6`。第45节仅覆盖SA19/0052，
本节单独确认 `0053_source_operations.sql` 四表及以下三文件精确补丁归SA侧Codex，
仅在 `/home/developer/magr-super-agent` 副本实施。Harness不并行修改本节确认的SA接线。
不重写旧迁移，不预占0054及以后编号，不安装业务库或导入任何H12提交。

- SQL SHA256：`1ae8bd6ee189f8efe7f937a2ccc90e2b49b2012107f751426b9d8a27e10d2c37`。
- patch SHA256：`ecc71e2f4effe7ccf138fe1b2663e33b22481e2e065828c41dcc28626a86c74a`。
- manifest SHA256：`55bff8b21ab46ebb63c0175d025ff0d94d666b7cf38e2d699f5791beaf1ff160`。
- interfaces SHA256：`5f8d6c9b47c6981c2f014b94a1f6fd0c63059d612b9eb8219e3dfb0a66fcb9ed`。
- 请求 SHA256：`8cb25c01180dc4879b1b52b1ff83272a9045c0793fc6bb52e61808d84c976a30`。
- scope SHA256：`895713740cb4419bc7da8fd5a478068f39139e6e288f29dd3eafc3ea5df26054`。
- 准备记录 SHA256：`dcf85642ef1b2bb39ce175dbca9cd1616dce8871a137003f8f890cdbe8beac9e`。

四表为 `app.sa_operations_versions`、`app.sa_operations_controls`、
`app.sa_operations_components`、`app.sa_operations_incidents`，以及草案中同前缀的
索引/函数/触发器。只保留Owner签署配置历史/当前引用、generation/boot/sequence、
待确认tick与无正文告警/wait/escalate事实，不复制Thread/Ticket/Run或预算/结算状态。

### 46.2 三文件精确范围与TLS/Owner边界

全部hunk在内存核对当前SA before、上下文、after哈希及Python语法，没有应用补丁。

- `v3/src/mgar_v3/coordination/engine.py`：仅import并组合 `SourceOperations(self)`；
  before `5658065429a9035ed5d60f4d90571a5adb8d5c732a0a1a53b741bca25841c815`，
  after `8d4e0ca17ef3fff7509e82a201bdd9d5bbe2e71560766fac3e9a5ad219869129`。
- `v3/src/mgar_v3/settings.py`：仅新增 `web_public_origin`、
  `operations_service_config` 及纯校验/Host/Origin集合属性；
  before `45325d1b10eb8ae3048dd9fd689b8bcf28f7b4741a1c3a793b112aa8a132b68f`，
  after `7fcbf070e4e4db395e2dbaecc30a53c0c26338952d3cd5f9c86bfe8b0361c880`。
- `v3/src/mgar_v3/api.py`：仅注册11接口、将原Host/Origin固定集合参数化、配置公网时
  强制Secure cookie并拒绝首次Owner setup；
  before `2d0e9dbf78158f425c823ef31f65cb5b37852b156df5b26b3937de55088d2b08`，
  after `524a8ec755e5e00ad66015e209410c90a1f212ca7a13a3f78b1e48b523fa3e99`。

确认单一严格HTTPS origin、既有回环集合保留、无通配Host/Origin、不从任意Forwarded头
取得身份；公网配置须已有本地Owner，原Owner会话/Origin/CSRF继续适用。运维客户端
仍须回环及分组件凭据，配置公网不授予业务执行权。此为所有权/接口确认，TLS代理、
真实公网部署及安全行为仍需SA正式接线后的独立验收，不因本节判为通过。

同一原Engine持锁PG连接/RLock和短事务仍为唯一业务写入边界；平台、模型与文件/备份
IO在事务外。独立Source/Scheduler/Worker/Dispatcher/Watchdog默认未启用，复用原
Source/角色/政策/Mandate/Assignment/Job/Outbox逐操作授权；unknown不清空、不自动
重发，重注册不恢复未确认工作，心跳不算语义进展。不改预算、ToolAuthority或H12。

### 46.3 十一个接口与私有范围

- `GET /api/v3/source-operations`。
- `PUT /api/v3/source-operations`。
- `GET /api/v3/source-operations/metrics`。
- `GET /api/v3/source-operations/incidents`。
- `POST /api/v3/source-operations/incidents/{incident_key}/decision`。
- `POST /api/v3/source-operations/components/{component_id}/open`。
- `POST /api/v3/source-operations/components/{component_id}/heartbeat`。
- `POST /api/v3/source-operations/components/{component_id}/tick`。
- `GET /api/v3/source-operations/components/{component_id}/snapshot`。
- `POST /api/v3/source-operations/components/{component_id}/observations`。
- `POST /api/v3/source-operations/components/{component_id}/relay/{source_id}`。

SA私有实现为 `source_operations*.py`、`tests/sa20_*`、
`test_source_operations_*`、`deploy/super-agent-v1/*`、专属schema/fixtures/config
及本提案目录。Source relay仅转交原签名Webhook，原Runtime重验并耐久提交后才ACK；
不以进程心跳冒充平台连接健康。备份/恢复限授权静默快照、新隔离库/目录且默认禁用，
保留未知账本、游标、撤权和密钥绑定；不回滚数据库以撤销远端写。

### 46.4 冲突、准备证据和本次操作

只读核对UTC `2026-11-15T11:39:56.309298+00:00`，Harness起点
`8d19308a585e80885fc4f0bfeec4853395236ab8`。七份提案文件与固定提交字节一致；
四个Harness工作树及SA正式迁移目录均未占0053，上述表/路由/服务名/Settings字段
在Harness无占用，未发现编号或本节精确范围冲突。Engine/API/Settings在两个副本
已有差异，未来集成须逐接口审查，不能整文件覆盖；本确认不占有整个共享文件。

记录SA准备证据：初版67PASS/3夹具ERROR，修订78513a4仅原3错误复测3PASS，
70个独特准备目标有分版本通过记录；初typecheck318文件及补丁可应用性通过，
仅失败lint目标后续通过。没有重跑旧通过项，本协调不将其提升为最新源码单轮全量
通过、正式接线通过或真实部署/长期能力通过。SA21未启动。

本次只追加并单独本地提交本节，不应用SQL/patch、不改SA活动文件或业务库、不导入/
夹带H12/R01/R02、不新增真实模型调用/外发、不推送。原H10未跟踪复验文件保留。
Harness的R01已在独立repair分支工程PASS；用户另已授权R02，继续独立实施。
本协调不改变H12质量FAIL/MVP未放行，不开放真实Coding、平台、通知或发布。

## 47. SA22 对 H12 依赖就绪情况的查询回执（2026-11-16）

已接收并只读核对用户转交的依赖查询：
`/home/developer/magr-super-agent/v3/docs/super-agent-v1/sa22-proposal/harness-readiness-request.txt`，
SHA256 `b68e2b5bfe3874e84e1561df21f65d92e5c8a5831c6611e37cf1a5f8896ed990`。

**当前没有 H12 真实Agent门PASS的交付，H12质量仍FAIL、MVP未放行。**
SA核对的1c4f494及原HV4-H12/accept.json不是被新真实质量验收取代的记录；
后续R00–R05仅完成工程修复，不能用这些PASS开放SA13/SA22真实Coding。

最新已提交工程交付位于独立副本`/home/developer/magr-harness-h12-repair`：

- 报告：`v3/docs/harness-v4/H12_R05_INDEPENDENT_REGRESSION_20261116.md`。
- 机器摘要：`v3/docs/harness-v4/accept/HV4-H12-R05/accept.json`。
- 源码提交：`08cb1e28fac1c5e2e27117f7de62fbd3c20d0ac2`。
- 工程交付提交：`8d5052cfb0f043d1c0c4be965d6666367c2a8fbd`。
- 源指纹：`8bee95a622e4fd6c504654e31dbce8ebf6880efec7277bd7ab87c629839cb56b`。
- 667项工程测试通过（新增R05 54项），lint/mypy通过。R05的live_model/live_agent/
  live_publication均NOT_RUN；原H12真实质量FAIL及历史成绩仍保留。

工程覆盖原PG/CAS、预算/权限/恢复、契约/有界输出/可纠正工具反馈、Python/Node入口
与HTTP生命周期、独立最终树全回归和评分重算。包括固定Python/Node/Go容器及免费
受控适配器的正反例；这不是任意仓库、任意依赖或真实Agent成功交付的证明。
最新源码未合并到本副本、未推送；不建议SA以整文件覆盖或导入未提交R06工作接线。

剩余门槛：R06正在审查任务类型基线、Judge公开可追溯性与结构判定，尚未验收；
其后R07公开反馈/覆盖及无进展检查、R08新候选冻结与全新独立持出真实验收仍未启动。
R08仍须开发集至少10/12、全新独立持出至少9/12，原安全/恢复/费用边界及独立复验
齐全；在此之前没有可据以放行真实编码的H12能力白名单。

针对用户本机Loz888/MAGR-v3的完整依赖准备、真实模型驱动debug→验证→PR，以及SA
固定切片与本次修复的联合恢复/Job/安全点/未知对账契约，均没有本轮新PASS交付，
按NOT_RUN处理；原只读问答及已通过的独立SA能力范围由SA自己的验收决定。
本次不提供绕过质量门的最小真实编码接线包。

共享H12仍为813次、US$4.211490/US$12、unknown/reserved=0；R00–R05零新增真实
供应商调用。SA22的USD2授权不追加至H12，不构成新H12付费实验许可。
本回执只追加并单独本地提交协调文档；不占0054、不应用SQL/补丁、不修改SA活动树、
不导入H12/R06提交、不启动Worker或模型、不发送消息/发布、不推送。协调提交不是
能力通过、集成通过或真实Agent门通过的回执；原H10未跟踪复验文件继续保留。

## 48. SA22 受限编码实验的0054编号及精确SQL所有权确认（2026-11-16）

已接收用户转交、已提交于SA提案19e3793的精确请求：
`/home/developer/magr-super-agent/v3/docs/super-agent-v1/sa22-proposal/model-coding/ownership-request.txt`，
SHA256 `86ece4b671fc819384e202c000037bb182499e88359c2f5838c2bebe07a3dc31`。
已只读核对完整SQL草案：
`/home/developer/magr-super-agent/v3/docs/super-agent-v1/sa22-proposal/model-coding/0054_source_coding_trial.sql`，
SHA256 **094909bf76fe89917c4733882719f389003208ffdec4baa196461ea609ef6f48**。

确认0054由SA侧独占实现；精确范围为一张
`app.sa_coding_trial_authorizations`实验授权表、其不可变守卫触发器，以及前向替换
SA13已归SA所有的`app.sa_coding_binding_guard()`。原0046等历史SQL不改写；Harness
不并行占用0054、该表/触发器或本次函数分支。已核对main、h12-repair、h12-v2、
h12-integration及SA正式迁移目录，未发现0054占用或上述新增表/触发器的所有权冲突；
0046中的同名函数是本次明确授权前向扩展的既有SA函数，不是新冲突。

确认遵循用户本次已记录的SA22受限实验授权，无需重批。第47节关于常规质量门的
FAIL仍保留；本节承接其后用户授权的实验例外，不能把实验授权记录为H12 PASS。
实验限本人、既定Bot群及本机Loz888/MAGR-v3，问答与编码合计USD2、期限及精确
Owner/Source/策略/profile/仓库基线/R05证据/manifest绑定；原分支及PR/推送/合并/
发布仍关闭。SQL守卫扩展仅允许质量已PASS或存在精确绑定且未过期的Owner实验授权，
原H11工程/恢复、H12工程及其余绑定/撤销守卫保留，不把H12字段改成PASS。

不改Engine/API/Settings、预算或结算算法，不增加HTTP路由或第二份任务/财务账本。
SA私有模型问答/Coding/Runtime/通知模块及正式SQL应用由SA侧继续负责，复用原
usage_calls/SA07/Assignment/隔离Broker/Verifier；H12/R06活动树及其工作不导入，
R05只读锚点仍为08cb1e2/8d5052c。SA22的USD2不转入H12共享预算，也不授权新的
H12矩阵或开启H13；本线程已获授权的R06工程工作独立继续。

本次仅确认上述编号、所有权和精确SQL范围，单独本地提交本节；没有执行SQL、
SA测试或真实模型/编码实验，没有应用补丁或修改SA活动树，不夹带H12/R06源码，
不推送。此确认不是SQL正确性、集成、费用控制或真实Agent质量验收通过；实际
实验结果和独立核验由SA另行保留，H12质量FAIL/MVP未放行不变。


## 49. SA22 USD10 受限实验的0055前向迁移及SQL所有权确认（2026-11-17）

Harness已接收并只读核对用户转交请求：
`/home/developer/magr-super-agent/v3/docs/super-agent-v1/sa22-proposal/budget-10-20261117/ownership-request.txt`，
SHA256 `294b2be4e9d8d217ccb5eb503bb556f2ea303e5b955000ab192ac760958164b2`。
准备候选 `21aa61a9f3de940f607f9302d30b62a455f8d930`；本次仅确认共享编号与所有权，
用户既有“可用余额更新为10美元”授权不再重批。第48节及0054原历史保留。

确认新前向 `0055_source_trial_budget_ceiling.sql` 由SA侧Codex独占实现，
仅在 `/home/developer/magr-super-agent` 副本实施；Harness不并行占用0055或改写本次
两个精确DDL范围。SQL SHA256：
**67e861128f6a8f3a01b591cfe33e1ed4e0649ae461738a7cdfb2fdf86e90a350**。

- `app.sa_coding_trial_authorizations.maximum_project_micros` 的原CHECK仅扩为
  `IN (2000000,10000000)`，保留USD2，并允许符合原绑定的新签USD10 trial。
- `app.sa_coding_binding_guard()`仅将唯一精确旧谓词
  `AND trial.maximum_project_micros=2000000`前向替换为
  `AND trial.maximum_project_micros IN (2000000,10000000)`；草案要求旧谓词恰好出现一次，
  其余函数字节及全部签署人、Source/策略/profile/仓库基线/R05/期限/不可变/撤权守卫保留。
- 不修改0054或更早迁移，不自动升级已有trial、不覆盖旧授权事实。原Owner预算/策略
  保存接口与追加不可变trial仍是正式激活路径，配置或来源正文不能单独授予USD10。

SA私有四文件预算类型/试验工厂补丁亦归SA，精确SHA256：
**ddb76d82fcef28367ddcc33a3b6a36289dec9ad9fe1d41202eecce16b256949b**。
仅为 `source_trial_limits.py`、`source_coding_trial.py`、`source_conversations.py`、
`source_coding_experiment.py`；默认USD2，受信配置与新Owner事实明确选USD10才适用。
不更改Engine/API/Settings或原预算/结算算法，不新增HTTP接口、任务库或财务账本。

USD10是SA共享累计上限，原已结算、known/unknown预占和未确认用量全部继续计入，
不清账、不退款、不重发unknown、不修改usage_calls/router_calls/旧authorization_budgets。
本人、原Bot群、本机Loz888/MAGR-v3、原数据/工具/期限、最多1Worker、每Run USD0.30
及发布关闭等范围均保留，不延长原期限。SA与H12预算独立；本次不挪用H12 USD12，
不导入任何H12源码，不将H12 FAIL改为PASS，不开放常规真实Coding/MVP或H13。

只读核对main、h12-repair、h12-v2、h12-integration及SA正式迁移目录和本地已知Git
迁移历史，未发现0055占用。既有表/同名绑定函数已在第48节归SA，本次只是明确的
最小前向扩展，无新增所有权冲突；不预占0056及以后编号。原文件若漂移，须重新绑定
完整哈希后协调，不以本节授权扩大DDL或补丁范围。

提案清单SHA256 `8c930ea83c48f08427790e58d33eefbfc4628ba50a5f1e3343317e318db1cbf6`。
SA自报新13项专属真实PG检查、四源码mypy及定向lint的分版本通过记录在其材料中；
Harness本轮仅核对请求、SQL、patch、完整哈希及编号，不运行SQL、SA测试或付费实验，
不据此认证实现正确性、正式激活、集成或真实Agent质量通过。

本次只追加并单独本地提交本节，不应用SQL/patch、不改SA活动树或业务库、不推送，
不夹带当前H12工作。原未跟踪H10复验文件保留。H12 R07工程PASS、原真实质量FAIL及
MVP未放行仍保留；R08准备独立推进，尚无新的真实Agent门PASS交付。
