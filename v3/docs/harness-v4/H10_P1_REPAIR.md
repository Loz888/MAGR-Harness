# H10 独立复验 P1 修复

当前源码：`c140dadf5aff517527c3c5e107843e215cc76556`。
标准源码指纹：`38a02725fe2a5b5967e2604951a0749d69aeea1c5b1640e6dc65901d10d10de8`。
状态：完整工程门通过，`PENDING_INDEPENDENT_RECHECK`；H10 尚未获得独立放行。

起点为纯文档提交 `282636a`，被复验源码为 `5707785`、指纹 `5529fbf5…`。
原独立 [F01 报告](accept/HV4-H10/independent-recheck-20261111T104041Z.json) 和
[F02 补充报告](accept/HV4-H10/independent-recheck-20261111T104041Z-supplement.json)
保持 FAIL，不用本次实现方测试改写历史裁决。那份非本轮创建的未跟踪复验记录
`independent-recheck-20261111T104754Z.json` 不修改、不暂存。

## F01：终态快照与完成条件

原缺陷是 `_finish` 发现树已变时只跳过应用验收清单，却仍沿用 `completed` 状态和
旧 verification。原最小复现重新运行仍失败；新增测试在旧实现为11失败、2正常通过。

修复将终态 snapshot、Diff、清单、工作摘要及补丁 hash 绑定为一份捕获。只有当前树、
verification 的树及任务归属一致、verification 为 verified，且全部必需项确实通过，
才允许完成。摘要构造不再自行重新取另一份树或 Diff。

Diff 读取、摘要持久化、终态事件及事件读取之后继续核树；观察到漂移后，该次完成
尝试保持 blocked，保存原 verification 的原树，不重签。终态 revision 递增后重新
保存当前树的未完成清单和 checkpoint。`run_finished` 带捕获树及 verification 树；
若事件回调之后发现漂移，追加 `terminal_invalidated` 和最终 blocked 事件，
不删除或暗改已经保存的历史事件。

最多捕获四轮。持续变化以 `HARNESS_TERMINAL_TREE_UNSTABLE` 失败关闭，不无限重试。
保证范围截止于最后一次终态观察：有限重读不能阻止观察后任意外部写入，也不构成
H11 的跨进程写者 fencing 或完整恢复。

回归文件 `tests/test_harness_v4_h10_finalization.py` 包括 Long/Simple 两类上下文、
verification 事件后、snapshot/Diff 返回后、摘要持久化中、完成事件中变树、必需项
pending、持久化失败、连续漂移及正常完成。原独立最小复现脚本保持不变，修复后退出0。

## F02：日志的实际生成权限

原缺陷是在命令结束后用当前租约为旧日志登记来源。真实容器负例在旧实现为7失败、
1通过，其中包含 wait、finished poll、read_log、archive、响应返回及同 ID/epoch
替换范围的场景。

Broker 从实际 `_authorize` 结果生成冻结的 `ExecutionProvenance`，包括操作、租约
ID/hash、actor/workspace/repository/epoch、有效读写范围、保护路径及 grant hash。
来源随 LaunchedCommand、CommandHandle、LogPage、ExecutionReceipt 传递，不能由
归档时的 `peek()` 冒充。已结束命令的缓存也不免除检查：poll 可返回标记 revoked
的元数据，日志读取始终检查来源；同 ID/epoch 而不同 hash 同样拒绝。

Registry 在日志读取后、严格归档后及响应返回前复查生成来源。真实命令只调用
`archive_command_log`，缺失来源失败关闭；归档及分页核对当前租约和 grant。
基线命令也采用严格入口，成功校验、归档之后才把含 stdout 的原 receipt 放入结果；
拒绝时只保留计数和不含原文的审计元数据，不能在拒绝响应中夹带旧日志。

旧回执和句柄的 provenance 默认 None，仍可读取旧数据；这些数据不能直接冒充真实
命令来源进行归档。原 `archive(kind="log")` 保留为受信调用方导入观察的兼容口，明确
标记 `trusted_import`，不声称建立了命令生成来源。生产 Registry 和基线已迁移到严格口。
没有新增执行入口、Engine 写者或数据库迁移，也没有从保存的来源恢复授权。

`tests/test_harness_v4_h10_lease_provenance.py` 使用真实固定 H04/H06 容器，覆盖10项，
包括基线适配器拿到真实回执后替换或撤权、来源正常时的逐字段和 CAS 一致性。

## 验证与交接

用户批准后，完整注册验收68项、H00–H10聚合281项、旧上下文15项均已通过，
无失败、错误或跳过；lint/typecheck已通过。两份新增边界测试共26项包含在完整门中，
不与聚合计数相加。
命令、JUnit、起止 HEAD/标准指纹、红测和重跑均保存到新的
`evidence/harness-v4/HV4-H10/<UTC>-repair-*` 目录，不覆盖独立复验或旧 attempt。
最新机器记录由 `accept/HV4-H10/accept.json` 的 `current_implementation` 指向。

沙箱 socket EPERM 和等待用户批准是已保留的历史阻塞。用户明确回复“批准”后，
已在沙箱外串行完成完整注册、完整聚合及真实 PostgreSQL 旧上下文回归。
未修改沙箱配置；每组起止标准指纹均为上述冻结源码指纹，历史失败和审批前的部分结果
继续保留，不用它们代替本轮完整结果。

本次仅修 F01/F02。付费调用、外发、推送为0，live 三门 NOT_RUN，H11/H12 未启动。
下一步交给另一会话按 [独立任务卡](CODEX_CLI_H10_REVIEW_TASK.md) 复验新
源码及两项负例；实现方不能将其升级为独立 ACCEPTED。
