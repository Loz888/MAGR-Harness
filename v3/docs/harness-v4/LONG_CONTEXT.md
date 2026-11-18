# HV4-H10：长任务上下文与原文工具

当前：原 `5707785` 独立复验 FAIL 保留；`c140dad` 的 F01/F02 修复已有范围限定的
独立复验 PASS，报告由 `9b52190` 提交。注册68项、聚合281项、旧上下文15项、静态检查、
原始复现及12项独立边界/变异检查均通过，标准源码指纹为
`38a02725fe2a5b5967e2604951a0749d69aeea1c5b1640e6dc65901d10d10de8`。
见 [独立报告](accept/HV4-H10/repair-c140dad-independent-recheck-20261111T120756Z.json) 和
[P1修复说明](H10_P1_REPAIR.md)。其他报告中的 UTF-8/search/SSE/收件箱/私有字段发现未因此关闭。

用户确认后已启动 H11，已实现并通过工程门，当前为 PENDING_INDEPENDENT_RECHECK，恢复接线见 [恢复协议](RECOVERY.md)。
下文保留 H10 首轮设计、计数与当时停止点；其中零 Run UUID、内存账本及“不启动H11”
描述的是 H10 历史实现边界，不能当作 H11 当前状态，也不构成 H11 工程或独立验收结论。

起点 `537e0658eed90dad31b30e7badb1372d4e75c296` 仅比交接基线增加任务卡文档，
标准源码指纹为 `915092c7504d95c31b88ea1e8fdb10722506d96cf31c841647944a7da9786331`。
规则及 H10 必读材料已核对；H00 冻结任务、隐藏判定器和阈值不变。

## 接线与不变量

`CodingLoop` 在每次模型调用前的安全点，从当前 TaskBinding、TaskPlan、工作树、
执行回执、工具归属索引及预算账本投影生成 `WorkingState`，交给
`LongContextManager.prepare`。API 使用同一个管理器和 `CodingToolRegistry`。
旧 `ContextManager.build` 端口及 `SimpleContextManager` 调用方继续兼容。
TaskPlan 新增带默认值的 `harness-task-plan-v2` 标识；没有 Engine/schema 迁移。

工作状态保留目标、任务类型、完整清单和未完成项、决策、失败签名、补丁和产物摘要、
当前树、待收回命令、待答问题、工具集合以及当前授权与账本引用。目标变更使用 inbox
指令版本；验收清单使用 checklist revision；安全点 sequence 和状态内容摘要共同形成
context revision。旧版本不能覆盖新投影；H09 的旧模型结果整体丢弃继续适用，调用期间
树变化也丢弃整份响应，重建上下文后再调用。

完整产物归属和 ACL 元数据保留在持久 WorkingState 与 Registry 索引；模型投影只重复
必要的路径、hash、树、任务归属、来源修订和完整性信息。投影按完整状态的 revision
绑定并精确核对；分页工具始终读取完整 Registry 归属和当前许可，不从投影恢复授权。

管理器复用 `ContextBuilder/ContextRequest/ContextBundle` 和
`compact_observations/ContextInvariants/validate_compaction`。压缩前后校验保护字段，
只在完整工具调用及全部结果的边界移动窗口；最终消息整体再次计量，固定信息超预算
时失败关闭。最新完整工具批次先预留空间；长工具正文可改为原文引用和预览，分页 JSON
逐字保留。预算包含工具声明等请求开销，并对序列化转义后的实际消息至多作八轮本地
确定性预算调整。压缩不增加模型请求。这里的 token 是现有三字节估算，
不是供应商 tokenizer 的精确计费值。历史原始消息另存 CAS 供审计，不能凭其 hash
通过原文工具取得内容；供模型重读的源码和日志必须经过 Registry 单独登记。

## 模型工具

- `artifact_page(path, artifact_hash, cursor?)`：读取固定字节上限的 UTF-8 页面；沿
  continuation 取完原文。`artifact_reread(path, artifact_hash)` 从第一页重新读取，
  仍然有界。两者返回 source tree/current tree，旧文本不能伪装为新树内容。
