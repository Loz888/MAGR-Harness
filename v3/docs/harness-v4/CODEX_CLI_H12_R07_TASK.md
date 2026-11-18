# HV4-H12-R07：公开测试反馈与开发集边界覆盖

状态：**PASS（本地工程，原H12真实质量仍FAIL）**。用户授权“启动R07”（2026-11-16）。基线交付22153a1，
已验源码88be494，指纹514e99fea7f55287bb857a240e75b60ea8ab00a07c4b985021c728f27f6cb21d。
依赖R00–R06本地工程PASS，原H12真实质量FAIL、MVP未放行。

## 范围与通过条件

显式Public Test Feedback Contract绑定公开命令ID、退出码、失败ID、断言摘要、原始
日志CAS、实际tree、Run/epoch及公开要求版本。新补丁后自动执行配置的目标、静态
检查和完整公开回归；同一模型批次多次修改分别检查，旧树结果不得替代新树。
命令仍经原Broker/CommandSession/RecoveryIO、租约、原Run预算和PG记账。

稳定失败签名剔除耗时/地址等无意义差异；逐工具批次判断新树、新证据和重复失败，
提醒后有界停止。阈值和计划冻结并在恢复后保留；新读取/新失败仍能推进，原Token、
调用、工具、费用、时间和权限上限不重置。公开规格模糊时沿用ask_user澄清，文字
回答不授权新能力。独立私有Judge仍只在最终验收，不向模型提供代码或隐藏答案。

新增跨项目/语言公开边界矩阵：未知参数剩余argv、quoted delimiter、模块入口导入、
readiness、清理、轻量导入回归。真实执行正反例，分开记录公开行为失败与协议/
环境未核实；免费受控模型闭环不宣称真实模型能力通过。

## 边界与交付

新策略仅新profile/新冻结显式启用，旧Judge/实验/源码证据不回写。保留128份历史
score、R00–R06验收与813次/US$4.211490共享账本，前后只读核账。
不改Engine/API/Settings/SQL/预算及底层授权算法，不扩大命令执行器、文件、网络或
凭据权限，不改SA共享接线。不新增付费调用，不启动R08/H13，不合并/推送/部署。

冻结最终源码后运行本卡注册make verify和lint/typecheck；保留所有失败attempt、
原PG/CAS/容器回执、恢复与信息流隔离证据。交付反馈协议/Schema、进展检测规则、
开发公开边界矩阵、逐例结果、机器验收和本地源码/文档提交。完成后停在R07。

任务书：下载HV4_H12_R_Systematic_Repair_Taskbook.md，SHA256
d7c4bc793b25b894fd98d1169e042a1bddedd3eff08e69a0cc52339ab7bc4d35，R07原文。

## 验收结果（2026-11-16）

最终已验源码7f19289dcf92618ac5298e0a9b2c4bd119dd2921，指纹
73d871033444376e23283f00ede50abefbc393dedf1cbb46cdbcd18c1fc23346。
注册785项全部通过，新增56项；lint、mypy279通过，零失败/错误/跳过。
全部13次尝试、原始PG/CAS/日志、来源epoch修复及原R03异步测试竞态均已保留。
128份历史score、R00–R06验收及813次/US$4.211490共享账本不变；新增真实调用0。

[完整报告](H12_R07_PUBLIC_FEEDBACK_20261116.md) ·
[反馈协议](H12_R07_PUBLIC_FEEDBACK_PROTOCOL.md) · [公开边界矩阵](H12_R07_PUBLIC_BOUNDARY_MATRIX.md) ·
[实际Schema](protocol/r07/README.md) · [56项逐例结果/条件](accept/HV4-H12-R07/cases.json) ·
[机器验收](accept/HV4-H12-R07/accept.json)。

边界资产是跨5个合成项目/3种语言的公开工程补充集，不表示实际H12新题集已冻结或
模型已经通过；R08仍须准备实际公开计划、独立新持出及真实评分。本轮停在R07，
R08/H13未启动，H12质量FAIL、MVP未放行保留。
