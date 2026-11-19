# GA01 通用任务与结果协议独立复验

2026-11-19，GA01 本地工程独立复验 **INDEPENDENT_PASS**。被验收源码 `01af7d16398b28990269147f0c6011622b7afb8f`，原交付 `c6674a4` 相对该源码仅改验收文档。独立检出的标准源码指纹前后均为 `81d3aa93947bd73f6047496582d2c0f5e8b9ccbc37f4a385b3e2704cd71d566e`。迁移 `0058_general_tasks.sql` 的哈希和原验收一致。[交付与复验入口](GA01_TASK_OUTCOME.md) · [原机器记录](accept/GA01/accept.json) · [独立机器记录](accept/GA01/independent-20261119.json)

## 独立测试

| 范围 | 通过 | 失败 | 错误 | 跳过 |
| --- | ---: | ---: | ---: | ---: |
| GA01 注册目标 | 34 | 0 | 0 | 0 |
| H07–H10 Coding 回归 | 138 | 0 | 0 | 0 |
| H11、H13 Coding 回归 | 105 | 0 | 0 | 0 |
| H14 与补充上下文回归 | 214 | 0 | 0 | 0 |
| H15–H18 Coding 回归 | 55 | 0 | 0 | 0 |
| **Coding 回归小计** | **512** | **0** | **0** | **0** |

使用隔离的 PostgreSQL 16、Redis 7.4 和已锁定依赖复跑，独立 JUnit 节点与原证据逐项一致。Coding 回归的 512 个节点组内去重，均来自 GA00 收集清单。两组中重复执行 H16 与 H18 各一个迁移恢复用例，跨组合计为 544 个唯一节点。Ruff 和 mypy 通过。原验收的 42 份证据引用逐项核对哈希一致，包含原 Coding 上下文失败及 12 次定向通过的回执。独立复跑再次通过该节点。[Coding 上下文实现](../../src/mgar_v3/harness/long_context.py)

## 复验结论

- 四领域共用版本化任务协议。仅给出目标时进入 `waiting` 与 `clarification_required`。补齐后通过带预期修订与版本的 API 转到 `ready`，历史目标保留。API 拒绝客户端写入计划和执行结果，并验证项目成员权限及撤权后的历史访问。[协议](../../src/mgar_v3/general_agent/contracts.py) · [API](../../src/mgar_v3/general_agent/api.py)
- 并发修订只接受一个写入，旧计划和验证途中迟到的成功结果被拒绝。完成前校验 CAS 中的交付物、原证据及回执，回执须绑定任务、当前修订、计划和验证版本。写入事务重新检查修订及版本，并原子保存结果与完成状态。测试使用受控验证器回执。[存储](../../src/mgar_v3/general_agent/store.py) · [专项测试](../../tests/test_general_agent_ga01.py)
- 五种 Coding Policy 可换接通用任务请求，适配过程不授予执行许可。旧 Coding 会话、权限、SDK、迁移恢复和上下文行为通过兼容测试。[适配器](../../src/mgar_v3/general_agent/coding.py)

真实模型调用 0 次，真实 Provider 和外部端到端验收为 `NOT_RUN`。独立工作树及专用容器已清理。GA02 等待批准。[任务队列](TASKS.md)
