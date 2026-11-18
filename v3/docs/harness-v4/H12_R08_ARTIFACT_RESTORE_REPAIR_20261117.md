# H12 R08 已知命令拒绝后的产物读取修复

修复位于独立分支 `hv4-h12-r08-wire-repair-20261117`。冻结评分源码仍是
`04ad15c6a5ba493a7f8fb110388131229af9865d`；开发题 03 的历史 FAIL 不变。

已只读核对原 PG 与 CAS：题 03 的 `h08-560d36cc9c47:tool:13:1` 是 known 的
`run_tests` 命令纠错回执，`HARNESS_COMMAND_DENIED`、`executed=false`，没有执行回执
或日志。CAS `d8e29ab866f4695a686bd9bd7d069668287358da02ed3e49d79b056a38552c21`
与 PG 完全相同。到第 18 次模型调用请求 `artifact_reread` 时，`restore_known`
遍历这条回执，误以为是“已执行但还未归档”的命令，调用
`ExecutionReceipt.model_validate(None)`，产生原记录中的 `ValidationError`。
空路径本身是合法的日志路径，不是此次失败原因。

修复先验证原 CAS/PG 回执，再识别明确 `executed=false` 的命令纠错／拒绝：保留
回执，但不把它当日志恢复。若这类回执同时声称有执行产物，或状态与未执行声明矛盾，
仍拒绝。真实已执行且测试失败的命令照常恢复其日志；损坏执行回执返回明确的 scope
错误，不能成为未经验证的日志。原任务、Run、grant、lease、epoch 和原产物绑定不变。

仅定向测试，无付费调用、无完整工程重跑：

| 检查 | 结果 |
|---|---|
| 两种已知未执行回执后读取合法源产物 | 原源码 2 FAIL；修复后 2 PASS |
| 未执行回执不能夹带日志引用 | PASS |
| 真实失败执行回执仍可恢复并读取日志 | PASS |
| 独立真实 PG、原 Broker、受控模型：拒绝命令→读源→reread→修复→固定原套件 | 原源码 FAIL；修复后 PASS |

合计 5 项通过。`attempt-002` 的 4 PASS 保留；其中一个负例的测试错误地把错误码
当异常消息匹配，修正断言后仅重跑该失败项，`attempt-003` 1 PASS。没有重复已通过项。
原失败、JUnit 和日志位于 `evidence/harness-v4/HV4-H12-R08-ARTIFACT/attempt-001`。
make lint、make typecheck（285 个源码文件）通过，设置 `UV_NO_SYNC=true` 保持共享
锁定环境不被重新安装。真实 PG 测试使用它自己创建的唯一临时数据库，不修改 H12
的原预算数据库。受控模型由测试禁止真实供应商网络。

原问题逐字段证据另存于冻结评测工作树的
`evidence/harness-v4/HV4-H12-R08/artifact-restore-original-pg-proof.json`。
本交付没有把任一历史失败改为通过，也不构成 H12/MVP 放行。
