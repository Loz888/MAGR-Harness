# GA02 与 GA03 独立复验

2026-11-19，验收源码 `1c882622a5f066cdd14b30c58eb33fe669729739`，原交付 `94c83dd` 只增加验收文档。**GA02 注册验收通过，独立验收 FAIL。GA03 本地工程独立验收 INDEPENDENT_PASS。** 源码标准指纹在两个独立环境复跑前后均为 `a558c59b24f53d29755788a4ba2d839016cad53f31323d6f36d1f76436e7d190`。迁移 `0059_general_task_graphs.sql` 和原始回执哈希已核对。[GA02 原回执](accept/GA02/accept.json) · [GA03 原回执](accept/GA03/accept.json)

| 独立复跑目标 | 通过 | 失败 | 错误 | 跳过 |
| --- | ---: | ---: | ---: | ---: |
| GA02 专项 | 15 | 0 | 0 | 0 |
| GA03 专项 | 8 | 0 | 0 | 0 |
| GA01 兼容 | 34 | 0 | 0 | 0 |
| H07 兼容 | 29 | 0 | 0 | 0 |
| H10 兼容 | 80 | 0 | 0 | 0 |
| MCP 兼容 | 10 | 0 | 0 | 0 |
| **注册与兼容合计** | **176** | **0** | **0** | **0** |

在独立 PostgreSQL 16 和 Redis 7.4 上复跑后，176 个唯一节点逐项对应原始 JUnit。Ruff 和 mypy 通过。归档的开发阶段失败保持原样。专项通过涵盖依赖失败向下游传播、审批与事件等待、并发预算、重启认领、旧修订回执隔离和 CodingLoop 复用。[GA02 机器记录](accept/GA02/independent-20261119.json)

## GA02 阻塞项

在另一个隔离 PostgreSQL 上安装包含报告节点和一个可选等待节点的任务图。报告节点提交成功回执，产出唯一必需交付物。通过局部重规划移除尚未认领的可选等待节点后，图修订递增，唯一剩余节点为 `succeeded`，交付物、回执和资源账均保留。**任务仍为 `running`，且无节点可认领，无法进入 GA01 的结果验证阶段。** 单节点对照图由正常回执结算进入 `verifying`。[局部重规划](../../src/mgar_v3/general_agent/graph_store.py#L338) · [阶段推进](../../src/mgar_v3/general_agent/graph_store.py#L691)

`replan` 保存新计划和图后直接返回，完成状态推进只在信号与回执结算路径触发。重规划应使用保存新计划后的任务版本，按新图检查是否可以进入验证阶段。修复后只需定向复测本轮新增的失败场景，核对重规划后状态和交付物、预算均正确。

## GA03 证据与范围

GA03 专项核实了 Native、MCP 与领域执行器按同一描述注册，MCP 发现只更新已固定条目的健康状态，不能授予权限。模型先复查当前有效认领再看到选中工具的唯一 Schema。受控传输和真实 PostgreSQL 记录了超时、版本差异、资源、输入模式等回退理由。[能力目录](../../src/mgar_v3/general_agent/capabilities.py) · [MCP 发现](../../src/mgar_v3/general_agent/mcp_discovery.py) · [GA03 机器记录](accept/GA03/independent-20261119.json)

真实模型调用 0 次，GA02 真实模型规划和 GA03 远程 MCP 联调为 `NOT_RUN`。Research、Data 和 Office 的生产执行器仍待接入。隔离工作树和容器已清理。GA04 等待授权。[任务队列](TASKS.md)
