# GA05 VerificationPlan 与 Evidence Contract

GA04 工程验收通过并落库于 `125b33a` 后开始 GA05 开发。新增 `0061` 迁移，保存验证计划、证据版本、验证报告和人工确认。[迁移](../../migrations/0061_general_verification.sql) · [GA04 先行验收](accept/GA04/accept.json)

## 契约与完成状态

验证计划与任务 ID、任务修订和当前计划版本绑定，列出预期交付物以及每项的机械检查、所需证据、来源版本和新鲜度。采集服务将证据内容哈希和认证后的采集身份登记到 PostgreSQL。证据追加新版本，执行和采集身份与验证器分离。受信验证服务在短事务之外读取 CAS 及外部动作回执，然后重新核对任务修订、证据版本和当前图交付物。[契约](../../src/mgar_v3/general_agent/verification_contracts.py) · [验证存储](../../src/mgar_v3/general_agent/verification_store.py)

| 检查 | 最低证明 |
| --- | --- |
| Coding | Diff 的工作区、前后文件树与补丁字节。测试或构建的命令、环境、文件树、退出码、来源，测试需有非零用例且零失败 |
| Research | 固定版本的来源、证据快照以及报告引用的原文片段 |
| Data | 固定数据集的 JSON 数值字段、样本数量和可重算的 sum、mean、count |
| Office 草稿 | 固定文档版本及可核对的引用 |
| Email 和 Calendar | GA04 已结算动作的任务修订、计划、操作、目标及持久化外部回执 |
| Browser | 固定来源的页面快照、最终页面地址和预期文本 |

检查失败、证据缺失或过期时，报告为 `unverified`，任务可继续补证。人工确认固定结果摘要哈希，证据替换会使旧确认失效。通用任务图或动作参与时，`GeneralTaskStore.finish` 检查当前有效的持久化验证记录及人工确认要求，且当前任务修订须没有待对账动作。历史 GA01 独立任务继续使用已验收的原结果契约。[机械检查](../../src/mgar_v3/general_agent/verification_checks.py) · [完成入口](../../src/mgar_v3/general_agent/store.py)

## 工程验收与复验

```bash
make lint
make typecheck
make verify TASK=GA05
make verify TASK=GA04
make verify TASK=GA02-REPLAN-REPAIR
make verify TASK=GA01
```

最终源码 `93eb6d3` 上，GA05 专项 16/16、GA04 专项 15/15、GA02 定向 1/1、GA01 兼容 34/34 共 66 个唯一用例通过。零失败、错误或跳过，Ruff 和 mypy 均通过。

定向验收覆盖伪造完成回执、证据更替、独立验证身份、未解决的外部副作用、重启后的确认和历史迁移兼容。Coding 夹具执行本地 unittest 与构建检查，邮件、日历与浏览器使用受控专用夹具。本地工程独立复验通过。生产供应商、真实模型和外部端到端为 `NOT_RUN`。[机器回执](accept/GA05/accept.json)

[独立复验](GA02_GA04_GA05_INDEPENDENT_20261119.md)
