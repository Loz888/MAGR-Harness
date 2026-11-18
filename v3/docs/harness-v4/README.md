# MAGR Harness 升级任务书与规划

2026 年 11 月 18 日，H12 R08 的开发集、独立持出集均通过 11/12，合计 22/24。独立复核确认正式质量门 PASS。产品发布状态为 `NOT_RELEASED`。[正式成绩](H12_R08_FORMAL_REAL_MODEL_20261118.md) · [独立验收报告](H12_R08_INDEPENDENT_MATRIX_REVIEW_20261118.md) · [匿名复核回执](accept/HV4-H12-R08-FORMAL-20261118/independent-review.json)

[H12 R08 正式实验开发集成绩](H12_R08_DEVELOPMENT_20261118.md)记录 2026 年 11 月 18 日冻结候选的 12 道开发首样本。[独立耗时复核](H12_R08_TIMING_INDEPENDENT_20261118.md)记录了持出准入结论。

2026-11-18，R08 上轮剩余三题均已取得真实模型端到端 PASS。03、08 通过于 `e86f024`，05 首次复测仍失败，修复读取参数反馈后通过于 `33e39a4`。本次共 23 次模型调用。完整同版本开发门和独立持出仍待验收。[定向报告](H12_R08_TARGETED_REPAIRS_20261118.md) · [机器记录](accept/HV4-H12-R08-READ-SIZE/accept.json)

2026-11-17 的 [R08 入口观察器修复](H12_R08_ENTRY_OBSERVER_REPAIR_20261117.md)记录了 9 项真实隔离进程检查。[精确测试标识](accept/HV4-H12-R08-ENTRY-REPAIR/cases-exact-nodeids.json)

2026-11-17：[R08失败题真实定向复测](H12_R08_FAILED_ONLY_20261117.md)完成：新源码ab449b0下
2/5 PASS、3/5 FAIL，代码Judge 5/5只作代码检查。原01/11通过，03/05/08仍未满足整题协议；
同源码最多9/12，独立持出12题未准入。新增72次，累计1002次/保守US$6.109758，unknown/reserved=0。
入口竞态另分支修复9项工程PASS、零新增模型，未代入冻结评分。H12/MVP仍未放行。
[任务卡](CODEX_CLI_H12_R08_TARGETED_V2_TASK.md) · [机器记录](accept/HV4-H12-R08-TARGETED-V2/accept.json)。
以下保留原首轮和早期任务记录。

2026-11-17：[H12-R08逐题真实报告](H12_R08_REAL_QUALITY_20261117.md)：开发12题均已
首次调用真实模型，核账视图7 PASS/5 FAIL，低于10/12；独立新持出12题未获准入。
冻结源码04ad15c，累计930次、保守US$5.224921/US$12，unknown/reserved均0；原128份
评分不变。四组后续修复独立源码06e30e2，24项相关检查分批通过，未换入冻结评分。
[任务卡](CODEX_CLI_H12_R08_TASK.md) · [机器记录](accept/HV4-H12-R08/accept.json)。
H12/MVP仍FAIL，H13未启动，未合并或推送。以下为此前任务记录。

2026-11-16：[H12-R07](CODEX_CLI_H12_R07_TASK.md)已完成本地工程验收：注册785项、
新增56项、lint/mypy279通过；逐编辑固定公开检查、真实失败反馈/当前树绑定、有界进展、
原PG恢复/澄清和隐藏信息隔离已验证；公开补充矩阵覆盖6类/5个合成项目/3种语言。
[完整报告](H12_R07_PUBLIC_FEEDBACK_20261116.md) · [协议](H12_R07_PUBLIC_FEEDBACK_PROTOCOL.md) ·
[公开矩阵](H12_R07_PUBLIC_BOUNDARY_MATRIX.md)。零新增真实调用；原128份score及
813次/US$4.211490共享账本不变。本轮停在R07，R08/H13未启动，H12质量FAIL、MVP
未放行保留；实际新题集仍须另版准备/冻结。以下为先前记录。

