# HV4-H12-R05：独立全回归、最终树绑定与评分完整性

状态：PASS（本地工程，2026-11-16）。用户授权“继续R05”。以R04交付
4430f3495ab8a5c7ed402c293af43bfa4d0c4879为起点；源码指纹
0c729bdc2fb1b1a05b2471995e60eea5b3103b0d57662266b8c89b761be8e16a。
依赖R00/R01/R04本地工程PASS；原H12模型质量FAIL、MVP未放行。

## 范围及通过条件

独立Runner在原基线与最终封存树的只读副本执行全部冻结公开回归命令；与模型期
目标测试分开保存。标准回执包含phase、suite、tree、argv/cwd、环境、退出码、
测试清单/失败ID/跳过、原始日志CAS和内容绑定的receipt ID。

Scorer只从独立回执及原始日志重算。两阶段同范围完整执行、解析成功且可比较时
才允许new_regression_failures为整数；否则为null及NOT_RUN/BLOCKED/FAIL。
基线已有失败和新增失败分开记录；即使新增为0，最终现存失败也不能算PASS。
目标、完整公开回归、独立Judge、结构约束、执行完整性分别报告；不从缺失证据
猜测子结论。Judge/回归/结构必须指向同一最终树；旧树结果无效。

必测：目标绿但test_light_imports全回归失败；只执行子集；未运行；跳过；缺日志；
解析失败；改写回执/日志；环境或树变化；基线/最终测试身份变化；重复计分一致；
失败/超时后继续收集其余冻结回归；原PG/CAS和恢复/封存边界。后置审计只追加记录。

## 权威及交付

复用原Engine/PostgreSQL、CAS、预算、Broker、封存和final_gate，不改变共享
Engine/API/Settings/schema、授权/结算算法、路径/网络/凭据或SA接线。
只读工程核账；不运行付费模型、不改旧score、不读取隐藏Judge资产或注入答案。
R06的任务类型基线与Judge语义审查不在本卡实施，R06–R08/H13不启动。

注册真实make verify TASK=HV4-H12-R05并通过lint/typecheck；使用配置PG上的唯一一次性测试库、
实际归属容器与免费适配器，保留所有失败attempt及原始日志。源码冻结后完整重验，
输出协议/Schema、机器验收、逐例条件、报告、导航与独立本地提交；不合并、不推送。

任务书：HV4_H12_R_Systematic_Repair_Taskbook.md，SHA256
d7c4bc793b25b894fd98d1169e042a1bddedd3eff08e69a0cc52339ab7bc4d35。
共享H12起点账本813次、US$4.211490/US$12、unknown/reserved=0，只读核对。

## 已完成及停止点

最终源码08cb1e28fac1c5e2e27117f7de62fbd3c20d0ac2，指纹
8bee95a622e4fd6c504654e31dbce8ebf6880efec7277bd7ab87c629839cb56b。
注册667项（新增54项）零失败/错误/跳过；lint和mypy 273文件通过。
34组独立Runner/PG/审计观察、1017份CAS副本核验；原128份score、R00–R04验收、
共享账本均不变，真实新增调用0。全部15次尝试保留，原H12/MVP FAIL不变。
交付：[完整报告](H12_R05_INDEPENDENT_REGRESSION_20261116.md)、
[协议](H12_R05_REGRESSION_PROTOCOL.md)、[机器验收](accept/HV4-H12-R05/accept.json)。
本轮停在R05，R06–R08/H13未启动，未合并/推送/部署。
