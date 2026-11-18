# HV4-H07：统一编码循环与多种任务验收

模块：`src/mgar_v3/harness/coding_loop.py`

本卡把调查、编辑、执行、验证与反馈收进同一个 `CodingLoop`。循环只组合冻结
端口：`ModelAdapter`、`ToolRegistry`、`WorkspaceProvider`、`ExecutionBroker`、
`Verifier`、`ContextManager`、`EventSink`、`BudgetLedger`。H07 没有新增执行
入口：仓库/命令工具仍由 H06 适配器调用 H04 Broker，应用进程仍由
`ApplicationPort`（H06 容器生命周期的接缝）承接。

## 1. 任务类型与策略

`TaskPolicy` 按 `TaskKind` 决定权限与必需证据，默认上限沿用
`TASK_TYPES.md`：

| 类型 | 写入 | 执行 | 必需证据 | repair_limit | legacy 上限 |
| --- | --- | --- | --- | --- | --- |
| inspect | 否 | 否 | evidence | 1 | 3 |
| bugfix | 是 | 是 | reproduction + behavior/regression | 8 | 3 |
| feature | 是 | 是 | behavior + regression | 10 | 3 |
| refactor | 是 | 是 | signature + regression | 8 | 3 |
| run_app | 是 | 是 | build/behavior + readiness + cleanup | 10 | 3 |

`attempt_policy="legacy"` 时上限固定为 3，`"resource"` 时用表中的
`repair_limit`，并仍受 Token/费用/时间/工具预算约束。两者都不是"跑满次数即
结束"：只要连续两轮无实质进展就以 `blocked` 停手。

## 2. 计划与验收清单

- `plan(task, baseline_command=..., service_plan=..., attempt_policy=...)`
  先取 `checkpoint_tree`，再按类型生成 `AcceptanceChecklist`。清单项绑定
  受信 `VerificationRequirement`（`requirement_id` 取摘要），来源
  `user_goal` / `trusted_command` / `model`。
- bugfix 一旦 `require_reproduction=True`，必须在基线树上留下**失败**的
  真实命令回执才能满足 reproduction；已绿的基线不能绕过复现。
- `accept_proposal(task, plan, proposals)` 只允许**追加**：
  - 不得复用/替换既有 `item_id`，也不得降低既有项的 `required`；
  - `requirement_id` 必须来自受信要求，且 kind 仅限
    behavior / regression / build；
  - 通过后 `revision+1`，模型不能删除未满足项或把目标改小。
- `run()` 校验 `task_id`、`objective`、`acceptance_revision`、
  `task_kind`、`checklist` 与 `checkpoint_tree`；工作树不等于计划检查点时
  返回 `HARNESS_PLAN_STALE`，避免在旧结论上继续修复。

## 3. 进展判定与防循环

每轮模型不带工具调用时进入验收评估，并计算进展：

- **工作树变化**：最终树哈希与上一轮不同，且不是回到已见过的树（补丁往返
  判为无进展）。
- **新证据**：出现新的 `(tree, argv, log_sha256)` 命令回执。
- 失败签名、通过项数量只用于诊断与反馈，**不再单独构成进展**：对同一证据
  重跑、换措辞的文字改写、重复空搜索都不会重置保护。

无进展连续 2 轮即 `blocked`，并在 `checkpoint` 中保留未满足项与所缺信息。
`empty_search` 反馈记录空结果；同一查询重复出现时追加
`(the same query was repeated)`。取得实质进展会清零连续计数，因此资源策略
可以超过旧的 3 次修复上限继续尝试（测试覆盖了第 4 次修复才成功）。

弱化验收的守卫：

- 删除基线中的测试文件 → 相关 behavior/regression 项失败；
- 测试文件的测试函数数、断言站点数下降（AST 计数）→ 同样失败，防止用
  `skipTest`、注释掉断言或删除裸 `assert` 冒充通过。断言站点按文件汇总：
  裸 `assert`（含带消息形式）与 `self.assert*(...)` 各计 1，因此把
  `assertEqual` 抽到使用裸 `assert` 的辅助函数这类等价重构不会改变总数；
- 最终 receipt 的 `tests_run` 少于基线 → 视为测试被削弱。

## 4. 结果契约

`CodingResult` 把结论绑定到实际代码树：

- `status` ∈ completed / incomplete / blocked / budget_exhausted / cancelled /
  unknown / denied；只有 `verified` 且无失败项才是 `completed`。
- `tree_before`、`tree_after`、`diff`（含增删改）、`receipts`、
  `service_receipts`、`verification`、`checklist`、`attempts`、`feedback`、
  `graph`、`checkpoint` 同批返回。
- 模型 `length`/`error`、未知用量、预算耗尽、工具未声明/越权、用户取消都
  直接结束且不会生成已完成结果。
- `RunGraph` 逐节点记录 plan/baseline/model/tool/command/acceptance/
  verification/feedback/checkpoint；`events_cursor` 对应 EventSink 游标。

## 5. 应用服务接缝

`ApplicationPort.start/stop` 由 H06 的真实容器生命周期实现；H07 的
`ServiceReceipt` 记录 argv、tree、started/ready/stopped、地址、进程号、
探测与清理细节、日志摘要。本卡测试使用内存实现验证 run_app 的
readiness/cleanup 判定，**没有**新增发布、网络或容器入口。

## 6. 与旧实现的边界

