# H13 独立验收（2026-11-18）

H13 工程验收和本机运行核验均为 PASS。实现提交为 `108a0b6`，集成记录提交为
`f09ac0b`。两处工作树的标准源码指纹一致，均为 `1de02f0c565aaed4bd0bae7630c9842d7161b44c314c95640ffd4e3b9cf07c2c`。

独立启动专用 PostgreSQL 16／pgvector 与 Redis，执行原注册清单。H13 通过
42 项，Web／RepoSage 通过 111 项，均为 0 失败、错误、跳过。Ruff 与 mypy 通过，
mypy 覆盖 288 个源码文件。已核验本次两个临时容器均已清理。

H13 注册检查覆盖重试回查原任务、准备中退出码、完成边界的末尾事件、调度器归属，
以及重启恢复后同一 Run 的暂停、恢复和取消。旧 `LIVE_PROFILE_REQUIRED` 失败后
在同一会话创建新回合的路径已在隔离测试中执行，模型使用受控夹具。

运行中的 18080 进程从 `/home/imini/magr-harness-h13-runtime/v3` 启动。首页和健康
接口返回 200，匿名会话请求返回 401。逐项比对 6 个现有会话的 HTTP、CLI 和 Python
SDK 导出，结果完全一致。3 个旧失败显示真实原因，事件流可从尾部游标读取终态。
2 个注册项目环境就绪且授权有效，读取到 30 条既有 RepoSage 记录，达到本次查询上限。
本机核验全部为查询请求。首次 WSL 连接超时，仅重试该读操作后完成。

真实模型检查为 `NOT_RUN`，新增 DeepSeek 调用 0。验收范围为 H13 工程与本机运行。

[机器验收记录](accept/HV4-H13/independent-20261118.json)。原始日志、JUnit 和
只读探针证据位于 `evidence/harness-v4/H13-INDEPENDENT-20261118/da33f66e087d`。
