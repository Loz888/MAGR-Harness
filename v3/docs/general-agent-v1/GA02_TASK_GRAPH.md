# GA02 General Planner 与动态 TaskGraph

基线为 GA01 独立复验提交 `d78612b`，与 GA03 共用候选源码 `1c88262`。PostgreSQL 迁移 `0059_general_task_graphs.sql` 保存任务图快照和事件。Engine 继续持有写锁，既有 GA01 任务版本与结果验证协议保留。[迁移](../../migrations/0059_general_task_graphs.sql)

## 规划与执行交接

- 确定性规划从已澄清的 `GeneralTaskSpec` 生成 DAG。当任务同时声明 Web 与数据集输入以及表格或图表、报告交付时，生成研究、数据分析、报告三个相依节点，并检查声明的能力。其他任务可使用单领域计划或经验证的显式 DAG。[规划实现](../../src/mgar_v3/general_agent/graph.py)
- 执行节点声明输入连线、具名输出、能力备选、预算、超时、最大尝试次数和完成条件。上游失败时阻塞下游。条件分支可跳过，汇合处可显式接受被跳过的上游。等待事件与审批使用当前图绑定的服务端信号。
- 每次认领先核对当前任务、图修订、成员权限、并行组和预算，再持久化操作 ID 和路由理由。已确认的幂等失败可在上限内重试。超时或回执不明时保留原认领和预算，待核对原操作。节点回执须匹配任务修订、节点内容和所选能力。所有规定交付物齐备后，任务进入 GA01 的验证阶段。
- 新证据、失败、用户指令和外部事件可作为重规划触发原因。重规划用图修订和快照版本做 CAS，同步更新任务的计划版本，保留无关分支的认领、回执及累计资源。修改范围内的未核对认领和已认领的非幂等操作会阻止替换。[图存储](../../src/mgar_v3/general_agent/graph_store.py)
- Coding 节点复用既有 `CodingTaskRequest` 和 CodingLoop。专项在实际注册的本地工作区完成只读检查。审批和实际执行继续由现有执行入口检查，外部写入按 Action/Outbox 边界处理。

## 已认证接口

| 方法 | 路径 | 请求要求 |
| --- | --- | --- |
| POST | `/api/v3/general/tasks/{task_id}/graph` | `expected_revision`、`expected_version`。可提供 `proposal`，省略则使用确定性规划 |
| GET | `/api/v3/general/tasks/{task_id}/graph` | 读取当前任务修订的快照 |
| PATCH | `/api/v3/general/tasks/{task_id}/graph` | `expected_revision`、`expected_graph_revision`、`expected_graph_version`、`trigger`、`proposal` |
| GET | `/api/v3/general/tasks/{task_id}/graph/events` | 读取当前任务修订最近 200 条事件 |

模型规划位于服务端 `plan_with_model`。接入方负责选取已授权并计量的适配器。请求包含任务和输出 Schema，调用在 Engine 事务外完成，返回后重新检查任务版本和 DAG。认领、审批信号、回执与工具授权使用内部端口。[接口定义](../../src/mgar_v3/general_agent/api.py)

## 验收与复验

以配置好独立 PostgreSQL 和 Redis 的锁定环境执行下列命令。专项覆盖跨领域依赖、条件汇合、事件和审批、预算与并发、部分重规划、晚到回执、取消撤权、事务故障及 Engine 重启。

```bash
make lint
make typecheck
make verify TASK=GA02
make verify TASK=GA01
make verify TASK=HV4-H07
make verify TASK=HV4-H10
```

专项 15/15、GA01 兼容 34/34、H07 只读与 Coding 策略回归 29/29、H10 上下文与工具回归 80/80 均通过，无失败、错误或跳过。GA01 兼容包含从旧版本恢复并升级的真实 PostgreSQL 用例。Ruff 和 mypy 均通过，注册测试执行前后源码指纹一致。

真实模型规划评测和外部端到端为 `NOT_RUN`，真实模型调用为 0。独立复验发现局部重规划后的阶段推进阻塞，结果为 FAIL。源码指纹、原始通过回执及开发阶段的失败记录见[验收记录](accept/GA02/accept.json)。

[独立复验](GA02_GA03_INDEPENDENT_20261119.md)
