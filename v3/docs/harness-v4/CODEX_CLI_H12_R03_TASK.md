# HV4-H12-R03：可恢复工具拒绝与安全参数纠错

状态：PASS（仅本地工程）。用户先提R04后明确纠正为“继续推进R03”；R04仅只读检查，未实施。
本轮只做R03，不启动R04–R08、付费模型矩阵或H13。起点d148cc1，指纹
d8f5e637166eefd7bfba85eb5eea29ac4a208e3068a8e63c77cccdb7fdddf5bb。
依赖R00/R01均本地工程PASS；R02本地工程347项PASS保留，原H12/MVP FAIL不变。

## 原任务卡范围

错误类别PATH_OUT_OF_SCOPE、SYMLINK_ESCAPE、INVALID_ARGUMENT、MISSING_DEPENDENCY、
CAPABILITY_NOT_GRANTED、RESOURCE_LIMIT、EPOCH_STALE、NON_RECOVERABLE_POLICY_DENIAL。
回执含工具、分类、允许的workspace-relative根、参数约束、可恢复性、剩余纠正次数。
不泄露无关宿主绝对路径、密钥或环境变量，不建议外挂载或宿主路径绕过。已知可纠正
参数允许有限次数新operation，仍经原Broker/当前权限；需要新能力进入既有Ask流程，
批准前保持阻塞，普通文字回复不变成授权。不可恢复违规立即拒绝。

每次拒绝及后续纠正独立operation ID和审计回执。原预算/期限/epoch/CAS绑定继续约束；
未知用量、未知工具/进程/写入先核对，不盲重发。成功纠正不代表拒绝从历史中消失。

## 验收与边界

必测宿主路径拒绝后安全纠正、次数耗尽、不能扩路径/网络/凭据/命令、symlink、..、
绝对路径、编码变体、竞态替换；补充旧epoch、撤权、真实PG恢复/已知拒绝回执复用、
模型缺参数与R02边界、权限Ask文字不授予能力、压缩与无进展门不会吞掉有界纠错。

复用原Engine/PostgreSQL、Run事件、CAS、usage_calls、harness_operations及RecoveryIO，
不加新业务真相/迁移，不改Engine/API/Settings、结算或ToolAuthority判定算法。
无SA副本/0053接线修改。仅Harness私有错误策略、循环、回执及现有服务组合。

新测试存在后注册真实make verify TASK=HV4-H12-R03，运行make lint/typecheck。
恢复与并发用独立一次性PG；模型仅受控免费夹具。原共享账本只读核对：813次、
保守US$4.211490，unknown/reserved=0。失败尝试写新attempt并保留。

来源为用户HV4_H12_R_Systematic_Repair_Taskbook.md的全局规则、R03、统一契约/门禁/
证据规范；SHA256 d7c4bc793b25b894fd98d1169e042a1bddedd3eff08e69a0cc52339ab7bc4d35。

## 本地交付

源码eb80eeebcde31537e4945720c765043ed50ecffd，注册464项全通过，无失败/错误/跳过；
新增R03测试44项，另四项免费PG取证通过。make lint和make typecheck（259文件）通过。
原128份score及原共享账本均未变：813次、US$4.211490、unknown/reserved=0。
原H12真实质量FAIL、MVP NOT_RELEASED；不启动R04–R08/H13或新付费模型矩阵。

[完整报告](H12_R03_TOOL_RECOVERY_20261115.md) ·
[错误协议](TOOL_ERROR_TAXONOMY.md) · [机器记录](accept/HV4-H12-R03/accept.json)。