2026-11-16：[H12-R06](CODEX_CLI_H12_R06_TASK.md)已完成本地工程验收：注册729项、
新增62项、lint/mypy278通过；类型基线、逐断言/命令审查和共享返回值结构证明已验证。
[完整报告](H12_R06_BASELINE_JUDGE_REVIEW_20261116.md) · [协议](H12_R06_BASELINE_JUDGE_PROTOCOL.md) ·
[Benchmark Review](H12_R06_BENCHMARK_REVIEW_20261116.md)。81组观察、1104份CAS副本，
零新增真实调用；原128份score及813次/US$4.211490账本不变。旧题仍需新版本审查/冻结。
本轮停在R06，R07/R08/H13未启动，原H12质量FAIL和MVP未放行保留。以下为先前记录。

2026-11-16：[H12-R05](CODEX_CLI_H12_R05_TASK.md)已完成本地工程验收：注册667项、
新增54项、lint/mypy通过；独立全回归与最终树/原PG绑定、缺失计数null、追加审计已验证。
[完整报告](H12_R05_INDEPENDENT_REGRESSION_20261116.md) · [协议](H12_R05_REGRESSION_PROTOCOL.md)。
34组真实观察、1017份CAS副本核验；零新增真实调用，原128份score及813次/US$4.211490账本不变。
本轮停在R05，R06–R08/H13未启动，原H12质量FAIL和MVP未放行保留。以下为先前记录。

2026-11-16：[H12-R04](CODEX_CLI_H12_R04_TASK.md)已完成本地工程验收：注册548项、
新增53项、lint/mypy通过，70组PG/进程/HTTP/CAS证据已核验。
[完整报告](H12_R04_LIFECYCLE_20261116.md) · [协议](H12_R04_LIFECYCLE_PROTOCOL.md)。
零新增真实调用，原128份score及813次/US$4.211490账本不变；本轮停在R04。
R05–R08/H13未启动，原H12质量FAIL和MVP未放行保留。以下为先前各次记录。


2026-11-15：用户纠正后授权的[H12-R03](CODEX_CLI_H12_R03_TASK.md)已完成本地工程验收：
注册464项、lint/mypy通过；安全错误回执、有界纠正、原PG拒绝复用及Ask权限边界已验证。
见[完整报告](H12_R03_TOOL_RECOVERY_20261115.md)和[协议](TOOL_ERROR_TAXONOMY.md)。
新增真实调用0、原128份score及813次/US$4.211490账本不变；本轮停在R03，R04–R08/
H13未启动，原H12质量FAIL与MVP未放行保留。以下为各次历史记录。

2026-11-15：用户授权的[H12-R02](CODEX_CLI_H12_R02_TASK.md)已完成本地工程验收：
注册347项、lint/mypy通过；有界输出、Tool Call完整性、私有CAS流证据和安全续写
已验证，恢复后累计调用/费用读取原PG账本。见[完整报告](H12_R02_OUTPUT_RECOVERY_20261115.md)。
新增真实调用0、原128份score不变；本轮停在R02，原质量FAIL保留，R03–R08未启动。

2026-11-15：用户追加批准的[H12-R01](CODEX_CLI_H12_R01_TASK.md)已完成本地工程验收，
注册206项、lint/mypy通过；版本化验收契约、完整投影、阶段绑定与追加目标恢复已验证。
新增真实调用0、原128份计分哈希不变。见[完整报告](H12_R01_ACCEPTANCE_CONTRACT_20261115.md)。
本轮停在R01，R02–R08未启动，原H12/MVP FAIL保留。

2026-11-15：用户仅批准的[H12-R00](CODEX_CLI_H12_R00_TASK.md)已完成本地冻结及证据核验，
40项、lint/mypy通过，零新增付费；[交付报告](H12_R00_BASELINE_FREEZE_20261115.md)。
本轮已停止，R01–R08未启动，原H12质量FAIL不改；旧来源引用差异及NOT_RUN比对保留。

