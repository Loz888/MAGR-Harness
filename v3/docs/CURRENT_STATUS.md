# MAGR-Harness 当前状态

本页是本仓库的状态入口。核查基线为公开提交 [`c07fdd5c2b696a8148e4295a0b6909686b90a76b`](https://github.com/Loz888/MAGR-Harness/tree/c07fdd5c2b696a8148e4295a0b6909686b90a76b)，核查日期 2026-11-21。以下结果绑定各自来源中的候选与时间，不表示该基线重新运行过全部检查。

| 对象 | 证据与结论 | 范围和限制 |
|---|---|---|
| 研发 Agent 与工作台 | 根首页提供独立项目 0.1.0 和启动入口。GA13 描述个人需求记录、补充、计划与可选项目关联。[应用说明](../README.md) · [GA13](general-agent-v1/GA13_RC.md) | 项目访问需相应授权；实时模型对话、跨领域自动执行取决于具体入口及独立验收，不由入口存在推定。 |
| GA13 候选 | 2026-11-19 源码 `175cabc` 本地工程独立复验通过；首轮 38/40，补齐沙箱镜像锁后只重跑阻塞的两项 2/2，同源合并 40/40。[报告](general-agent-v1/GA13_INDEPENDENT_20261119.md) · [机器记录](general-agent-v1/accept/GA13/independent-20261119.json) | RC 发布 **BLOCKED**；专用账户真实 Coding、Data、Office、跨领域端到端 **NOT_RUN**，真实任务集和门槛待冻结；本次真实模型调用及外部写入为 0。 |
| H12 历史质量矩阵 | 2026-11-18 `hv4-h12-r08-20261118-formal-v5`：开发 11/12、独立持出 11/12，质量门 PASS；24 个首样本中 23 个发生供应商调用。[独立复核](harness-v4/H12_R08_INDEPENDENT_MATRIX_REVIEW_20261118.md) | 该固定实验的产品发布状态为 NOT_RELEASED；不能把质量门写成当前源码重验或整体发布通过。 |
| H13–H18 工程 | 各卡本地独立工程证据分别归档。[H13](harness-v4/H13_INDEPENDENT_ACCEPTANCE_20261118.md) · [H14](harness-v4/H14_INDEPENDENT_ACCEPTANCE_20261118.md) · [H15](harness-v4/H15_INDEPENDENT_ACCEPTANCE_20261118.md) · [H16](harness-v4/H16_INDEPENDENT_ACCEPTANCE_20261118.md) · [H17](harness-v4/H17_INDEPENDENT_ACCEPTANCE_20261119.md) · [H18](harness-v4/H18_PUBLISH_RETRY_INDEPENDENT_20261119.md) | H18 发布故障定向复验 1/1；原 146 项属于较早源码。真实模型调用 0，生产和原生 Linux 宿主 NOT_RUN。 |
| 能力实验 | 2026-11-20 至 21 的各轮实验中 E1 检索质量门失败、E2 未达非劣、E4 写入失败、E5 本地性能候选通过；项目整体发布仍 BLOCKED。[实验档案](../experiments/capability-eval-v1/README.md) · [解读](EVALUATION_SUMMARY.md) | 不同候选、样本和门分别记录；性能通过不覆盖检索质量失败。Phase 3 真实模型复测 H12 为 19 PASS、7 FAIL、6 BLOCKED，独立操作者复核 NOT_RUN，不能和历史 22/24 合并。 |
| 默认执行方式 | 项目首页记录默认单 Agent，H17 受控实验未建立委派收益。[首页基线](https://github.com/Loz888/MAGR-Harness/blob/c07fdd5c2b696a8148e4295a0b6909686b90a76b/README.md) · [Phase 3](../experiments/capability-eval-v1/phase3/live/report.md) | 委派可用臂实际专家调用为 0；不能推断实际多专家推理效果。 |

## 与 Super-Agent 的关系

| 层次 | 已知范围 |
|---|---|
| 设计职责 | Harness 处理单 Run 的工具循环与执行证据；[Super-Agent](https://github.com/Loz888/Magr-super-agent) 的设计围绕来源、持续任务、路由、调度及反馈。设计描述见其[固定架构稿](https://github.com/Loz888/Magr-super-agent/blob/3fce8c15f9d9dd6ba6315889a33790ad58775666/v3/docs/super-agent-v1/ARCHITECTURE.md)，该稿标记 PLANNED。 |
| 历史接收 | 下游 SA00 记录 H00 owner `de4775e` 和本地合入 `1a65b83`；H01 owner `38a65ba` 的 `harness-contract-v1.1` 仅按契约范围接收，含本地注册验证与独立权限探针。[SA00 机器记录](https://github.com/Loz888/Magr-super-agent/blob/3fce8c15f9d9dd6ba6315889a33790ad58775666/v3/docs/super-agent-v1/accept/SAV1-00/accept.json) |
| 下游试用 | 下游 2026-11-18 的 L02 在特定 Worker `2f5205a` 和隔离候选 `f8a0b14` 上记录真实飞书回执、补丁和定向检查，正式 Verifier 为 incomplete。[下游进展](https://github.com/Loz888/Magr-super-agent/blob/3fce8c15f9d9dd6ba6315889a33790ad58775666/v3/docs/super-agent-v1/SA22_PROGRESS_20261118.md) |
| 尚未确认 | 未找到本页基线与下游公开基线 `3fce8c1` 组合完成联合部署验收的记录；也未找到下游接收 H12 formal-v5 并重验其试用门的记录。上游固定矩阵 PASS 和下游记录的试用质量 FAIL 各自保留，互不自动覆盖。 |

[项目说明](PROJECT_OVERVIEW.md) · [实验解读](EVALUATION_SUMMARY.md) · [文档导航](README.md) · [Super-Agent 当前状态](https://github.com/Loz888/Magr-super-agent/blob/main/v3/docs/CURRENT_STATUS.md)
