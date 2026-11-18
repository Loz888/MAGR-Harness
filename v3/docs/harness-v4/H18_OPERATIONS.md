# H18 安装、升级与恢复

## 安装发行包

在源码仓库执行 `make release RELEASE_OUTPUT=/path/to/magr-harness.tar.gz` 生成指定提交的发行包和 SHA-256。发行包含应用、依赖锁、迁移、Compose、沙箱定义和逐文件校验。校验后解包进入 `magr-harness`。需要 Python 3.12、uv、Docker 和 Docker Compose。备份和恢复另需与 PostgreSQL 服务器兼容的 `pg_dump` 和 `pg_restore`。

```bash
make setup
make deps-up
make migrate
make sandbox-build
make doctor
make dev
```

本地网页为 `http://127.0.0.1:18080`，首次安装从网页初始化管理员。首次安装需要下载固定依赖和镜像。已准备相同版本缓存的机器可复用依赖。启动独立测试实例时，在首次 `make setup` 前为 `MGAR_V3_DB_HOST_PORT` 和 `MGAR_V3_REDIS_HOST_PORT` 指定不同的空闲端口，并在所有 Compose 命令中用同一个 `COMPOSE_PROJECT`。

## 诊断

`make doctor` 返回 JSON。可使用 `uv run --frozen mgar doctor --project /path/to/project` 检查项目，或用 `--network-url https://example.com/` 在主机上检查指定 HTTPS 端点。

| 诊断 | 处理 |
| --- | --- |
| `CONFIGURATION_INVALID` | 根据 fields 补齐本机设置。新安装用 `make setup` 初始化。 |
| `PG_AUTHENTICATION_FAILED` | 核对数据库实际角色密码与 DSN。已有数据卷时，修改初始化环境变量不会重设角色密码。 |
| `PG_UNREACHABLE` | 核对服务状态、地址和端口，查看 Compose 的 PostgreSQL 日志。 |
| `SCHEMA_UPGRADE_REQUIRED` | 备份后应用列出的前向迁移。 |
| `SCHEMA_CHECKSUM_MISMATCH` 或 `SCHEMA_NEWER_THAN_RELEASE` | 核对迁移校验值，找到与数据库迁移历史匹配的发行包。 |
| `DOCKER_UNREACHABLE`、`SANDBOX_LOCK_MISSING`、`SANDBOX_START_FAILED` | 分别检查 Docker 守护进程、固定镜像锁和容器启动。 |
| `NETWORK_ACCESS_DENIED` | 按返回状态码核对主机代理和访问授权。 |

## 升级与回滚

在隔离或维护窗口停止派发并让 Run 到达安全点。先将当前数据库、CAS 和凭据密钥导出到新目录，记录所用发行提交。项目原件、Harness 工作区根目录和运行配置由原部署工具一并保存。运行新版本的 `make migrate` 和 `make doctor`，再启动控制器并核对原登录及历史 Run。重复执行迁移可核对待执行数量为零。

```bash
uv run --frozen mgar ops backup /path/to/new-backup
uv run --frozen mgar ops restore /path/to/new-backup \
  --target-dsn-env MGAR_RESTORE_DATABASE_URL \
  --runtime /path/to/new-runtime
```

预先创建目标空数据库，并用指定的环境变量提供 DSN。恢复会校验快照文件，在新目录恢复 `artifacts/` 和 `web/credentials.key`，以事务导入目标数据库并复核迁移历史及 CAS。回滚时将对应的旧发行包指向恢复库、恢复目录下的 `artifacts/` 和原工作区卷。数据库集群角色和授权由原部署工具恢复。Web 账户、会话随数据库恢复，已加密的凭据使用备份内的密钥恢复。

## 指标与保留

使用 `uv run --frozen mgar ops metrics --hours 24 --limit 200` 导出本机指标，或用 `--run-id` 查看单个 Run。结果包括 Harness 请求、Run 和操作标识，模型、工具、命令、验证和排队耗时，以及已确认和预占费用。HTTP 事件流的 `X-MGAR-Request-ID` 与断线记录相同，客户端可用 `Last-Event-ID` 续读。结果过多时返回 `truncated`，可按 Run 缩小范围。导出仅提取允许的字段，源码、模型上下文、命令参数、原始日志和凭据仍由原有访问权限控制。

快照目录由操作者私有管理。保留最近一次已核验可恢复的快照及升级前快照。生成并验证新恢复点后，可清理过期的独立备份、发行归档和已外存的验收目录。CAS 与其数据库引用按同一恢复点保留。
