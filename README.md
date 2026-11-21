# MAGR-Harness

MAGR-Harness 是一个独立的研发 Agent 项目，帮助使用者提出任务、在授权范围内执行，并根据测试、产物和回执审查结果。它也提供可复用的单次任务执行能力。项目源码发行版为 0.1.0；[当前状态](v3/docs/CURRENT_STATUS.md)按候选和验证范围区分可用入口、工程证据与尚未放行的能力。

一次真实记录展示了为什么任务需要可检查的恢复过程：数据库导入完成后，运行目录因权限故障无法发布。CLI 指明数据库已恢复；恢复权限后，操作者使用相同快照、数据库、目标和命令完成剩余发布，并核对 CAS 字节和收据。这是 2026-11-19 在 WSL2 上对候选 `8a6489b` 的本地故障注入，定向 **1/1**，没有真实模型调用；生产和原生 Linux 宿主未验收。[恢复案例与原记录](v3/docs/PROJECT_OVERVIEW.md#数据库已经恢复运行目录发布失败后怎样继续)

## 一个任务怎样推进

- **明确目标与范围：** 在工作台提交需求、补充交付物和完成条件；个人任务访问项目资源时需要关联已授权项目。
- **执行与观察：** 研发任务可使用隔离工作区、项目指导及获准工具；执行过程和产物供使用者查看。
- **验证与审查：** 测试和验证回执为结果提供依据；工具调用成功不等于业务任务已完成或获准发布。
- **恢复与交付：** 检查阶段状态与已知回执后继续适用的操作；外部操作结果未知时先核对，不盲目重试。

通用工作台提供个人需求记录、补充和规划；项目助手与 GitHub 分析有各自入口和授权。跨领域真实自动执行取决于独立验收与具体执行入口，不能从页面可访问推断已通过。更多任务过程见[项目说明](v3/docs/PROJECT_OVERVIEW.md)与 [GA13 候选说明](v3/docs/general-agent-v1/GA13_RC.md)。

## 当前边界

GA13 候选 `175cabc` 的本地独立工程复验通过，RC 发布仍 **BLOCKED**；专用账户真实 Coding、Data、Office 和跨领域端到端为 **NOT_RUN**，真实任务集与门槛待冻结。历史 H12 冻结质量矩阵开发集和持出集各 11/12，属于该实验候选；较新的能力实验及其限制另行记录。上述结果不代表当前源码已完成新的全量验证。[当前状态与来源](v3/docs/CURRENT_STATUS.md) · [实验解读](v3/docs/EVALUATION_SUMMARY.md)

## 与 Super-Agent 的关系

[Magr-super-agent](https://github.com/Loz888/Magr-super-agent) 关注同一研发问题跨消息与事件的接续；Harness 关注单次任务执行、验证与恢复。历史交接和已接收契约有固定版本，尚未找到两个当前公开基线完成联合部署验收的记录。[版本与验证范围](v3/docs/CURRENT_STATUS.md#与-super-agent-的关系)

## 开始使用

准备 Git、Make、uv、Python 3.12、Docker 和 Docker Compose。Windows 用户可参阅 [WSL2 安装说明](v3/README.md#wsl2-首次启动)。

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

打开 `http://127.0.0.1:18080`，首次使用时创建管理员账户。实际入口与依赖准备见[应用说明](v3/README.md)、[工作台候选说明](v3/docs/general-agent-v1/GA13_RC.md)和[安装与恢复](v3/docs/harness-v4/H18_OPERATIONS.md)。

[文档导航](v3/docs/README.md) · [项目说明](v3/docs/PROJECT_OVERVIEW.md) · [实验档案](v3/experiments/capability-eval-v1/README.md) · [历史公开快照](PUBLICATION.md)
