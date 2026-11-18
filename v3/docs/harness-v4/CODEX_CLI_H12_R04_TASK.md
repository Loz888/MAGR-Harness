# HV4-H12-R04：入口、Readiness、服务生命周期与清理

状态：PASS（本地工程）。用户明确授权“启动R04”；本轮只实施R04，不启动R05–R08/H13
或新付费模型实验。R03交付44e966826ee62f146187b8dcb9ae4ad852125745为起点，源码
指纹0c592451a1e3a2f9491898412e063001bf1a006e04ddf2efc13bcd4085558b6f。
依赖R00/R01本地工程PASS，R02/R03原记录保留；原H12质量FAIL、MVP未放行。

## 范围

新增/修改入口的Python import及Node require/import探针；cwd/包路径/入口解析。
结构化Ready、HTTP观察、Process/Port回执，绑定Run/Command/epoch及最终树。
真实端口绑定与Ready日志端口、路由/状态/Content-Type/响应体必须匹配；动态端口
记录实际值。selfcheck需输出实际HTTP观察状态，不能只凭exit 0。
SIGTERM、截止时间、取消及失败均清理；独立观察进程、子进程/进程组、端口和日志。
无法核实或超时不能声称PASS；已知和未知回执仍经原PG/CAS/RecoveryIO恢复。

必测错误相对导入、缺模块、无Ready行、缺端口、路由错误、SIGTERM失效、孤儿进程、
端口占用和selfcheck无状态输出。原大套件通过不能替代入口探针。

## 权威与验收

继续复用原Engine/PostgreSQL、CAS、预算和隔离Broker；不改Engine/API/Settings、
SQL迁移、结算算法或SA共享接线。固定控制探针仅在原已授权、同任务容器内执行，
不向模型开放新命令、网络、凭据、宿主路径或任意挂载。旧冻结profile保持兼容，
新策略明确版本化；未知副作用先核对，不盲重放。

新增测试存在后注册真实make verify TASK=HV4-H12-R04；make lint/typecheck及真实
隔离容器/一次性PG集成必须有原始日志。失败保留独立attempt，不停共享旧容器。
共享H12账本仅只读核对：813次、US$4.211490/US$12、unknown/reserved=0。

来源为用户任务书R04、全局规则和统一证据契约：
HV4_H12_R_Systematic_Repair_Taskbook.md，SHA256
d7c4bc793b25b894fd98d1169e042a1bddedd3eff08e69a0cc52339ab7bc4d35。


## 最终验收（2026-11-16）

548项全部通过，0失败/错误/跳过；新增R04为53项。make lint、make typecheck通过，
源码指纹前后一致；最终源码`24cce0b3b2341b1c6172559398c019a07c74ed13`。PG/CAS/实际容器观察导出70组，
原失败尝试全部保留。原128份score及共享813次/US$4.211490账本不变，新增付费0。

[完整报告](H12_R04_LIFECYCLE_20261116.md) · [协议](H12_R04_LIFECYCLE_PROTOCOL.md) ·
[机器验收](accept/HV4-H12-R04/accept.json) · [逐例条件](accept/HV4-H12-R04/cases.json)。
本轮止于R04；R05–R08/H13未启动，原H12质量FAIL、MVP未放行。
