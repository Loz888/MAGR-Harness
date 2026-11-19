# General Agent 任务队列

GA00 冻结和架构盘点已完成独立复验，原冻结工程基线为 FAIL，修复定向独立复验为 INDEPENDENT_PASS。GA01 工程验收和独立复验均通过。GA03 独立工程复验通过。GA02 修复通过定向独立复验，GA05 本地工程独立复验通过。GA04 已通过定向独立复验，Outbox 队首阻塞已关闭。GA06–GA10 已复现原工程矩阵。GA07、GA09、GA10 独立工程复验通过，GA06、GA08 的独立补充项失败。[任务书原文](TASKBOOK_V1.md)

| 卡片 | 内容 | 依赖 | 当前状态 |
| --- | --- | --- | --- |
| GA00 | 当前版本冻结与架构盘点 | 无 | 冻结与盘点 INDEPENDENT_PASS，修复定向复验 INDEPENDENT_PASS |
| [GA01](GA01_TASK_OUTCOME.md) | GeneralTaskSpec 与 Outcome Contract | GA00 | INDEPENDENT_PASS |
| [GA02](GA02_TASK_GRAPH.md) | General Planner 与动态 TaskGraph | GA01 | 修复定向 INDEPENDENT_PASS，真实模型规划 NOT_RUN |
| [GA03](GA03_CAPABILITY_ROUTING.md) | Capability Registry 与 Domain Router | GA01 | INDEPENDENT_PASS，远程 MCP 联调 NOT_RUN |
| [GA04](GA04_ACTION_CONTRACT.md) | Universal ActionContract 与副作用治理 | GA01、GA03 | 本地工程 INDEPENDENT_PASS，原阻塞已关闭 |
| [GA05](GA05_VERIFICATION_CONTRACT.md) | VerificationPlan 与 Evidence Contract | GA01、GA04 | 本地工程 INDEPENDENT_PASS |
| [GA06](GA06_WEB_RESEARCH_EXECUTOR.md) | Web Research Executor | GA02、GA03、GA05 | INDEPENDENT_FAIL，PDF 正文缺字，公开 Web BLOCKED |
| [GA07](GA07_DATA_EXECUTOR.md) | Data Analysis Executor | GA02、GA03、GA04、GA05 | INDEPENDENT_PASS，桌面 Excel NOT_RUN |
| [GA08](GA08_OFFICE_EXECUTOR.md) | Office Automation Executor | GA03、GA04、GA05 | INDEPENDENT_FAIL，跨项目日历动作身份冲突，生产账户 NOT_RUN |
| [GA09](GA09_CODING_EXECUTOR.md) | Coding Executor 统一适配 | GA01、GA03、GA05 | INDEPENDENT_PASS，真实模型 NOT_RUN |
| [GA10](GA10_PERSONAL_MEMORY.md) | Personal / Cross-domain Memory | GA01、GA05 | INDEPENDENT_PASS |
| GA11 | Cross-domain Context & Knowledge Gateway | GA03、GA05、GA10 | TODO |
| GA12 | General Agent Benchmark Suite | GA01–GA09 | TODO |
| GA13 | General Agent RC 集成验收 | GA00–GA12 | TODO |

[GA00 冻结报告](GA00_BASELINE_20261119.md) · [调用链与差距矩阵](GA00_ARCHITECTURE.md) · [独立复验入口](GA00_RECHECK.md) · [机器验收记录](accept/GA00/accept.json) · [独立复验报告](GA00_INDEPENDENT_20261119.md) · [工程基线修复](GA00_ENGINEERING_REPAIR_20261119.md) · [修复独立复验](GA00_ENGINEERING_REPAIR_INDEPENDENT_20261119.md) · [GA01 独立复验](GA01_INDEPENDENT_20261119.md) · [GA02 与 GA03 独立复验](GA02_GA03_INDEPENDENT_20261119.md)

[GA02 重规划修复](GA02_REPLAN_REPAIR_20261119.md) · [GA02、GA04 与 GA05 独立复验](GA02_GA04_GA05_INDEPENDENT_20261119.md) · [GA04 修复独立复验](GA04_PAYLOAD_REPAIR_INDEPENDENT_20261119.md)

最终源码 `45971ec` 通过 GA06–GA10 和混合任务图的 65 项专项，计入兼容回归共 443 个唯一用例通过。公开来源读取被地址策略阻止，Office 专用生产账户端到端与付费模型实验待跑。[GA06–GA10 集成验收](accept/GA06-GA10/accept.json)

[GA06–GA10 独立复验](GA06_GA10_INDEPENDENT_20261119.md) · [机器记录](accept/GA06-GA10/independent-20261119.json)
