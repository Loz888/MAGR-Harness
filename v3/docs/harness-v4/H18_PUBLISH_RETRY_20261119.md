# H18 运行目录发布权限中断定向复测

源码 `8a6489b` 的权限中断复测 **PASS**，1 项通过，零失败、错误或跳过。[原独立失败报告](H18_RESTORE_RETRY_INDEPENDENT_20261119.md) · [机器记录](accept/HV4-H18/publish-retry-20261119.json)

- 修复前，在真实创建运行目录后将父目录权限从 `0700` 改为 `0500`。复现 CLI 返回 `OPERATOR_FILESYSTEM_ERROR`，目标路径留下空目录。
- 修复后，发布通过一次原子重命名完成，并保护已有目录。在发布操作前将父目录改为 `0500`，内核拒绝重命名。CLI 保留 `SNAPSHOT_RUNTIME_PUBLISH_FAILED` 和 `database_restored: true`，目标路径仍空缺，父目录权限保持 `0500`。暂存目录的清理错误单独记录。
- 恢复父目录写权限后，保持快照、数据库、运行路径和命令不变。测试同时让 `pg_restore` 在 PATH 中不可用，原命令直接完成发布，证明没有再次导入。已导入的迁移和恢复后的 CAS 字节均通过核验。

仅复测本次权限中断。Ruff 和 mypy 通过，真实模型调用 0。既往独立结果按原源码保留。发行包 `magr-harness-8a6489b.tar.gz` 的 SHA-256 为 `7a4cfdfc20108268e21560d28e6346193beef61c0c3ee35c2c749d39ecafa872`。[运维指南](H18_OPERATIONS.md)
