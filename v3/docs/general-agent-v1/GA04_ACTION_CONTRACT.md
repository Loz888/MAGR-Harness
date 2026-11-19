# GA04 Universal ActionContract 与副作用治理

GA04 为通用任务增加一条由 Engine 持久化的动作通道。迁移 `0060` 保存租约、动作意图、Outbox 和审计事件。原 Coding 与 GitHub 专用交付仍走各自已验收的通道。[动作实现](../../src/mgar_v3/general_agent/actions.py) · [迁移](../../migrations/0060_general_actions.sql)

## 动作从审查到回执

1. 受信服务为当前任务修订和计划签发租约。授权固定操作者、项目、能力版本、提供方、目标、操作、副作用等级与模式。新租约递增 epoch 并撤回旧租约。
2. 提交 ActionIntent 时校验租约以及任务风险、授权范围和幂等键。审批固定意图摘要、租约和 epoch。发布、删除、金融动作及无补偿的写入需要更高审批等级。
3. Dispatcher 读取已获批的候选，先校验 CAS 载荷，再读取版本前置条件。Broker 检查当前成员、授权、租约、审批、版本与剩余额度。认领与预算在同一事务持久化。节点与动作共用任务修订的资源上限。
4. 提供方适配器在事务外使用原幂等键和版本条件执行。明确拒绝终止；断连和超时保留未知状态、原认领及预算。同一目标的新写入在未知期间等待。对账只查原键，成功回执绑定意图、目标和提供方。 [Dispatcher](../../src/mgar_v3/effects/dispatcher.py)

动作端口供受信适配器、授权服务和审批服务调用。发现的 MCP 写工具仍经通用路由拒绝直调。平台接入生产写适配器时，适配器需固定到获授权的专用账户并发送原幂等键和版本条件。生产写入同时要求部署写入开关和显式作用域授权。

## 工程验收

```bash
make lint
make typecheck
make verify TASK=GA04
```

隔离 PostgreSQL 与专用夹具账户覆盖审批、越权、租约轮换、撤权、预算共用、并发重投、事务回滚、重启和未知对账。生产远程写入为 `NOT_RUN`，真实模型调用为 0。[先行验收](accept/GA04/accept.json) · [最终集成回执](accept/GA05/accept.json)

注册测试独立复跑通过。补充预检故障用例发现，队首载荷缺失会阻塞同一提供方的后续动作，独立验收为 FAIL。

[独立复验](GA02_GA04_GA05_INDEPENDENT_20261119.md)

## 队首载荷缺失修复

载荷缺失或完整性校验失败时，Engine 将仍未认领的动作置为 `failed`，同时写入失败码、预检阶段和载荷哈希。下一轮轮询可继续处理其他已批准的动作。

源码 `b5b9b83` 在隔离 PostgreSQL 上定向通过 1/1。首个动作没有派发或预算预占，另一目标的动作在下一轮成功执行。原独立失败记录保留，修复已通过定向独立复验。[定向回执](accept/GA04/payload-repair-20261119.json)

```bash
make verify TASK=GA04-PAYLOAD-REPAIR
```

[修复独立复验](GA04_PAYLOAD_REPAIR_INDEPENDENT_20261119.md)
