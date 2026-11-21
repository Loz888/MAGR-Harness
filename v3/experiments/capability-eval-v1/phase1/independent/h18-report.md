# H18-R1 独立复验

**候选 ff1562c6e98bc6788b11141d15c1b215b79701bf 通过 8/8 项，基线 fcabf97653a77db0032af5f80463d1ad72447181 通过 6/8 项。** 两次评测各使用全新 PostgreSQL 16.12、隔离 Unix socket 和临时文件系统。两次均未调用模型。基线的导入中断重试失败和前向迁移入口缺失保留在原始记录中。

| 项目 | 基线 | 候选 | 候选直接观测 |
|---|---|---|---|
| G01 缺少 pg_restore | 通过 | 通过 | PATH 无客户端时 CLI 明确报错。PG 目标空、最终路径缺失；还原 PATH 后原命令成功。 |
| G02 导入中断 | 失败 | 通过 | 私有 PG 确认真实 pg_restore 已在事务内 COPY；此时中断客户端，首次返回 SNAPSHOT_TOOL_INTERRUPTED，库回滚且路径缺失。基线用原命令重试得到 SNAPSHOT_RESTORE_OUTCOME_UNKNOWN；候选原命令成功。 |
| G03 前向迁移失败 | 未通过 | 通过 | 基线没有 --upgrade 入口。候选从 0062 快照开始，在目标 PG 执行 0063 时触发一次真实 SQL 错误。CLI 返回 SNAPSHOT_MIGRATION_FAILED、database_restored=true、retry_same_command=true；PG 保持 40 项已应用迁移，运行目录未发布。撤故障、从 PATH 移除 pg_restore 后，原 --upgrade 命令成功，41 项迁移的名称与内容哈希全部匹配发行版。 |
| G04 CAS 哈希不符 | 通过 | 通过 | 对私有快照的一个 CAS 对象做等长单字节损坏。CLI 报 SNAPSHOT_CHECKSUM_MISMATCH，目标库空且未发布；恢复原字节后原命令成功。 |
| G05 原子发布权限失败 | 通过 | 通过 | 在原子重命名前撤去实际父目录写权限，内核拒绝发布。CLI 明确报告 PG 已恢复，可执行原命令。恢复权限并移除 pg_restore 后原路径发布成功；PG、清单与文件校验相符。 |
| G06 发布后 API 启动失败 | 通过 | 通过 | 在成功发布后暂时撤去私有 API 套接字父目录写权限。服务退出码 1，运行目录和 PG 数据保留。还原权限后在原 socket 用相同服务命令重启，原 Cookie 读取会话和受保护接口均返回 HTTP 200。 |
| G07 重复恢复 | 通过 | 通过 | 目标包含用户标记文件时，CLI 拒绝恢复，标记 inode、内容及空库保持原状。对已成功恢复的同目标重复执行返回 SNAPSHOT_DESTINATION_EXISTS，恢复目录 inode、数据库观测和文件哈希不变。 |
| G08 身份、凭据与配置 | 通过 | 通过 | 原 Cookie 对应相同账户 UUID，真实 HTTP 仍有原管理员身份。恢复密钥能解密原合成凭据，配置、版本和授权完全相同。PG 中 CAS 哈希与字节数和实际内容匹配，数据库 dump 及文件与清单一致。 |

H18-R1 使用同一份冻结脚本，所有恢复均从隔离检出的真实 CLI 入口执行，前向迁移显式传入 --upgrade。G06 的发布已成功，需要重试的操作是原服务启动命令。G07 按预注册约定接受对已发布目标的明确拒绝。未单独枚举未知提交结果、宿主机断电等其他中断窗口。G05 权限故障期间会有暂存目录清理告警，最终目标受到保护；重新获得写权限可在原目标恢复。实验结束时专属容器及整棵临时文件系统已清理。

## 核验材料

| 文件 | SHA-256 |
|---|---|
| REGISTRATION.txt | e8e78b719e7f149227643689785268c4bbba99dc650e8142753b61518e8d39c0 |
| run_private.py | 6a8ecfd8d57c0fdf51fa225a7044d6bd0868159a6067e62913934ec3072751f2 |
| serve_local.py | 8b3a143a8489d41238ca8ee77b8738c86164c5031ad4e2fe0a47ee6ea03aa1e0 |
| baseline-v2/summary.json | ba010b3cbc7b27e66f25d4b3039bb570b41c5d35c087bb5d8ac0b04c7cc97b9e |
| baseline-v2/observations.jsonl | f239cb92f578c593769e374a23a415805b0fc8035fb4cf95ace4436b4a2c196d |
| candidate-ff1562c/summary.json | 4a4f83ca6679eb1e14970bd7fac5f2744147d77ff5b82fad38326c6fec624aa9 |
| candidate-ff1562c/observations.jsonl | 068cce029228a0b3f1f7bbc4c88a32707b06028cdbc326e5733478a8cd3495d1 |

首次试运行在环境准备时发现宿主 PG 缺少 pgvector，原日志保留。正式两次运行均使用仓库锁定、本地已有的镜像 33198da2828a14c30348d2ccb4750833d5ed9a44c88d840a0e523d7417120337 与主机 PG16 客户端。G03 另有一次不计分的夹具验证，确认数据库端错误、事务回滚和解除故障后的前向迁移均实际发生。
