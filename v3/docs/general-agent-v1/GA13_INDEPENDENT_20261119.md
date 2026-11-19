# GA13 RC 独立复验 · 2026-11-19

**本地工程 INDEPENDENT_PASS，RC 发布 BLOCKED。** 源码 `175cabc` 在单独 Git 检出、锁定依赖、真实 PostgreSQL 16、pgvector 和 Redis 上复验。`make verify TASK=GA13` 首轮执行 40 项，38 项通过，复验环境缺少沙箱镜像锁导致两项 Coding 测试受阻。补齐镜像锁后只重跑这两项，2/2 通过。同源合并结果为 40/40。Ruff、mypy 和锁文件检查通过。[原验收](accept/GA13/accept.json) · [独立机器记录](accept/GA13/independent-20261119.json)

| 范围 | 独立结果 |
| --- | --- |
| 三端与浏览器 | 注册测试通过实际 HTTP 核对 Web、CLI 和 SDK 的同一任务修订与计划。解包服务在临时端口 `43526` 上通过 Edge 实测，包括账户初始化、自然语言创建、待补充、修订、生成计划和刷新后恢复。 |
| 升级与回滚 | 登记的真实数据库演练通过，从 0057 升级到 0062，并将升级前快照恢复到独立数据库和运行目录，核对会话与 CAS。 |
| 工作流与 Office | Coding、Research、Data、Office 和跨领域本地受控链路通过。恢复后的模拟 Office `unknown` 动作通过原操作回执对账，只发生一次模拟写入。 |
| Worker | 实际以 SIGKILL 终止独立子进程。读取到原认领，租约到期进入 `unknown`。认领和工具调用各一次，重复结算原回执仍幂等，任务进入 `verifying`。 |
| 发行包 | 两次重建与原归档字节相同，1,498 份文件哈希匹配。解包后离线 `make setup`、迁移和实际 HTTP 启动通过。公开开发集与原封存持出集主评测组各 60/60，私有原件和回执在仓库外。 |

`18080` 在复验前后为同一 H13 进程，工作目录为 `/home/imini/magr-harness-h13-runtime/v3`。通用任务页面和 API 返回 404，OpenAPI 无对应路由。只有独立候选占用的临时服务和容器参与启动、测试及清理。[环境回执](accept/GA13/independent-20261119.json)

GA12 的持出准入独立通过记录 `40e8dbf` 是本候选直接父提交，该部分源码没有发生变化。公开 Research 探测再次触发 `RESEARCH_SCOPE`。专用账户真实 Coding、Data、Office、跨领域端到端均为 `NOT_RUN`，真实任务集和分数门槛待冻结。真实模型调用、外部写入均为 0。[GA12 独立复验](GA12_HOLDOUT_ADMISSION_REPAIR_INDEPENDENT_20261119.md) · [GA13 任务书](TASKBOOK_V1.md#ga13--general-agent-rc-集成验收)
