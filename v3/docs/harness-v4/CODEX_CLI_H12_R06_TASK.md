# HV4-H12-R06：Baseline、任务类型与Judge有效性审查

状态：**PASS（本地工程，原H12质量仍FAIL）**。用户授权“启动R06”（2026-11-16）。起点为R05交付
8d5052cfb0f043d1c0c4be965d6666367c2a8fbd，最终已验源码08cb1e2，指纹
8bee95a622e4fd6c504654e31dbce8ebf6880efec7277bd7ab87c629839cb56b。
原R00–R05本地工程PASS；原H12真实质量FAIL、MVP未放行。

## 本卡范围

显式冻结Task-Type Baseline Policy和两阶段公开检查：Bugfix原树目标红、最终绿；
Feature允许公开声明的新API/入口原树缺失，但依赖/语法/非目标错误仍阻断；
Refactor基线仅既有行为、最终验证已声明结构及行为等价；Run App基线不启动新服务，
最终沿用R04入口、Ready、HTTP与清理证明。

建立Judge Traceability Manifest，将每项断言绑定公开acceptance_requirement_id，
检查断言清单及源码hash、双向覆盖与未公开附加要求。具体名称只能来自公开要求；
“单一helper并复用”采用行为与调用/数据流检查，不强制未公开名称。
新增协议通过新profile/基准修订显式启用；旧Judge、旧实验配置和历史score保留。
R05完整回归与nullable计数不降级；预期编译红不能冒充已执行绿测试或新增失败0。

真实容器、原PG/CAS、原封存及Scorer闭环验证，免费受控模型；不新增付费调用。
覆盖合法替代helper命名、独立/无用helper、结构/行为不等价、未声明名字、未知断言、
Feature目标/非目标缺失、语法/依赖/混合错误、基线阶段误用最终检查、缺证据和恢复。

## 边界及产物

不改Engine/API/Settings/SQL/账本/授权算法或SA共享接线，不扩大路径、网络、凭据、
预算或epoch权限。私有Judge源码/断言不进入模型上下文；仅使用公开合成夹具及已见
旧问题的审查证据，不查看或创建新的独立持出答案。
保留原128份score和813次/US$4.211490账本，核账只读；保留所有失败attempt。

注册真实make verify TASK=HV4-H12-R06，冻结源码后完整验证，运行lint/typecheck。
交付Baseline Policy/Traceability Schema、两阶段协议、Benchmark Review、逐例结果、
机器验收与本地提交。本轮完成后停在R06；R07/R08/H13不启动，不合并/推送/部署。

依据：下载任务书HV4_H12_R_Systematic_Repair_Taskbook.md，SHA256
d7c4bc793b25b894fd98d1169e042a1bddedd3eff08e69a0cc52339ab7bc4d35，R06原文。

## 验收结果（2026-11-16）

最终源码88be4949c712e0e1c5fc8256a08325adf0339cc6，指纹
514e99fea7f55287bb857a240e75b60ea8ab00a07c4b985021c728f27f6cb21d。
注册729项全部通过，新增62项；lint、mypy278通过，零失败/错误/跳过。
81组真实观察、1104份CAS副本和三种补丁的只读导出重算通过；全部10次尝试保留。
128份历史score、R00–R05验收及813次/US$4.211490共享账本不变；新增付费调用0。

[完整报告](H12_R06_BASELINE_JUDGE_REVIEW_20261116.md) ·
[两阶段协议](H12_R06_BASELINE_JUDGE_PROTOCOL.md) ·
[Benchmark Review](H12_R06_BENCHMARK_REVIEW_20261116.md) ·
[实际Schema](protocol/r06/README.md) · [逐例结果/条件](accept/HV4-H12-R06/cases.json) ·
[机器验收](accept/HV4-H12-R06/accept.json)。

旧24题的私有Judge未被追认证为合格，实际新版本仍须公开准备、逐断言语义审查及
重新冻结，旧持出仅诊断。当前Python/Go结构与固定Python私有清单适配器有明确边界，
不支持的情形保持受阻。本轮停在R06，R07/R08/H13未启动，H12/MVP仍未放行。

用户中途追加SA22协调已在主副本独立完成：HANDOFF第47节a7dbfd4（依赖查询），
第48节efb09f4（0054编号/所有权），不导入本卡、不应用SA SQL、不改变质量门。