2026-11-15补充：原未入模三题已在单独冻结候选`0655b59`完成首样本，2PASS/1FAIL，
实际模型覆盖为主轮21题加补测3题，共24/24。Requests私有指定名称与公开规格不一致，
原FAIL保留。累计813次/保守US$4.211490，未知/预占0；后置回归指标修复工程253项通过。
见[补测任务卡及证据](CODEX_CLI_H12_FIRST_MODEL_RECOVERY_TASK.md)与
[完整测试及根因](H12_FULL_MATRIX_THINKING_20261115.md)。主轮分数不变，质量仍FAIL。

2026-11-15：最高思考模式完整诊断已保存24题记录，21题进入模型；开发6PASS/6FAIL，
持出6PASS/3FAIL/3NOT_RUN。后17题最多2题并发，仍共用原预算。见
[完整测试及根因记录](H12_FULL_MATRIX_THINKING_20261115.md)，质量FAIL、MVP未放行。

2026-11-15用户要求所有新DeepSeek调用开启思考并使用最高`max`强度；实现与验收见
[推理模式修复记录](DEEPSEEK_REASONING_20261115.md)。H12旧配置与历史分数保留，
新模式的真实评分须重新准备和冻结。

自然语言项目入口的本轮整合见[产品任务卡](HARNESS_PRODUCT_TASK.md)；它复用既有
Harness 组件并追加独立验收，不改变下列 H12 质量门和历史状态。

2026-11-14：首笔公开候选UNKNOWN已依据用户供应商账单完成费用核对，原预占释放，
次数不退回、Token不编造、原请求不重发；续测两题各FAIL，追加视图0PASS/3FAIL/
9NOT_RUN，按冻结门槛停止。累计333次、保守US$1.483486、未知/预占0，MVP未放行。
见[核账与续测记录](H12_BILLING_RECONCILIATION_20261114.md)。

2026-11-13 H12 v2 质量结论：新独立持出集12/12零模型就绪；当前冻结的真实开发集0 PASS、3 FAIL、9 NOT_RUN，最高只能9/12，质量门 FAIL。真实持出模型评分 NOT_RUN，编码 MVP 未放行。原H12成绩与失败历史继续保留。见[本轮质量验收](H12_QUALITY_ACCEPTANCE_20261113.md)及[后续任务](H12_REVISION_AND_FOLLOWUPS.md)
和[v2草案](../../experiments/harness-v4/benchmark-v2/README.md)。下文原21/24为原v4记录。
在线上下文压缩与布局修复已部署，工程回归去重988项通过，见
[修复与使用说明](CONTEXT_AND_UI_REPAIR_20261112.md)。这不改变原H12质量门。

质量FAIL后的[离线运行时修复](H12_RUNTIME_REPAIR_20261113.md)在独立repair分支完成
39项定向工程覆盖，修调用额度可见性、真实测试分项及原10项Docker归属检查。
公开重现/行为probe及新冻结真实评分仍待完成，未合main、未新增付费，MVP仍未放行。

状态：**IN_PROGRESS**（2026-11-12）。H00/H01 已完成并有接收记录；H02 已通过外部复核，
真实模型门仍为 `PARTIAL`；H03 已实现，历史工程/安全门为 PASS，但最终独立接收记录
尚待归档核实，保留 `PENDING_EXTERNAL_REVIEW`。H04–H09 已有各自历史提交的
`ACCEPTED / PASS_AFTER_INDEPENDENT_RECHECK`；H10 原实现独立复验 FAIL 保留，`c140dad`
的 F01/F02 修复已获范围限定的独立复验 PASS。H11 在 `d3f3457` 上已通过范围限定的独立复验。
H12 已实施并完成可执行范围评测，等待独立复验；H13–H18 仍为 `PLANNED`。
H12 首轮历史真实模型门 PASS、真实 Agent 门 FAIL、真实发布门 NOT_RUN，其他报告的 OPEN 项保持原状态。