`TASKS.md` 标注的主要位置（`repository_analysis/agent.py`、
`repair/repair_agent.py`、`workbench/orchestrator.py`）仍是旧 V3.5 回路。
H07 交付的是供 H08 接入的统一运行时；把首页/API 与旧入口接到本循环属于
H08（"接入 H07 的真实运行时"），本卡未改写旧 agent，以免在未接新 API 前
破坏现有只读/修复入口。旧 `RepairBudget` 的 3 次默认上限在新循环中以
`legacy` 策略保留。

## 7. 残余限制

- `ApplicationPort` 只是接缝；真实应用进程、就绪探测与清理仍由 H06 负责，
  H07 测试不启动容器。
- 弱化检测是 AST 计数启发式：按文件汇总裸 `assert` 与 `self.assert*` 站点；
  测试改名、拆分、参数化，或把同一断言抽成被多处调用的辅助函数，仍可能
  误判；非 Python 测试不做 AST 计数，只看 `tests_run` 与文件存在性。
- 进展判定以树/回执为单位；模型只读不改、只改文字时会被判无进展。
- 验收结论来自 H06 `Verifier` 与工具回执；仓库内代码伪造测试摘要的问题
  继续适用（见 H06 残余限制）。
- `pytest`/`node` 真实执行、三个 live 门仍 NOT_RUN；本卡只跑脚本化离线
  provider 与 Python unittest 真命令。

## 8. 写工具的两种协议（H08）

模型可见的 `apply_edits` 现在同时接受两种互斥的编辑载荷：

- 整文件 `content` + `expected_sha256`：历史协议，错误码保持
  `HARNESS_EDIT_STALE` / `WORKSPACE_EDIT_STALE`，行为不变。
- 增量 `replacements`：每项含 `old_text`、`new_text` 与可选 `occurrence`；
  `old_sha256` 可选，缺省时由 harness 以 `sha256(old_text)` 固定（真实语言
  模型无法自行计算哈希，而 `old_text` 的精确匹配本身就是更强的约束；显式
  传入错误哈希仍抛 `PATCH_HASH_MISMATCH`）。整文件预像哈希不匹配抛
  `PATCH_STALE`，片段哈希不匹配抛 `PATCH_HASH_MISMATCH`，片段不唯一且未
  指定 `occurrence` 抛 `PATCH_AMBIGUOUS`，替换数量或输出字节超限抛
  `PATCH_LIMIT`。匹配逻辑直接复用 H05
  `repair.worktree.apply_text_replacements`，没有第二套实现。

真实模型还需要知道它无法计算的预像摘要：H08 的 `CodingToolRegistry` 在
`read_file` 结果末尾附加一段 `<<mgar-read-file sha256=… tree=…>>` 注记，
模型把 `sha256` 用作 `expected_sha256`、把 `tree` 用作 `expected_tree`。
注记只存在于 H08 服务层，H04/H05 原工具结果不变；确定性适配器在读取时
剥离注记，因此两种适配器行为一致。

输出字节按模型实际需要生成的 `content` 或 `new_text` 计量，并写入工具结果
（`data.output_bytes` / `data.protocol`）；默认单补丁上限 65,536 字节，与
H05 一致。大文件小改因此只传片段，不再整文件回传。合成证据为受限模型
（2,048 字节输出上限）：同一个约 100 KB 文件，整文件重写被截断为
`model_length`，增量替换在同一上限内完成并通过验收；H07 的弱化检测对新
协议同样生效（用替换删断言仍不能完成）。

真实模型证据（受控单次，2026-11-11）：34,178 字节的 `big.py`，真实
`deepseek-chat` 在 H06 容器内完成 `target(n)` 的单行修改，6 次模型调用 /
8 次工具调用，模型输出 **922 字节**，费用 17,182 micros（约 $0.017），
基线失败 → 修改 → 最终测试通过，Diff 仅一行，未出现
`PATCH_LIMIT`/`PATCH_TRUNCATED`。该证据只覆盖一次小规模真实调用；H12 的
付费模型验收门仍未完成。

浏览器入口与版本化 API（H08 阶段一）：首页 `agent.html` 新增“编码任务”
模式，读取 `GET /api/v3/harness/profile` 与 `/projects`，提交
`POST /api/v3/harness/tasks`，再轮询 `GET /tasks/{id}`，展示
`GET /tasks/{id}/diff`（纯文本）与 `GET /tasks/{id}/patch`（附件下载），
产物清单在 `GET /tasks/{id}/artifacts`。legacy 页面与分析入口未改动；
所有路由继续走 `IsolatedExecutionBroker` + `CommandSession` 的 lease /
epoch / allowlist / 预算，没有新增第二条执行路径。

`CodingResult.changes[]`（以及 API `result.json`）的 `before_sha256` 直接取
任务工作区 `baseline.json` 记录的摘要字符串，不再二次哈希；`after_sha256`
是磁盘后像的真实摘要。删除按 H07 `LocalWorkspace` 既有语义上报为
`modified` + `after_sha256 = sha256("")`，重命名呈现为一次新增加一次删除；
未修改文件不出现在 `changes` 中。独立复验可直接用
`sha256sum`/`hashlib` 对照磁盘校验每个条目。

`GET /tasks/{id}` 与 `/tasks/{id}/patch` 在拼接文件路径前要求 `task_id`
`fullmatch ^h08-[0-9a-f]{12}$`，编码点段（`%2e%2e`）、编码斜杠、空/超长/
非十六进制 id 一律 404 `HARNESS_TASK_UNKNOWN`，不会读到 `<harness_root>`
下的其他文件。
