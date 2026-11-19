# GA01 通用任务与结果协议

Coding、Research、Data 和 Office 共用 `GeneralTaskSpec` 和既有 Harness 认证。PostgreSQL 在同一个 Engine 中保存任务目标修订、计划、当前阶段、结果和证据引用。迁移 `0058_general_tasks.sql` 为追加式，原编码会话可继续通过原 Web、CLI 和 SDK 访问。[任务协议](../../src/mgar_v3/general_agent/contracts.py) · [Engine 存储](../../src/mgar_v3/general_agent/store.py)

## 任务入口

| 操作 | HTTP 路径 | 请求要点 |
| --- | --- | --- |
| 创建任务 | `POST /api/v3/general/tasks` | 提供 `project_id`、可重试的 UUID `request_id` 和 `spec` |
| 读取当前任务 | `GET /api/v3/general/tasks/{task_id}` | 返回目标、计划、状态、修订与结果 |
| 提交目标修订 | `POST /api/v3/general/tasks/{task_id}/revisions` | 提供完整的新 `spec`、`expected_revision` 和 `expected_version` |
| 读取旧修订 | `GET /api/v3/general/tasks/{task_id}/revisions/{revision}` | 支持已授权的历史查询 |

任务可以从一条需求开始。只填写 `spec.goal` 时，返回 `waiting`、`clarification_required` 与缺失字段。调用者可以保留具体疑问于 `open_questions`，待用户补齐再提交修订。齐备的任务进入 `ready`，附 `draft: true` 可保存草稿。截止时间要求时区，超过截止时间的已提交任务进入 `blocked`。

任务的预算、风险、能力范围和审批策略是申请约束。实际权限由既有项目授权机制决定。成员入口沿用有效项目成员身份，创建和修改要求 contributor。历史记录绑原成员身份，撤权后历史访问随即失效。计划和验收结果由服务端入口写入。

## 阶段和版本

- 目标变更递增 `revision`，保留旧目标、旧计划和旧结果。新修订由自己的目标重新判定就绪状态。
- 同一修订内，计划写入和状态转换递增 `version`。更新同时比较修订和版本，过期写入返回 `GENERAL_TASK_STALE`。
- 计划包含声明式步骤及其目标修订。执行状态支持 `draft`、`ready`、`running`、`waiting`、`blocked`、`verifying`、`completed`、`failed`、`cancelled`。开始运行前需要当前修订的计划。
- 最终成功由 `OutcomeContract` 提交。每个声明交付物绑定内容哈希，每条成功条件绑定可信验证器写入 CAS 的回执。回执需匹配任务、目标修订、计划版本、验证版本、条件及对应交付物。Engine 读取并核对交付物、原证据和回执，再次确认版本后原子完成入库。

`inspect`、`bugfix`、`feature`、`refactor`、`run_app` 可从原 `CodingTaskRequest` 映射为通用目标和验收条件，再转换回原请求类型。适配器生成未授权执行的请求，原预检、项目授权和 CodingLoop 验收继续由现有 Harness 处理。[编码适配器](../../src/mgar_v3/general_agent/coding.py)

## 独立复验

在同源码检出安装锁定依赖，配置隔离的 pgvector PostgreSQL 16 和 Redis 7.4，设置 `MGAR_V3_DATABASE_URL`、`MGAR_V3_REDIS_URL`、`MGAR_V3_LOCAL_TOKEN`。备份恢复用例需要 `pg_dump` 与 `pg_restore`。使用短临时目录运行。

```bash
uv sync --frozen
export TMPDIR="$(mktemp -d /tmp/ga1.XXXXXX)"
export MGAR_V3_ALLOW_PAID=0 MGAR_V3_ALLOW_EXTERNAL_WRITE=0
make lint
make typecheck
make verify TASK=GA01
```

复验覆盖四个领域的 Schema、澄清、旧计划与并发写入拦截、绑定证据的完成门、项目成员授权、旧会话和迁移恢复。全量 Coding 回归命令及原始报告见交付机器记录。