2026-11-12 追加 Harness/工具/环境修复：分页与固定命令可纠正、按框架解析测试、工具批次停滞检测、
CAS 分块与恢复、安全内部链接及零模型真实预检已实现；本轮未新增付费调用。
开发环境仍有原公开回归与隔离约束冲突，付费门保持关闭，待独立复验。
H12专项351项、H00–H12聚合700项（含H12）与兼容24项通过。当前零模型可用性为10/24：开发5/12（v3）、原持出5/12（原v2只读审计）；不是模型通过率。
见[修复说明](H12_REPAIR_20261112.md)与[本轮接收记录](accept/HV4-H12-REPAIR/accept.json)。

上述10/24为上一轮快照。用户随后授权执行[环境阻塞修复任务书](CODEX_CLI_H12_ENV_UNBLOCK_TASK.md)：
最新同一源码预检为 **21/24 READY**（开发11/12、原持出10/12）；注册409项、聚合758项（含H12）及兼容24项通过，新增真实模型调用0。
三项冻结基线/资产/公开时间断言阻塞保留，另有crypto原测试不稳定项OPEN，付费与MVP门未放行。
见[本轮修复与限制](H12_ENV_REPAIR_20261112.md)及[接收记录](accept/HV4-H12-ENV/accept.json)。

H12 首轮历史源码 `ce338bf`，指纹 `84ad1b9af3e998f77af01075866044f34c98cc933fae622ede57e3900e09c7d2`。
注册177项、H00–H12聚合526项、旧上下文与Engine恢复24项和静态检查通过，范围限定于列明工程与安全检查。
最终开发集0/12（7 FAIL、5 NOT_RUN），持出集0/12（5 FAIL、7 NOT_RUN）；编码MVP未放行。
全部候选累计195次派发，保守费用上界US$0.839914，未知/预占均为0。首轮失败和追加计分更正分开保留。
见[完整结果](H12_RESULTS.md)、[接收摘要](accept/HV4-H12/accept.json)与[复现说明](H12_EVALUATION.md)。

H10 修复源码 `c140dad`：独立复验确认注册68项、聚合281项、旧上下文15项及静态检查
全部通过，原始复现与12项独立边界/变异检查通过。源码指纹
`38a02725fe2a5b5967e2604951a0749d69aeea1c5b1640e6dc65901d10d10de8` 一致，报告已由
`9b52190` 本地提交。此结论限定于 F01 终态树绑定、F02 生成租约日志来源及列明回归，
不关闭其他报告中的发现。见 [独立复验报告](accept/HV4-H10/repair-c140dad-independent-recheck-20261111T120756Z.json)。
H11 独立复跑注册68项、H00–H11聚合349项、旧上下文15项与既有 Engine 恢复9项全通过，lint/typecheck通过；另有16组自建探针通过，包含完整服务真实 SIGKILL 恢复。首轮探针计数断言失败已保留，修正后四场景全通过。见 [独立复验报告](accept/HV4-H11/independent-recheck-20261111T134513Z.json)；[实施证据](accept/HV4-H11/accept.json) 的原始字段保留。
H11 的接线与故障矩阵见 [实施任务卡](CODEX_CLI_H11_TASK.md) 和 [恢复协议](RECOVERY.md)。

H10 首轮实施历史：源码 `5707785`，注册验收42项、H00–H10聚合255项、旧上下文15项及
lint/typecheck通过。原文分页、三次以上压缩、运行期版本与持久摘要已接入API；
三个live门均未运行，付费调用和推送为0。见 [H10说明](LONG_CONTEXT.md)、
[待独立复验记录](accept/HV4-H10/accept.json)；独立验收读取
[复验任务卡](CODEX_CLI_H10_REVIEW_TASK.md)。当时 H11 未启动；当前状态以上文为准。

