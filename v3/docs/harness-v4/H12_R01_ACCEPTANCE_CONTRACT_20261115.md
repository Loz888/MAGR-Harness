# H12-R01 本地工程验收记录（2026-11-15）

结论：R01 工程 PASS；原 H12 质量 FAIL、MVP 未放行不变。新增真实模型调用和费用均为0。
本轮只执行用户授权的 R01，完成后停止，R02–R08 和 H13 未启动。

工作分支 `hv4-h12-r01-20261115`，源码提交
`cc626d81ba3ef07061b5e720cc2f6c273cdab3de`；注册验收前后标准源码指纹均为
`e0f38c877dea8be31435de9866401d76621bc7403210f4c89d0fd6b112251729`。
起点为 R00 的 `7789be31d55951965b88d0f99089d667d3b4573d`，起点指纹
`6bff0896ec4c15e8c3322d7c8688afd88b8a6676d8fb8fe09f227600abaa4636`。
本分支仅本地提交，未合并、推送或部署。相邻 Harness main 和 SA 副本未修改。

## 修改与依据

实现[验收契约协议](ACCEPTANCE_CONTRACT.md)：完整公开目标、稳定 required IDs、必需
产物、范围、基线、类型和版本进入同一不可静默削弱的契约。初版与修订完整记录在既有
Run 事件；重新计算哈希不能制造未批准的修订。模型建议保留原有加法规则，不可替换 required。

Planner、Worker、Context、Verifier 与 Scorer 记录同一 revision/hash。上下文有两层
完整性检查：Compactor 对受保护状态精确核对；派发前检查实际发给模型的消息。
新 H12 profile 使用 contract-v1，评分缺契约或缺当前验证/产物时不能 PASS。
旧 profile 的默认新增字段不序列化，历史核账按归档协议读取；不改旧 score。

用户追加指令后创建新修订，保留前版、修订原因和受影响 ID，旧回复整批丢弃。
恢复检查既有 checkpoint 目标与 PG inbox 一致；新目标下已结算回复可复用，不重发。
若恢复快照与已应用的 PG 指令不同，会在新调用前拒绝并要求核对，不能覆盖新目标。

新增契约使一个原8192 Token场景无法同时容纳大页。分页按现有输入窗口收紧至最多
四分之一，保留完整页字节和原哈希续页；原文仍能完整恢复，没有提高预算或删验收条款。
多次压缩测试还暴露权限声明扫描对长JSON行反复回溯，将同一识别规则固定在行首后消除
重复扫描。实际路径、网络、费用、epoch和Broker权限守卫保持原接口。

未改 Engine/API/SQL 迁移、预算/结算算法或 SA 接线；没有增加第二套业务状态。
公开自然语言 required 的稳定 ID 链接现有机械检查及其回执。此关联只证明完整传递，
不能当作每条自然语言要求均有独立行为覆盖。入口、Ready、完整回归和 Judge 有效性
继续由后续任务卡处理，未注入私有断言或答案，未重新进行 H12 真实质量评分。

## 注册验收

`make verify TASK=HV4-H12-R01` 最终 **206 PASS / 0 FAIL / 0 ERROR / 0 SKIP**，
48.082秒。`make lint` 通过；`make typecheck` 的257个源码文件通过。
测试使用受控免费模型、一次性真实 PostgreSQL 和原隔离命令；供应商真实传输被测试守卫阻止。

| R01 条件 | 已执行检查及结论 |
| --- | --- |
| 八个必需字段不能缺省通过 | 逐字段移除触发 schema 拒绝，PASS |
| Planner/Worker/Context/Verifier/Scorer 一致 | PG完整闭环和评分绑定相同hash，PASS |
| required/版本/哈希/产物不可削弱 | 删除、伪造新hash/revision、重写Planner绑定均在调用前拒绝，PASS |
| Prompt Projection丢字段 | 实际消息投影故障注入，0次模型派发，PASS |
| Compaction丢字段 | Builder输出故障注入，0次模型派发，PASS |
| 多次压缩 | 四轮历史压缩保留所有required ID/hash/公开条款，PASS |
| 模型不能替换required | 原项替换拒绝；合法附加项保留前版与required，PASS |
| 用户指令更新 | 旧工具回复不执行，新修订保留旧ID/previous_hash/原因，PASS |
| 新目标恢复 | PG重建/新epoch复用已结算回复，原operation仅发送一次，PASS |
| 公开自检要求完整进入Worker | dev-11对应公开任务3条required完整投影，包含实际HTTP状态要求，PASS |
| Scorer不能默认为PASS | 缺契约、错版本/哈希、缺验证或必需产物均拒绝，PASS |
| 历史兼容与预算 | 原128份score逐份哈希不变，原共享账本只读核对不变，PASS |

最后两类 PG 样例保留五阶段绑定及完整 revision1→2 链；自检样例仅验证公开投影，
没有将合成 inspect 成功记作原 run_app 业务成功。完整原始日志、JUnit、Schema和回执
索引见机器接收记录。

## 失败尝试与修复证据

证据根 `evidence/harness-v4/HV4-H12-R01/`，目录名是尝试标签；准确执行时间以注册回执为准。

| 尝试 | 结果与处置 |
| --- | --- |
| 001 | 17通过/3失败，测试和校验器取错系统说明消息、Builder故障注入目标错误；初版lint/type错误保留 |
| 002 | 17通过后，长单行扫描耗时；中断本轮自身测试，BLOCKED，未结束的测试不计PASS |
| 003 | 17通过后，JSON转义仍构成长行；中断自身测试并修正扫描规则，BLOCKED |
| 004 | 54通过/2失败，修正公开任务ID，并诊断小窗口完整契约+大页溢出 |
| 005 | 136项测试通过；LocalVerifier payload类型错误使该尝试整体FAIL，修正后再验 |
| 006 | 注册205项与静态检查通过；复核后补充伪造修订链检查，不作为最终候选 |
| 007 | 源码cc626d8，最终注册206项、lint/mypy、历史/账本核对通过 |

007中的一次离线证据导出起初误将本地result投影当成带run_id的API返回值，缺字段时
Scorer正确拒绝；导出失败记录保留。随后显式注明从原accepted task快照取run_id，
仅用于合成回执归档；没有修改原结果、源码或评分。

## 门禁与交付

共享账本仍为 **813次、保守US$4.211490/US$12、unknown=0、reserved=0**。
历史题目、阈值、FAIL和NOT_RUN全部保留。R00及原128份计分哈希保留核对证据。

本地工程通过不替代真实质量验收。原H12/MVP状态保持FAIL/NOT_RELEASED，
新付费矩阵、独立新持出、R02–R08/H13、推送/发布均NOT_RUN。
交付包括[任务卡](CODEX_CLI_H12_R01_TASK.md)、[协议](ACCEPTANCE_CONTRACT.md)和
[机器接收记录](accept/HV4-H12-R01/accept.json)。
