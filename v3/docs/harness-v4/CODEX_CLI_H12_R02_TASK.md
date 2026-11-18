# HV4-H12-R02：有界输出、Tool Call完整性与安全恢复

状态：DONE / ENGINEERING_PASS。用户2026-11-15授权“启动R02”。起点647435d，R00/R01本地工程
PASS；源码指纹e0f38c877dea8be31435de9866401d76621bc7403210f4c89d0fd6b112251729。
仅本卡工程实施/验收，不启动R03–R08、付费矩阵或H13，保留原H12/MVP FAIL。

## 原卡范围与必测

区分stop/tool_calls/length/error/取消/流中断/结果未知；上下文、思考、参数、正文预算
分别记录，按阶段选有界输出上限，不全局固定8192。参数完整组装并通过JSON/schema
校验才交Broker；不完整JSON、字段、工具名和参数永不执行。保存部分流、原finish、
provider request ID、usage、tool ID和not_executed状态。续写绑定原请求、CAS引用、
剩余额度、次数上限；仅已知用量且无未知副作用时允许新operation，不重发旧请求。
已有工具回执恢复不重复副作用；未知先核账。优先H05增量补丁，不要求整文件重写。
输出耗尽保留未完成Artifact，不能completed/PASS。

必测JSON中间截断、参数/补丁截断、usage缺失、流中断及副作用未知、续写前已有回执、
次数耗尽；增加原finish保真、工具schema拒绝、并行调用整批校验、分阶段配置、
CAS/PG/重建与epoch检查。新字段默认省略，旧profile及历史回执保持可读。

## 实施边界与验收

复用Engine持锁PG、现有Run事件、CAS、usage_calls、harness_operations、continuation
和原BudgetLedger。无新业务库/迁移，不改结算算法、权限、Engine/API/Settings接线，
不修改SA副本或协调的0053范围。DeepSeek仍thinking enabled、reasoning_effort max。
供应商没有独立思考token参数时只记录本地字节界限/供应商报告，缺计数不伪造为0。

注册非空 `make verify TASK=HV4-H12-R02`；make lint/typecheck；恢复检查用独立一次性
真实PG，所有模型流使用MockTransport/受控本地适配器。原账本只读核对，起点813次、
保守US$4.211490、unknown/reserved=0。失败尝试保留在新attempt目录。

来源：用户下载HV4_H12_R_Systematic_Repair_Taskbook.md的全局规则、R02、4.2、5、6；
SHA256 d7c4bc793b25b894fd98d1169e042a1bddedd3eff08e69a0cc52339ab7bc4d35。

## 最终本地验收

源码89639adff0a6756956bb4238844577b987f4f0dc；指纹
d8f5e637166eefd7bfba85eb5eea29ac4a208e3068a8e63c77cccdb7fdddf5bb。
注册347项（新增34项）全部通过，零失败/错误/跳过；lint/mypy通过。
原128份score和813次/US$4.211490账本不变，新增真实调用0；后续卡未启动。

[完整报告](H12_R02_OUTPUT_RECOVERY_20261115.md) ·
[协议](OUTPUT_RECOVERY.md) · [机器接收记录](accept/HV4-H12-R02/accept.json)。
完成后停在R02，原H12/MVP FAIL不变，不自动进入R03或真实付费评测。
