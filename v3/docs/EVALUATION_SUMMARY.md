# 如何理解 Harness 的实验

这些实验回答不同问题，使用不同候选、样本和验证方式。数字不是同一次运行，也不构成整体产品通过率；原报告、失败和未运行项均保留在[实验档案](../experiments/capability-eval-v1/README.md)。

| 问题 | 结果与限制 | 来源 |
|---|---|---|
| 冻结编码题的质量如何？ | 2026-11-18 H12 R08 formal-v5 独立质量门：开发和持出各 11/12。2026-11-21 Phase 3 另有 32 次真实模型试次，19 PASS、7 FAIL、6 BLOCKED；独立操作者复核 NOT_RUN。两组候选与范围不同，不合并分母。 | [H12 独立复核](harness-v4/H12_R08_INDEPENDENT_MATRIX_REVIEW_20261118.md) · [Phase 3 真实模型](../experiments/capability-eval-v1/phase3/live/report.md) |
| 检索混合策略是否改善质量？ | E1 对 315 条开发、210 条独立持出各运行六组；持出候选 F Recall@5 为 27.61%，BM25 为 68.56%，可答覆盖 65/150；独立质量门失败。工程接通不是质量提升。 | [Phase 2](../experiments/capability-eval-v1/phase2/report.md) · [独立判定](../experiments/capability-eval-v1/phase2/e1/independent_review.md) |
| 压缩节省输入是否保持任务质量？ | E2 的 1,440 个真实模型试次中，独立持出 MAGR+CAS 为 65/90，完整历史 83/90；差 −20 个百分点，未达冻结的 −5 个百分点非劣门槛。持出输入节省 9.35%，不等于质量非劣。 | [Phase 2](../experiments/capability-eval-v1/phase2/report.md) |
| 跨会话记忆带来什么？ | E3 的 40 条合成只读任务链共 640 个真实模型会话；四组各 160 会话成功。关闭记忆组 320 次调用，其他组各 247；本次支持调用效率改善，未建立成功率收益。 | [Phase 2](../experiments/capability-eval-v1/phase2/report.md) · [独立复核](../experiments/capability-eval-v1/phase2/e3/independent_review.md) |
| 自动写入何时开放？ | E4 最终候选 `9455688` 的 448 条盲测实测 38 条，其中 31 符合、7 不符，遇到无外部确认的自动 verified 后停止；410 条未运行，质量门仍 FAIL。未运行项不能移出分母后当作完整通过率。 | [独立盲测记录](../experiments/capability-eval-v1/phase1-write-repair/report.md) |
| 委派是否改善结果？ | 2026-11-21 H17 受控任务单 Agent 87/90、委派可用臂 80/90，但实际专家调用为 0；不能据此评价真正发生专家介入时的优劣。项目默认单 Agent。 | [Phase 3](../experiments/capability-eval-v1/phase3/live/report.md) · [H17](harness-v4/H17_INDEPENDENT_ACCEPTANCE_20261119.md) |
| 查询性能瓶颈在哪里？ | E5-R1 本地隔离 PostgreSQL 性能候选通过 172 格、36,864 次检索；减少逐条明细 SQL 的路径。候选仍在实验分支与隔离 PG，不代表整体发布，历史 E1 质量门仍 FAIL。 | [Phase 4 报告与环境](../experiments/capability-eval-v1/phase4/report.md) |

## 记忆写入为何仍然受限

E4 的三轮盲测各自冻结，不能将未运行样本合并或丢弃：

- 首轮 `ff1562c`：372 条封存、15 条执行、357 条未执行；敏感记忆误入库的硬门失败保留，之后夹具争锁中断使完整成绩 BLOCKED。[Phase 1](../experiments/capability-eval-v1/phase1/report.md)
- 修复候选 `c863196`：360 条封存、4 条评分、356 条未运行；第 4 条敏感内容错误自动标记 verified，独立结论 FAIL，没有评分设施阻塞。[独立盲测](../experiments/capability-eval-v1/phase1-write-repair/c863196-review/report.md)
- 后续候选 `9455688`：448 条封存、38 条评分、410 条未运行；指令型内容错误自动标记 verified 后停止，独立结论仍 FAIL。公开工程回归通过不能补足正式盲测缺失的样本。[独立复核](../experiments/capability-eval-v1/phase1-write-repair/independent_review.md)

实验档案的 [publication.json](../experiments/capability-eval-v1/publication.json) 登记公开材料的 SHA-256；私有持出原件与逐次供应商回执的保管范围以原索引说明为准。[当前状态](CURRENT_STATUS.md) · [项目说明](PROJECT_OVERVIEW.md)
