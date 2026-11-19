# H18 恢复重试修复与定向复测

源码 `cefa386` 的恢复重试定向复测 **PASS**，1 项通过，零失败、错误或跳过。独立报告中的原失败保留，历史已通过的其他用例无需复跑。[原独立验收](H18_INDEPENDENT_ACCEPTANCE_20261119.md) · [定向验收记录](accept/HV4-H18/restore-retry-20261119.json)

- 修复前复现了相同故障。缺少 `pg_restore` 时，目标库为空，但运行目录留在原路径。
- 修复后，数据库导入和迁移、CAS 核验通过后才发布运行目录。执行进程的 PATH 暂时找不到 `pg_restore` 时，CLI 返回 `SNAPSHOT_POSTGRES_CLIENT_MISSING`，目标库仍为空、运行目录未创建。恢复 PATH 后使用完全相同的快照、目标库、路径和参数，恢复成功。
- 同一用例在数据库导入完成后注入了目录发布权限错误。CLI 通过 `database_restored: true` 确认导入结果。撤销注入的故障后，原命令使用绑定到原快照及目标的收据完成发布，省去重复导入。两次成功恢复都核对了 CAS，并通过真实本机 HTTP 验证原 Cookie 可读取会话 API。
- Ruff 和 mypy 通过。真实模型调用为 0。更新后的发行包为 `magr-harness-cefa386.tar.gz`，SHA-256 为 `a54dba1fbbb6fb5c6746256ad118c16f1413b316b379500683d9a5b0cd93e967`。

复测入口为 `make verify TASK=HV4-H18-RESTORE-RETRY`。[运维指南](H18_OPERATIONS.md)
