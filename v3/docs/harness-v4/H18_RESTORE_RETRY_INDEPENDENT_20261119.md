# H18 恢复重试独立复验

2026-11-19，源码 `cefa3860f879e8f503903039954e996c6aa7eda6`。修复提交与复跑工作树 `2a45bee` 之间只有验收文档。标准源码指纹为 `f19775395efc62aee8a9b961f6ffaf04cf5578855f3f34b218e319aa2f62698f`，前后相同。**原故障已修复，独立工程门仍为 FAIL**，因为数据库导入后，运行目录发布到一半仍可阻塞同目标重试。[原独立报告](H18_INDEPENDENT_ACCEPTANCE_20261119.md) · [修复方定向回执](accept/HV4-H18/restore-retry-20261119.json) · [本轮机器记录](accept/HV4-H18/restore-retry-independent-20261119.json)

## 已通过

- 仅复跑 `HV4-H18-RESTORE-RETRY`，1/1 通过，无失败、错误或跳过。缺少 `pg_restore` 时目标库为空、运行目录未创建。恢复 PATH 后用相同快照、目标库、目录和参数重试成功，原 Cookie 可通过本地 HTTP 登录，CAS 字节一致。
- 同一用例在进入目录发布前注入错误。CLI 返回 `SNAPSHOT_RUNTIME_PUBLISH_FAILED` 和 `database_restored: true`。解除故障后，原命令即使找不到 `pg_restore` 也能继续发布，证实没有重复导入。原 Cookie 和 CAS 同样通过核验。
- Ruff、mypy 均通过。修正版发行包重建后逐字节一致，SHA-256 为 `a54dba1fbbb6fb5c6746256ad118c16f1413b316b379500683d9a5b0cd93e967`。逐文件核对清单的 1,353 项和修复方的 7 份引用证据，哈希均一致。

## 发布过程中失去写权限，原命令仍无法重试

从修正版发行包加载代码，在新建的隔离 PostgreSQL 和临时文件系统目录上测试。完成数据库导入后，在真实 `mkdir(runtime)` 返回后立即将父目录从 `0700` 改为 `0500`，让重命名与清理遇到实际的权限错误。第一次 CLI 返回 `OPERATOR_FILESYSTEM_ERROR`。35 个数据库迁移和已导入收据都在，运行目录也留在原处且为空。恢复 `0700` 权限后，保持快照、数据库、目录和命令不变，CLI 仍返回 `SNAPSHOT_DESTINATION_EXISTS`。[目录发布实现](../../src/mgar_v3/ops/snapshot.py#L86) · [恢复入口](../../src/mgar_v3/ops/snapshot.py#L258)

应让发布一步完成且拒绝覆盖已有目录，或通过匹配的收据识别并接续本次恢复创建的空目录。后续清理错误也应保留已完成数据库导入的诊断。修复后仅重跑本轮失败的已建目录场景，核对恢复权限后原命令继续发布且不重复导入。

历史通过的其他注册用例未复跑。真实模型调用 0，生产与原生 Linux 宿主机为 `NOT_RUN`。本轮隔离服务已清理。
