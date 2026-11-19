# GA04 Outbox 载荷缺失定向独立复验

2026-11-19，GA04 本地工程独立验收 **INDEPENDENT_PASS**。原 Outbox 队首阻塞已关闭。验收源码 `b5b9b833a18780fd26de646ba45acc3ba6d9230e`，修复方交付 `3275cb1` 相对源码只修改验收文档。标准源码指纹复跑前后一致，为 `cc0416ea71c9374ed1175cf2eecdbbbd64e70ac944e2ce3b75e43d57d055ece3`。[原独立失败](GA02_GA04_GA05_INDEPENDENT_20261119.md) · [修复方定向回执](accept/GA04/payload-repair-20261119.json) · [本轮机器回执](accept/GA04/payload-repair-independent-20261119.json)

## 原失败场景

独立检出中使用专用 PostgreSQL 和受控提供方。两个目标不同的动作均已批准。第一个动作的本地 CAS 载荷消失后，首次轮询在认领前将其置为 `failed`。`failed` 事件持久化 `payload_preflight`、`ARTIFACT_MISSING` 和原载荷 SHA-256。该动作没有派发尝试、回执或预算预占，提供方没有收到观察或发送调用。第二轮轮询正常派发另一目标的已批准动作，产生唯一的成功回执。

`GA04-PAYLOAD-REPAIR` 1/1 通过，零失败、错误或跳过。Ruff 和 mypy 均通过。原修复方的前后 JUnit 与 7 份证据哈希均已核对。先前 15 项专项与集成结果按 `93eb6d3` 保留，本轮只重跑已知失败场景。真实模型及生产提供方调用为 0，外部端到端为 `NOT_RUN`。隔离工作树、临时数据库和容器已清理。[任务队列](TASKS.md)
