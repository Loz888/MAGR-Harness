# MAGR-Harness 文档导航

从[项目首页](../../README.md)开始，依次了解任务过程、当前范围与证据。当前结论以[当前状态](CURRENT_STATUS.md)中标明的版本与验证范围为准；以下历史设计和验收保留当时的状态。

## 了解与使用

- [项目说明：从目标到可审查的结果](PROJECT_OVERVIEW.md)
- [安装、启动与 WSL2](../README.md) · [通用工作台候选](general-agent-v1/GA13_RC.md) · [项目助手](agent-ui.md)
- [CLI 与 SDK](harness-v4/HEADLESS.md) · [安装与恢复](harness-v4/H18_OPERATIONS.md)

## 理解设计

- [项目指导、扩展与 MCP](harness-v4/PROJECT_GUIDANCE_AND_PLUGINS.md) · [Harness 任务与设计档案](harness-v4/README.md)
- [通用 Agent 任务书](general-agent-v1/TASKBOOK_V1.md) · [状态与任务图](general-agent-v1/GA02_TASK_GRAPH.md)
- [与 Super-Agent 的关系](CURRENT_STATUS.md#与-super-agent-的关系) · [历史交接原件](harness-v4/SUPER_AGENT_HANDOFF.md)

## 核查证据

- [当前状态与未决事项](CURRENT_STATUS.md) · [实验解读](EVALUATION_SUMMARY.md) · [能力实验原始档案](../experiments/capability-eval-v1/README.md)
- [GA13 独立复验](general-agent-v1/GA13_INDEPENDENT_20261119.md) · [H12 冻结质量矩阵](harness-v4/H12_R08_INDEPENDENT_MATRIX_REVIEW_20261118.md) · [H18 定向恢复复验](harness-v4/H18_PUBLISH_RETRY_INDEPENDENT_20261119.md)
- [历史公开快照与范围](../../PUBLICATION.md) · [发布索引](../../PUBLICATION.json)

## 开发与历史档案

- [Harness 任务卡](harness-v4/TASKS.md) · [General Agent 任务队列](general-agent-v1/TASKS.md) · [ADR](adr/)
- [V3.1 历史技术报告](TECHNICAL_REPORT.md) · [当时的 PDF](reports/20261106/MGAR-V3.1-Technical-Report.pdf) · [事实索引](reports/20261106/report-data.json)
- [V3.5 历史演示](v35/DEMO.md) · [P0 历史交付](repairs/P0-20261107/README.md) · [原实施档案](repairs/P0-20261106/README.md)

以下按阶段保留设计、历史验收和问题档案；较旧状态不能替代当前状态页。

## 运行与架构

| 阶段 | 设计 | 操作或验收 |
|---|---|---|
| R0/R1 基础与首个真实交付 | [边界ADR](adr/0001-v31-boundaries.md) | [R1真实验收](acceptance/r1-live-20261102.json) |
| R2 持续任务 | [Mailbox/安全点](adr/0002-r2-mailbox.md) | [工程验收](acceptance/r2-engineering-20261102.json) |
| R3 路由与任务关联 | [Router](adr/0003-r3-router.md) | [限定真实验收](acceptance/r3-20261102.json) |
| R4 跨源调查 | [知识/数据/专家](adr/0005-r4-investigation.md) | [运行](r4-runbook.md) · [真实对照](r4-live-report.md) |
| R5 受限修复 | [修复边界](adr/0007-r5-repair-boundaries.md) · [真实适配](adr/0008-r5-native-adapters.md) | [运行](r5-runbook.md) · [真实PR恢复](r5-live-report.md) |
| R6 监控/有限主动性 | [恢复请求](adr/0011-observe-only-watchdog.md) · [评测](adr/0012-r6-monitoring-proactivity-evaluation.md) | [运行](r6-runbook.md) · [故障矩阵](r6-failure-matrix.md) |
| 协调补验与修复 | [D/E/F设计](adr/0014-coordination-comparison.md) · [候选契约](adr/0015-coordination-candidate-contract.md) | [v1报告](experiments/coordination-v1/README.md) · [v2报告](experiments/coordination-v2/README.md) |
| 两项P0 | [冻结设计](adr/0016-p0-commerce-and-business-results-design.md) · [实施ADR0017](adr/0017-p0-commerce-implementation.md) · [复验ADR0018](adr/0018-exact-proposal-review.md) | [最终验收](repairs/P0-20261107/acceptance.json) · [原实验](repairs/P0-20261106/acceptance.json) · [运行](p0-commerce-runbook.md) |
| V3.2 通用仓库分析 | [运行手册](repo-analysis.md) | Web `/repo` · `mgar repo-analyze` · API `/api/v3/repository-analyses` |

## 结果与问题档案

| 档案 | 内容 |
|---|---|
| [R6完整修复](repairs/R6-20261103/README.md) | 46/90→74/90→87/90、数值/契约/权限/补丁协议修复、账单核对、原三项FAIL |
| [JSON模式请求事故](repairs/R6-20261103/json-mode-incident.md) | HTTP400、预检缺口、未知预占与严格账单核对 |
| [宿主与恢复](repairs/R6-HOST-20261105/README.md) | V2测试时钟维护、独立systemd VM、整虚拟节点/PG/角色/CAS恢复 |
| [E/F第二版修复](repairs/COORD-20261106/README.md) | 动作/UUID契约问题、级联失败、原子重核、恢复收据及Worker残余FAIL |
| [最终三臂逐项结果](../eval/reports/final-v6-results.json) | 30任务×3配置，87PASS/3FAIL |
| [最终三臂统计](../eval/reports/final-v6-summary.json) | 调用、费用、延迟、引用、置信区间和配对统计；消融NOT_RUN |

旧ADR和交付文件保留当时状态与限制；它们不会因后续修复被改写为“当时已通过”。最新结论应结合上述报告的时间和作用范围读取。
