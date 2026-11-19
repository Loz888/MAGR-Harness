# General Agent 任务队列

任务逐张授权。GA00 冻结和架构盘点已完成独立复验，原冻结工程基线为 FAIL。修复候选的同源合并工程复验为 PASS，修复独立复验待执行。GA01 等待授权。[任务书原文](TASKBOOK_V1.md)

| 卡片 | 内容 | 依赖 | 当前状态 |
| --- | --- | --- | --- |
| GA00 | 当前版本冻结与架构盘点 | 无 | 冻结与盘点 INDEPENDENT_PASS，修复工程复验 PASS |
| GA01 | GeneralTaskSpec 与 Outcome Contract | GA00 | TODO |
| GA02 | General Planner 与动态 TaskGraph | GA01 | TODO |
| GA03 | Capability Registry 与 Domain Router | GA01 | TODO |
| GA04 | Universal ActionContract 与副作用治理 | GA01、GA03 | TODO |
| GA05 | VerificationPlan 与 Evidence Contract | GA01、GA04 | TODO |
| GA06 | Web Research Executor | GA02、GA03、GA05 | TODO |
| GA07 | Data Analysis Executor | GA02、GA03、GA04、GA05 | TODO |
| GA08 | Office Automation Executor | GA03、GA04、GA05 | TODO |
| GA09 | Coding Executor 统一适配 | GA01、GA03、GA05 | TODO |
| GA10 | Personal / Cross-domain Memory | GA01、GA05 | TODO |
| GA11 | Cross-domain Context & Knowledge Gateway | GA03、GA05、GA10 | TODO |
| GA12 | General Agent Benchmark Suite | GA01–GA09 | TODO |
| GA13 | General Agent RC 集成验收 | GA00–GA12 | TODO |

[GA00 冻结报告](GA00_BASELINE_20261119.md) · [调用链与差距矩阵](GA00_ARCHITECTURE.md) · [独立复验入口](GA00_RECHECK.md) · [机器验收记录](accept/GA00/accept.json) · [独立复验报告](GA00_INDEPENDENT_20261119.md) · [工程基线修复](GA00_ENGINEERING_REPAIR_20261119.md)