2026-11-11 哈希边界复验的历史记录（保留）：`98d3042` 的宽泛 `.coverage*` 排除被独立文件
探针复现为配置遗漏，记为 FAIL；收窄规则的修复 `096300e` 已通过本轮**独立复验 PASS**。
两轮聚合分别为 H04–H09 各130项、H00–H09 各213项；45项独立探针、两种变异检测、
原 H06 用例10次稳定性、lint/typecheck 和 H06/H09 注册门均通过。此结论限定于本轮复验范围。当前状态和勘误见
[记录对齐摘要](RECORD_RECONCILIATION_20261111.json)，最终独立结果由
[哈希复验记录](accept/HV4-H09/workspace-hash-recheck-20261111.json) 单独归档；
原始记录和失败证据继续保留。
应用功能基线仍为 `1d07285aa27ece9bae4ccbedc3a77ce2fbcbaf3a`；本轮起点为 `68dc81a`。

| 范围 | 当前可核对状态 | 能力边界 |
| --- | --- | --- |
| H00–H02 | 基线、契约、Python 后端选型、模型适配已实现；H02 外部复核 PASS | 第二供应商真实调用未验收；H02 live 为 PARTIAL |
| H03 | 工作区保护已实现，历史工程/安全 PASS，独立接收元数据待核实 | 历史覆盖为 `a803784` / `0bbe935`，不认证后续源码 |
| H04–H06 | Broker、增量编辑、命令与日志已有历史独立 PASS | 依赖准备仍拒绝；pytest/Node 真实执行尚未验收 |
| H07–H09 | 统一循环、首页/API、事件流与交互已有历史独立 PASS | 脚本化模型证据与弱化守卫启发式仍适用；Run/账本接线见 H11 |
| 哈希边界修复 | 原规则配置遗漏 FAIL 保留，`096300e` 独立复验 PASS | 不复用旧工作区与旧树的测试/审批结论 |
| H10 | 原实现独立FAIL保留；c140dad F01/F02 独立复验PASS | 其他报告发现继续OPEN，live三门NOT_RUN |
| H11 | d3f3457 一致恢复独立复验 PASS | 仅 H11 工程范围；live 未跑，其他 OPEN 项保留 |
| H12 | 历史实施记录保留；v2独立持出零模型就绪、同源注册441项PASS | v2开发0 PASS/3 FAIL/9 NOT_RUN，质量FAIL；持出模型NOT_RUN，MVP未放行 |

> 历史状态快照（2026-11-10，保留原文）：
> 状态：**HV4-H00 / HV4-H01 已被 Super-Agent 接收；HV4-H02 已实施、等待外部复验；
> H03–H18 仍为 PLANNED**（2026-11-10）。审计日期：
> 2026-11-10。应用基线：`1d07285aa27ece9bae4ccbedc3a77ce2fbcbaf3a`。
冻结基线、任务集与评分协议见 [基线回归日志](BASELINE_REGRESSION.md)、
[能力审计](CAPABILITY_AUDIT.md)、[任务类型契约](TASK_TYPES.md) 与
[评分协议 v1](SCORING_PROTOCOL.md)。H01 接口见 [Harness 契约 v1.1](HARNESS_CONTRACTS.md)、
[选型 ADR](adr/HV4-H01-deepseek-harness.md) 与 [Super-Agent 交接](SUPER_AGENT_HANDOFF.md)。
H02 模型适配器见 [模型能力与协议](MODEL_ADAPTERS.md)。

本规划的目标是让用户选定项目、描述目标后，Agent 能在同一会话内调查、修改文件、
运行验证、根据失败继续修复，最终交付可检查的变更。第一版面向单机管理员，沿用
现有网页与 Python 服务；支持本机项目和 GitHub 工作区。多人托管、远程 Runner、
多 Agent 和桌面客户端分阶段推进。