- `read_file` 仍返回原正文及 H08 文件/树摘要，追加原文 CAS 引用；旧模型协议兼容。
- `run_tests` 通过既有 H06 CommandSession 执行；保留的日志逐页收集到 CAS，明确
  complete、total/retained bytes，已有租约输出限制导致的丢弃不会标成完整原文。
  基线 Broker 回执也登记保留日志；旧回执缺少截断标志，因此其 complete 保守记为 false，
  不把已保留的字节冒称为完整进程日志。
- `search_files` 按 task/workspace/tree/query/path/filter/当前 grant 与 lease 缓存；
  每次先核权，树变化清缓存，缓存命中不会新增循环进展证据。

CAS 索引绑定任务、工作区、来源路径、原树和租约。每一页都经当前 Grant 与 Broker
授权复核；受保护路径、跨任务/工作区、撤权、旧 epoch、猜中的 hash 均不能授予访问。
日志还绑定生成租约，替换租约后不能复用旧日志范围。cursor 只是内容位置，没有权限。

## 持久化与边界

每次安全点保存版本化状态、ContextBundle、上下文消息、原文引用、输入字节/token、
累计压缩次数及损失检查结果到 CAS；原子更新任务 `context.json` 指针。终态保存最终
清单、树与引用，关联前一上下文，结果和未完成 checkpoint 暴露这些字段。
读取必须由调用方提供重新核对过的当前 WorkingState，严格校验版本、内容摘要、归属及
原文引用；损坏或不匹配失败关闭，不能自动重签旧树或旧许可。

摘要和指针只供检查，不恢复授权、预算、进程或未知调用，不写业务账本。本卡未新建
长期记忆体系；失败表示沿用现有 compactor 约定；原检索和 memory 协议不变。
H11 的重启一致性、零 Thread/Run UUID、固定初始 epoch、内存预算账本及既有 H07
启发式限制仍适用。真实模型、真实 Agent、真实发布三门均为 `NOT_RUN`。

## 验证入口

`tests/test_harness_v4_h10.py` 为核心接线、压缩与持久化验收，工具负例另见
`tests/test_harness_v4_h10_tools.py`。注册门还纳入 `test_harness_v4_h10_finalization.py` 和
`test_harness_v4_h10_lease_provenance.py`；`make verify TASK=HV4-H10` 当前执行四份文件。
证据和逐项映射见 `accept/HV4-H10/accept.json`；独立复验入口为
`CODEX_CLI_H10_REVIEW_TASK.md`。完成后停止，不启动 H11/H12，不推送、不发起付费调用。

## 本轮基础设施失败的核实

沙箱内首次旧上下文回归为 10 passed / 5 errors，错误发生于 PostgreSQL fixture 建连，
不是工程 PASS。无凭据分阶段探针证实：创建 `AF_INET/SOCK_STREAM` socket 时就返回
`PermissionError: EPERM (errno=1)`，尚未执行 TCP connect 或 PostgreSQL 认证。
该现象归于本次 Codex 执行沙箱的系统调用限制，不能据此判定数据库宕机或密码错误。
经过执行权限批准，在沙箱外使用原锁定环境、原配置及隔离真实 PostgreSQL 重跑，
同两组回归为 15 passed / 0 failures / 0 errors / 0 skipped。

原始失败保存在 `evidence/harness-v4/HV4-H10/20261111T095858Z-context-regression-0504b008f5/`；
分阶段根因探针为 `20261111T100707Z-sandbox-socket-root-cause/probe.json`，
对照成功为 `20261111T100103Z-context-regression-retry-86dda80161/`（均相对同一 H10 证据根）。
pytest 初始长回溯自动展开了连接参数：原始日志与 JUnit 仅本地受限保存，另有脱敏副本，
不随 Git 提交；后续使用短回溯，用户报告不输出凭据。

最终工程检查：H10注册42项、H00–H10聚合255项、旧上下文15项均通过，无失败/错误/跳过；
lint/typecheck通过。原失败与重跑逐次保留。两项长任务分别21次和15次真实压缩；
纯读原文28640字节、修复任务原源码17063字节与失败日志6499字节逐字节恢复。
标准源码指纹及命令/逐项映射见 [H10验收记录](accept/HV4-H10/accept.json)。
