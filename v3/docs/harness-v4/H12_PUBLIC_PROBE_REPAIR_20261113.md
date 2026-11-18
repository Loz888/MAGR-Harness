# H12 公开复现与行为核验修复（2026-11-13）

状态：已实现，分阶段定向工程验证完成，待独立复验；原H12质量FAIL、编码MVP未放行。修复仅在隔离分支
`h12-runtime-repair-20261113`，不改在线主树，不启动H13，不推送。

## 解决的问题

原引号内分号题的原suite与修复suite都绿，但Verifier要求原树失败；冻结命令和
写范围没有合法补充复现的入口。URL新函数也只跑旧suite，缺少新需求的公开行为
覆盖。本轮提供通用的显式 `public-probe-v1` 协议，不嵌入任何题目答案。

- 原公开suite负责回归；固定的公开probe负责目标行为。bugfix同一probe必须在
  原树留下真实断言失败、在最终树通过，原suite无需故意变红。
- 可信controller冻结脚本SHA256、plan ID、case ID及固定argv。只挂载一个只读
  文件，路径在工作区外；模型仅能 `read_public_probe` 或调用固定
  `command_id=public_probe`，不能提供宿主路径或任意命令。
- 执行沿用原Broker、CommandSession、租约、资源与PG预算。帧/退出码/完整日志/
  tree/plan/原epoch必须一致；import error、skip、xfail、截断和假红均不成立。
  Verifier重新核对完整plan与case集合，不能凭一个red/green标签完成。
- 自动记录两份原树基线，并把真实状态和回执索引提供给模型；最终树分别核验
  两个要求。refactor签名、run_app就绪与清理门继续保留。
- 恢复复用已知回执，未知操作保持阻塞；原始回执与规范化事实分别保留，不把
  原epoch重标为新执行。两个解释器程序只能来自冻结回归命令及probe命令。
- 公开probe的前端测试摘要使用真实case数与状态，不再误交给pytest解析器。
  原始回执元数据引用既有归档日志，工具CAS不再重复内嵌两份stdout；百万ASCII
  日志的对象大小定向验证单列，不提高2MB CAS上限。
- 默认None省略新字段，保留旧合同/profile/receipt/checkpoint的表示和哈希。

## 准备路径及边界

新增dev-only公开计划加载、冻结及零模型原树准备入口。显式计划可只覆盖部分
Python开发题，未列题明确沿用旧协议；未知task ID或引用缺失一律拒绝。
`prepare_public_readiness(evidence)`保存原probe、原回归、完整日志、镜像、tree及
权限来源；`validate_public_readiness()`按显式哈希重新核对。

`PUBLIC_SUBSET_READY`只表示这部分公开工程准备完成。完整12题的新候选准备与
真实开发评分尚未执行；付费入口仍返回 `H12_FULL_DEV_READINESS_REQUIRED`。
新协议初版仅Python，不宣称Node/Go已有对应公开probe执行协议。正式公开脚本
必须将import/collection异常与目标断言分开处理；严格解析器不能代替脚本审阅或
独立私有Judge。模型新增检查也不能代替独立评分。

## 验证与证据

使用合成玩具任务、受控模型、真实一次性PG及owned Docker对象；没有新增付费
模型调用，没有读取私有Judge或持出实现，没有修改主库或停止其他任务的容器。
首次失败保留，修复后仅复验失败/新增节点，旧777项、H04/H06和质量套件未重跑。
本轮窄注册入口为 `make verify TASK=HV4-H12-PUBLIC-PROBE`；本次逐批验证的结果
按源码哈希分别记录，不能等同于在最终源码上重跑整个注册套件。

分阶段共128个不同工程节点有通过证据：资产/执行38、Verifier/兼容39、公开计划20、
零模型就绪14、PG/Docker服务12、摘要/CAS5。最后源码仅做128项收集检查，未重新
执行全部128项；分阶段源码指纹、首次失败与定向复验均保留。全局lint与mypy243
源码文件通过。alias仅证明PG准备和精确双program绑定，固定镜像无python别名，
不把该case写成命令执行通过。崩溃恢复为受控SystemExit与真实PG/Docker，不宣称
进行了OS SIGKILL实验。

最终分项、首次失败、定向复验和静态检查见
[机器记录](accept/H12-PUBLIC-PROBE-20261113/accept.json)；本地原始日志在
`evidence/harness-v4/H12-PUBLIC-PROBE-20261113/`。

## 质量门与后续

原v2开发0PASS/3FAIL/9NOT_RUN、独立持出模型12NOT_RUN仍保留；开发10/12、持出9/12
门槛不改。共享账本仍为305次调用、保守费用US$1.316981/US$12，本轮新增0。
本修复不表示三题已通过，也不把补丁/工程验证写成真实模型成绩。

后续需要从公开需求编写并审阅正式公开资产，冻结新候选及相应任务协议，完成
完整开发集准备，再对原三题和达到10/12所需的未测题做有账本的真实模型评分。
只有达到开发门槛后，才进入独立持出真实评分。原失败与每次新尝试分别保留。