- [逐项任务书与依赖](TASKS.md)
- [架构、里程碑与排期](ROADMAP.md)
- [验收与评测方案](ACCEPTANCE.md)
- [机器可读任务队列（当前状态与历史记录分列）](plan.json)
- [2026-11-11 验收记录对齐与勘误](RECORD_RECONCILIATION_20261111.json)
- [资料版本与来源](sources.json)

## Harness 在本项目中的含义

Harness 是模型外部的执行系统：模型接口、项目工作区、工具、权限、会话与上下文、
执行循环、验证、恢复、交互和扩展机制共同工作。用户描述的是任务目标，系统负责
组织实际操作，并用文件变更、命令退出码和测试回执说明完成情况。

“能循环调用模型”“有流式聊天”“第二次调用模型检查回答”已是有用基础；通用
编码任务还需要实际编辑、执行与验证的闭环。模型自身的推理能力与 Harness 的
可靠性分别评估；接入另一个模型不能自动补齐工作区和执行能力。

## 基线能力审计快照（2026-11-10）

以下表格及旧回归数描述当时应用基线，不代表当前 Harness 卡片的实现进度；当前状态见页首。

| 层面 | 已有实现 | 本次审计结论 |
| --- | --- | --- |
| 网页体验 | 账户登录、网页配置凭据与许可、历史追问、Enter/Shift+Enter | 已接入首页，继续复用 |
| 只读分析 | 固定 GitHub Commit、搜索/阅读/Issue/PR、证据引用 | 已接入首页，属于真实只读链路 |
| 自动推进 | Token/费用/时间预算、重复读取复用、无进展防护、回答核验反馈 | 已实现于仓库分析；不能据此推定旧修复器也已升级 |
| 流式与恢复 | 供应商 SSE、报告草稿、持久事件游标、刷新恢复、checkpoint/epoch | 已有；目前公开草稿主要是 conclusion/structure，不是通用终端/补丁事件协议 |
| 业务基础 | Engine、PostgreSQL、预算预占/结算、CAS、Action/Outbox、哈希审批 | 有明确可复用价值，保持权威边界 |
| 编码组件 | 固定版本快照、受限多文件编辑器、Docker 沙箱、Go/Python 适配、修复反馈、Review Bundle | 已存在于其他链路，覆盖范围有限，尚未成为首页通用编码能力 |
| 上下文与记忆 | 确定性压缩、引用/失败/权限不变量、工作记忆和专家模块 | 已有实现；后台压缩标记为未实现，完整编码闭环效果尚未验收 |
| 验证基础 | 注册验收、故障注入、评测 Harness、分开记录工程/live 门 | 已有基础；历史真实修复失败和未执行项必须保留 |

最近一轮会话中的 `AGENT-UI` 为 61 项通过、`V35-R05` 为 27 项通过；使用真实
PostgreSQL 与受控模型响应。这些是当时源码的工程回归记录，本次规划未重新执行，
也不代表真实通用编码能力通过。V3.5 E02 记录的真实模型/Agent 门仍为 `FAIL`，
真实 GitHub 写门为 `NOT_RUN`；更早 R5/P0 的受限真实 PR 交付只覆盖各自的实验范围。

## 三个参照产品的基线差距审计（2026-11-10）

以下差距表保留原始规划依据；H02–H09 的后续交付与残余限制以页首和对应 accept 记录为准。

参照版本固定在 [sources.json](sources.json)。对 Codex、Claude Code 和 DeepSeek
Harness 的描述来自官方公开资料；本次没有安装运行或进行同题性能对照，不给成功率
排名，也不推断闭源产品的内部实现。

