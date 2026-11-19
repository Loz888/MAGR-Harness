# GA09 Coding Executor

GA09 将 TaskGraph 当前认领交给已有 Harness CodingLoop。新能力 `coding.execute.inspect` 与 `coding.execute` 和原 Coding 能力并存。原 Harness 请求保存完整认领、授权、请求摘要和有界预算。子任务 ID 由认领确定，重复提交和重启后继续使用原任务与 Run。派发、恢复和受保护操作都会核对当前任务图、能力版本、权限、期限与成员身份。[执行器](../../src/mgar_v3/general_agent/coding_executor.py) · [Harness 服务](../../src/mgar_v3/harness/service.py)

`CodingExecutor.start` 接收已获准的图认领和服务端配置。inspect 使用授权路径；bugfix、feature、refactor 的确定性适配器要求明确批准及匹配原文的替换内容；run_app 使用已批准的启动计划和既有隔离服务生命周期。命令继续经原 Broker，TaskGraph 预占能力资源，原 Harness 预算记录子任务实际用量。[派发契约](../../src/mgar_v3/general_agent/coding_executor_contract.py)

`pause` 和 `resume` 沿用同一子任务与 Run。已知结果在 `poll` 时结算到图，失败传播到下游，回执不明时保留未知。GA05 独立核对只读检查或服务回执的原 Harness 结果、Run 和最终文件树；补丁和测试产物沿用独立 Diff 与命令检查。有效验收记录绑定当前图产物，然后经既有发布入口完成任务。[验证存储](../../src/mgar_v3/general_agent/verification_store.py)

## 验收

```bash
make verify TASK=GA09
```

专项覆盖只读检查、暂停恢复、失败传播、排队恢复、模型结果已结算后的恢复、离线 Python 修复，以及本地 HTTP 服务就绪与退出。feature 和 refactor 沿用批准后的补丁路径，四种需批准的策略均覆盖拒绝场景。生产模型和发布为 `NOT_RUN`。

H11/H12 的早期兼容检查为 158 项通过、3 项受冻结 H00 文件缺失影响。在独立检出的 `fee1914` 基线复现了相同三项。最终源码 `45971ec` 从历史归档按原摘要恢复输入后，H12 派发检查 10/10、R08 定向工程回归 67/67 通过。H11 注册回归 68/68 通过。各轮原始回执及最终兼容统计均可复核。[集成验收记录](accept/GA06-GA10/accept.json)
