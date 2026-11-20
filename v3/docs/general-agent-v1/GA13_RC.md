# GA13 通用 Agent 候选集成

通用智能体是首页，`/general` 也可直接打开。登录后可以直接描述需求。个人任务按当前浏览器账号保存，选择或关联项目为可选操作。访问仓库或项目知识时，任务需要关联已有授权的项目。关联已有个人任务会增加同一任务的修订，保留此前的需求。通过「准备任务计划」确认任务方向、交付物和完成条件，补充消息保存在任务修订中。侧栏展示当前账号可访问的最近任务。项目助手、GitHub 分析、Runner 配对和交付可从侧栏进入。[任务规范](GA01_TASK_OUTCOME.md) · [计划和节点](GA02_TASK_GRAPH.md)

个人入口支持需求记录、补充和规划。实时模型对话与跨领域自动执行取决于独立验收和执行入口，个人任务本身不会授予任何项目权限。

## CLI 与 SDK

在和候选服务相同的机器上，CLI 使用 `MGAR_V3_LOCAL_TOKEN` 或候选目录中的 `.env`。CLI 项目任务的初始请求包含项目 ID、固定的客户端请求 UUID 和自然语言目标。保留该 UUID 可在连接中断后重试同一创建请求。个人任务使用登录浏览器的账号会话。

```json
{
  "project_id": "OPENED_PROJECT_ID",
  "request_id": "426efd19-ffb7-4785-8d3c-40ef53159b20",
  "spec": {"goal": "根据已提供的资料整理一份研究报告"}
}
```

```bash
uv run --frozen mgar --api http://127.0.0.1:18081 harness general submit --input request.json
uv run --frozen mgar --api http://127.0.0.1:18081 harness general task "$TASK_ID"
uv run --frozen mgar --api http://127.0.0.1:18081 harness general revise "$TASK_ID" --input revision.json
uv run --frozen mgar --api http://127.0.0.1:18081 harness general plan "$TASK_ID" --input plan.json
uv run --frozen mgar --api http://127.0.0.1:18081 harness general graph "$TASK_ID"
```

`revision.json` 提供 `expected_revision`、`expected_version` 和完整 `spec`。例如研究任务需要 `domain=research`、明确的交付物与通过条件，以及计划引用的 `allowed_capabilities`，如 `research.sources`。`plan.json` 提供修订后返回的 `expected_revision` 与 `expected_version`。命令退出码 0 表示 API 请求成功，任务终态在响应的 `state` 中。SDK 的对应方法为 `create_general_task`、`general_task`、`revise_general_task`、`plan_general_task` 和 `general_task_graph`。[SDK](../../src/mgar_v3/harness/client.py)

## 安装与回滚

在 Git 检出上运行 `make verify TASK=GA13`。使用独立测试数据库、Redis 和 CAS 目录。源码归档由 `scripts/build_harness_release.py --revision COMMIT --output /path/to/magr-harness.tar.gz` 生成。使用归档的整体 SHA-256 和 `RELEASE.json` 逐文件校验。GA12 运行时公开文件随归档交付，私有持出包由独立评测环境保管。

运行需要 Python 3.12、uv、Docker Compose、PostgreSQL 16/pgvector、Redis 7.4，以及 PostgreSQL 16 工具 `pg_dump` 和 `pg_restore`。编码沙箱需要固定镜像。为运行目录和备份预留足够空间容纳数据库、CAS、工作区和一份完整恢复副本。对照已有监听端口选择一组空闲端口，首次 `make setup` 前设置数据库和 Redis 的端口变量。

```bash
export COMPOSE_PROJECT=magr-ga13-rc
export MGAR_V3_DB_HOST_PORT=55434 MGAR_V3_REDIS_HOST_PORT=56380
make setup
make deps-up
make migrate
make sandbox-build
make doctor
uv run --frozen uvicorn mgar_v3.api:create_app --factory --host 127.0.0.1 --port 18081
```

使用 `http://127.0.0.1:18081/static/agent.html` 初始化本地试用账户。前向升级前停止派发，在安全点备份数据库、CAS 和凭据密钥，并保留工作区及配置。回滚时用旧发行包指向从升级前快照恢复的新数据库和运行目录，核对旧登录、迁移历史及产物哈希。如果外部动作在切换前为 `unknown`，从供应商查询原操作回执后再决定后续动作。[备份、恢复与回滚命令](../harness-v4/H18_OPERATIONS.md)

## 发布条件

GA13 分别记录本地工程、独立验收、专用账户真实端到端和真实题集成绩。GA12 持出准入修复与原 GA13 本地工程候选已通过独立复验。正式真实任务集门槛仍待冻结。跨领域工作流的本地验证使用受控执行器。真实模型、外部账户、公开 Web 和平台依赖以各自回执为准。[GA12 独立复验](GA12_HOLDOUT_ADMISSION_REPAIR_INDEPENDENT_20261119.md) · [GA13 独立复验](GA13_INDEPENDENT_20261119.md)
