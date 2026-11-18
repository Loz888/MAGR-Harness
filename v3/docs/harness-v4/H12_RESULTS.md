# H12 交付与真实评测

H12 已实施并完成可执行范围的评测，待独立复验。编码 MVP 未放行。

源码：`ce338bfdf1294a446ec2aafd58eebcb6af2f8626`；指纹：`84ad1b9af3e998f77af01075866044f34c98cc933fae622ede57e3900e09c7d2`。

工程：H12 注册 177 项、H00–H12 聚合 526 项、旧上下文与恢复 24 项通过，lint/typecheck 通过。注册项包含在聚合内，不能相加。

| 集合 | PASS | FAIL | NOT_RUN | UNKNOWN | 冻结门槛 |
|---|---:|---:|---:|---:|---|
| 开发集 | 0/12 | 7 | 5 | 0 | 10/12 |
| 持出集 | 0/12 | 5 | 7 | 0 | 9/12 |

全部候选累计 195 份供应商派发记录，保守费用上界 US$0.839914；预占 0 micros，未知调用 0。总授权上限 US$12，所有候选与对照共用同一 PG 预算。

首轮 v3 与最终 v4 单独报告，不合并通过率。v3 原始成绩不覆盖，核账更正为 7 FAIL、5 NOT_RUN，51 次调用、185573 micros；更正未调用模型。v4 仅修评分/异常收口和公开参考资产加载，模型配置与工具求解策略保持。持出模型只在 v4 执行一次，结果未用于后续调参。

五门：工程 PASS（列明范围）、安全 PASS（列明范围）、真实模型 PASS、真实 Agent FAIL、真实发布 NOT_RUN。已有 H09/H10 OPEN 不因本轮工程通过而关闭。

主要边界：非法或跨产物分页游标会被拒绝并终止任务；非许可命令被拒绝；大产物保存可能失败；源码符号链接、Go 版本、依赖或固定沙箱资源可能导致 NOT_RUN。所有未运行题目仍保留在 12 的分母内。

旧版同模型共享预算适配器未就绪，对照为 NOT_RUN；没有据此推断 Harness 收益或产品排名。未推送、未发布、未开始 H13。

完整机器记录：[accept.json](accept/HV4-H12/accept.json)。运行与复现：[H12_EVALUATION.md](H12_EVALUATION.md)。

| 集合 | 任务 ID | 状态 | 原因 | 模型调用 | 费用上界 micros |
|---|---|---|---|---:|---:|
| dev | hv4-dev-click-help-name | FAIL | CONTEXT_CURSOR_INVALID | 4 | 8987 |
| dev | hv4-dev-click-multifile-placeholder | FAIL | CONTEXT_CURSOR_INVALID | 15 | 58057 |
| dev | hv4-dev-click-short-help | FAIL | HARNESS_COMMAND_DENIED | 15 | 68870 |
| dev | hv4-dev-click-style-refactor | FAIL | CONTEXT_CURSOR_INVALID | 3 | 4837 |
| dev | hv4-dev-express-healthz | FAIL | ARTIFACT_TOO_LARGE | 20 | 101139 |
| dev | hv4-dev-express-server-lifecycle | FAIL | ARTIFACT_TOO_LARGE | 19 | 97461 |
| dev | hv4-dev-got-header-value | NOT_RUN | DEPENDENCY_OR_LOG_UNAVAILABLE | 0 | 0 |
| dev | hv4-dev-got-local-roundtrip | NOT_RUN | DEPENDENCY_OR_LOG_UNAVAILABLE | 0 | 0 |
| dev | hv4-dev-pflag-unknown-flag | FAIL | CONTEXT_CURSOR_INVALID | 4 | 7318 |
| dev | hv4-dev-requests-content-type | NOT_RUN | SOURCE_SYMLINK_DENIED | 0 | 0 |
| dev | hv4-dev-requests-encoding-refactor | NOT_RUN | SOURCE_SYMLINK_DENIED | 0 | 0 |
| dev | hv4-dev-requests-http-url-helper | NOT_RUN | SOURCE_SYMLINK_DENIED | 0 | 0 |