- **Codex**：[官方说明](https://github.com/openai/codex/blob/77fde36ae112437a8ce23e752209099ee04a0d50/README.md)
  覆盖本机 CLI、IDE 和 Web；[公开工具实现](https://github.com/openai/codex/blob/77fde36ae112437a8ce23e752209099ee04a0d50/codex-rs/core/src/tools/handlers/mod.rs) 包含补丁编辑、
  unified exec、计划和交互等能力，另有 AGENTS.md、Skills 与安全机制说明。
- **Claude Code**：[官方说明](https://github.com/anthropics/claude-code/blob/ce6cb8ee39759afb5d8bf2e3fd26582f575e9095/README.md)
  以终端中的项目编码和 Git 工作流为核心，同时提供 IDE
  与 GitHub 使用方式；公开插件说明与更新记录涉及工具、权限、会话恢复和上下文。
- **DeepSeek Harness**：指 DeepSeek 官方的
  [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)。它以
  Cordis 实现[可组合插件](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/architecture.md)，模型、工具注册、会话日志与 Agent Loop 均可配置替换，
  提供 Web、Desktop、headless、SDK、ACP 等入口。官方仍标注 developer preview，
  因此其架构可作为参照，稳定性需要按具体版本实测。

| 能力 | MAGR 当前实际差距 | 优先级 / 任务 |
| --- | --- | --- |
| 同一会话完成编码 | 首页走只读分析；修复、测试和审批属于另一套入口与编排 | P0：H01、H07、H08 |
| 操作用户项目 | 有 GitHub 快照与临时工作树；首页缺少本机目录、脏工作区、工作区归属和冲突处理 | P0：H03 |
| 可靠增量编辑 | 旧修复协议要求完整替换文件；大文件的小修改也可能耗尽输出窗口 | P0：H05 |
| 实际执行与环境 | 有固定沙箱测试；缺少首页可调用的通用命令、依赖准备、服务启动、持续日志及进程控制 | P0：H04、H06 |
| 通用任务验收 | 当前分析核验是引用检查加模型评审；旧修复器要求基线先失败，不能直接覆盖新增功能/正常重构 | P0：H07 |
| 自动编码循环 | 分析已按资源推进，旧 RepairBudget 仍默认 3 次尝试、10 次测试 | P0：H07 |
| 可替换模型 | 分析客户端硬编码 DeepSeek 请求与结构化 action/report；未形成统一工具调用与能力协商接口 | P0：H02 |
| 会话中途协作 | 首页执行时禁用输入；缺少持久收件箱、安全点接收新指示、缺信息时的 ask_user | P0/P1：H09 |
| 完整执行流 | 已有报告 SSE；需要增量补丁、终端 stdout/stderr、权限请求、测试和产物事件 | P0/P1：H06、H09 |
| 长任务上下文 | 有压缩组件；需要把计划、验收缺口、工作树版本、命令状态及分页原文串进编码循环 | P1：H10 |
| 崩溃后的工作恢复 | H11 已验证完整工作区、owned 子进程、账本和事件的一致恢复；未知模型结果保留预占并阻止重发 | d3f3457 独立工程复验 PASS；真实能力门见 H12 |
| Skills / MCP / 插件生态 | H14 已交付项目规则、Skills、操作者插件锁与 GitHub／知识／数据 MCP 适配 | [H14 工程验收](H14_ACCEPTANCE_20261118.md) |
| SDK / 自动化 / 远程运行 | CLI 目前主要调用业务 API；缺少统一 headless/SDK 协议和远程 Runner 配对 | P1/P2：H13、H15 |
| 多用户和规模化 | H16 已验收项目隔离、有界调度和单写者进程拓扑 | [H16 工程验收](H16_ACCEPTANCE_20261118.md)、H18 |
| 能力证据 | 工程回归较丰富；通用真实修复、持出集和真实模型对照证据不足 | P0/P1：H00、H12 |

## 历史阻塞点与后续接线边界

以下保留旧修复器与规划时的阻塞证据。H05/H08 已实现增量编辑，H07 支持多种任务契约；
旧 RepairAgent / Workbench 分支没有因此被视为已经全面升级。

1. [V35-E02](../v35/cards/V35-E02.md) 记录：修改 142KB 的 `src/click/core.py`
   时，完整文件输出协议与 8192 输出 Token 上限冲突，四臂全部 `PATCH_TRUNCATED`。
   增加累计 Token 预算不能解决单次输出窗口不足，H05 必须先完成。
2. [RepairAgent](../../src/mgar_v3/repair/repair_agent.py) 在基线不是 `test_failed`
   时返回 `baseline_not_reproduced`。这适用于部分 Bug 复现，新增功能和保持行为的
   重构需要各自的验收契约，不能直接复用这条前置条件。

此外，`WorkbenchService._prepare_repair` 对非 fixture 仍返回
`LIVE_REPAIR_EXECUTION_NOT_ENABLED`。H08 的目标是接入经过授权、可验证的真实编码
运行时，不能仅删除 403 分支。

## 路线与已落实的选型

H01 ADR 已选择现有 Python 内核加公共端口；DSH 受控接线当时因无 Node 运行时记为
NOT_RUN。以下保留最初推荐方向，后续卡片按冻结契约推进。

保留 Python/FastAPI、Engine/PostgreSQL、CAS、现有预算与发布协议，新增薄的 Harness
接口层，将模型、工具、执行环境和事件解耦。首页与 CLI 使用同一运行时；旧 API
通过适配保持兼容。

H01 先进行有限的 DeepSeek Harness 复用验证：考察其 SDK/插件是否能把每次模型
请求和工具操作送入现有授权、预算与回执入口。满足契约时可以采用它作为执行后端；
达不到时，以现有 Python 实现最小闭环。资料中“可插件化”不等于本项目已经完成
集成，正式选型以验证产物为准。

第一阶段建议交付：**单机、单管理员、Python 与 JS/TS 项目、读/改/执行/验证闭环、
实时日志与 Diff、暂停恢复、当前预算内自动推进、最终可检查的变更产物**。Go 复用
现有适配器做兼容验证。真实 PR 发布继续走原有独立写授权与 Review Bundle。

首批实施顺序：H00 → H01 → H02/H03 → H04 → H05/H06 → H07 → H08。
可演示闭环完成后，通过 H09–H12 的交互、长任务、故障与真实评测门再发布 MVP。
多 Agent、应用商店和桌面打包放在单 Agent 编码质量稳定之后，详见 [排期](ROADMAP.md)。

## 代码审计入口

- 首页运行与追问：[repository_analysis/service.py](../../src/mgar_v3/repository_analysis/service.py)、
  [web/agent.js](../../web/agent.js)
- 只读工具契约：[repository_analysis/domain.py](../../src/mgar_v3/repository_analysis/domain.py)
- 报告核验与防循环：[repository_analysis/agent.py](../../src/mgar_v3/repository_analysis/agent.py)
- 模型与报告流：[model.py](../../src/mgar_v3/repository_analysis/model.py)、
  [streaming.py](../../src/mgar_v3/repository_analysis/streaming.py)
- 快照与编辑：[repository_analysis/workspace.py](../../src/mgar_v3/repository_analysis/workspace.py)、
  [repair/worktree.py](../../src/mgar_v3/repair/worktree.py)
- 修复与沙箱：[repair/repair_agent.py](../../src/mgar_v3/repair/repair_agent.py)、
  [repair/providers/structured.py](../../src/mgar_v3/repair/providers/structured.py)、
  [repair/sandbox_executor.py](../../src/mgar_v3/repair/sandbox_executor.py)
- 接线与业务权威：[workbench/service.py](../../src/mgar_v3/workbench/service.py)、
  [workbench/orchestrator.py](../../src/mgar_v3/workbench/orchestrator.py)、
  [coordination/engine.py](../../src/mgar_v3/coordination/engine.py)
- 现有边界与历史验收：[架构契约](../v35/ARCHITECTURE_CONTRACT.md)、
  [评测索引](../v35/EVALUATION_INDEX.md)
