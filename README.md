# MAGR-Harness

MAGR-Harness 是独立的研发 Agent 项目。你可以直接描述需求，让 Agent 调查 GitHub 仓库、理解本地项目、在隔离工作区提出修改，并根据测试回执审查结果。项目使用自己的版本号，当前源码发行版为 **0.1.0**。

## 开始使用

准备 Git、Make、uv、Python 3.12、Docker 和 Docker Compose。Windows 用户可使用 [WSL2 安装说明](v3/README.md#wsl2-首次启动)。

```bash
git clone https://github.com/Loz888/MAGR-Harness.git
cd MAGR-Harness/v3
uv python install 3.12
make setup
make deps-up
make migrate
make sandbox-build
make doctor
make dev
```

打开 `http://127.0.0.1:18080` 进入通用智能体工作台，首次使用创建管理员账户。登录后可以直接提交个人任务，访问项目资源时再关联已授权的项目。提交后可补充任务范围、查看计划和已验证结果。项目助手和 GitHub 分析可从侧栏进入，并使用各自的模型与项目授权。[通用工作台](v3/docs/general-agent-v1/GA13_RC.md) · [项目助手](v3/docs/agent-ui.md) · [安装与恢复](v3/docs/harness-v4/H18_OPERATIONS.md)

## 已交付能力

| 能力 | 内容 |
| --- | --- |
| 项目 Agent | 自然语言任务、可恢复会话、流式执行过程、固定测试、补丁审查 |
| GitHub 仓库分析 | 固定提交、只读取证、证据引用、持续追问 |
| CLI 与 Python SDK | 使用同一套任务、事件和产物接口进行自动化 |
| 项目指导与工具集成 | AGENTS.md、版本化扩展及授权 MCP |
| Runner 与多用户 | 配对设备运行固定计划、项目和凭据隔离、队列与取消恢复 |
| 只读仓库专家 | 操作者可按需开启，共享 Run 预算，默认单 Agent |
| 运维与恢复 | 发行包校验、doctor、PostgreSQL 与 CAS 快照、脱敏指标 |

项目以 PostgreSQL 保存权威任务状态，使用 Docker 隔离项目执行。模型调用和外部写入受原有授权、预算和审批约束。[技术报告](v3/docs/TECHNICAL_REPORT.md) · [CLI 与 SDK](v3/docs/harness-v4/HEADLESS.md) · [扩展与 MCP](v3/docs/harness-v4/PROJECT_GUIDANCE_AND_PLUGINS.md)

## 验收记录

- H12 的冻结真实模型质量评测共通过 22/24，开发集与独立持出集各 11/12。该记录的产品发布状态为 `NOT_RELEASED`。[独立质量复核](v3/docs/harness-v4/H12_R08_INDEPENDENT_MATRIX_REVIEW_20261118.md)
- H13 至 H18 的本地独立工程验收已归档。H17 受控对照支持保留单 Agent 默认设置。H18 复核通过 WSL2 安装、Debian 12 容器安装及恢复权限后用原命令继续发布。原生 Linux 宿主机和生产多机验收为 `NOT_RUN`。[H13](v3/docs/harness-v4/H13_INDEPENDENT_ACCEPTANCE_20261118.md) · [H14](v3/docs/harness-v4/H14_INDEPENDENT_ACCEPTANCE_20261118.md) · [H15](v3/docs/harness-v4/H15_INDEPENDENT_ACCEPTANCE_20261118.md) · [H16](v3/docs/harness-v4/H16_INDEPENDENT_ACCEPTANCE_20261118.md) · [H17](v3/docs/harness-v4/H17_INDEPENDENT_ACCEPTANCE_20261119.md) · [H18](v3/docs/harness-v4/H18_PUBLISH_RETRY_INDEPENDENT_20261119.md)

[文档导航](v3/docs/README.md) · [任务卡](v3/docs/harness-v4/TASKS.md) · [历史公开快照说明](PUBLICATION.md)
