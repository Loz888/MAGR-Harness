# Harness V4 逐项任务书

2026-11-18，H12 R08 正式候选完成 24 题评分。开发 11/12、独立持出 11/12，合计 22/24。独立复核确认正式质量门 PASS。23 题实际调用模型，持出第 12 题在公开基线阶段阻塞。产品放行另行记录。[逐题成绩与核账](H12_R08_FORMAL_REAL_MODEL_20261118.md)

2026-11-18，R08 剩余失败题的定向复测已完成。03、08 在入口修复后通过。05 保留首次复测的 FAIL，在读取参数纠错修复后通过。编码 MVP 仍待正式放行。[逐题报告](H12_R08_TARGETED_REPAIRS_20261118.md) · [机器记录](accept/HV4-H12-R08-READ-SIZE/accept.json)

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


2026-11-12 用户追加授权实施[自然语言产品闭环](HARNESS_PRODUCT_TASK.md)：统一首页、
持续会话、模型配置、项目权限、执行环境、预览和变更交付。本轮复用 H06–H11 与 H14
组件，现为 **ENGINEERING_PASS**，产品199项、旧回归758项和真实浏览器15项通过；
集合重叠及原始结果见配套验收记录。真实供应商调用为0，live为NOT_RUN。
不提前将 H13–H18 或 H12 质量门标为通过。

当前状态（2026-11-12）：**IN_PROGRESS**。H00/H01 已完成并有接收记录；H02 外部复核
PASS，live_model_gate 仍为 PARTIAL；H03 已实现但保留 PENDING_EXTERNAL_REVIEW；
H04–H09 已有历史提交的独立 ACCEPTED 记录；H10 `c140dad` 的 F01/F02 修复已有
范围限定的独立 PASS（文档提交 `9b52190`），其他报告发现未因此关闭。
H11 `d3f3457` 的恢复工程范围已独立复验 PASS。H12 已实施并完成评测，待独立复验；
工程聚合526项通过，真实开发/持出均0/12，MVP未放行；H13–H18仍为PLANNED。见[H12结果](H12_RESULTS.md)。
2026-11-12 追加[Harness/工具/环境修复](H12_REPAIR_20261112.md)，新增真实执行就绪门；
环境阻塞未清除前不派发付费模型，历史 FAIL 与阈值保留，待独立复验。
本轮H12注册351项、H00–H12聚合700项（含H12）、兼容24项通过；原24题零模型基础设施可用性为10/24，
开发5/12（v3）、原持出5/12（原v2只读审计），环境版本不同，不是新增模型通过率。
哈希边界修复 `096300e` 已独立复验 PASS（两轮130/213、稳定性10/10）；该历史轮次止于记录收尾。依据与勘误见
[记录对齐摘要](RECORD_RECONCILIATION_20261111.json)。各卡下方保留的旧等待状态是历史
交付时点记录，不是当前卡片推进指令。

> 历史总状态（2026-11-10，保留原文）：
> HV4-H00 / HV4-H01 已实施并被 Super-Agent 接收；HV4-H02 已实施并等待外部复验；
> H03–H18 仍为 **PLANNED**（2026-11-10）。
H00 等简称对应完整 ID `HV4-H00`；编号本身不表示已经实施。
本规划不修改 V3.5 历史卡的 DONE/FAIL/NOT_RUN，也不把历史实验许可转用于新实验。
估时为熟悉现有代码的一名工程师的人日，含卡内测试，不含外部权限等待与跨卡余量。

## 执行规则

1. 从 H00 开始，依赖工程门通过后再推进下一卡。每卡分别提交实现、相关回归与结果。
2. 复用 Engine/PostgreSQL、CAS、现有权限、修复、验证及发布服务，不增加平行业务真相。
3. 实施时才注册 `make verify TASK=HV4-Hxx`，并提供真实目标；本次不注册空测试或
   将未来能力标成 PASS。新增迁移从实施时最新编号续排，不改已发布迁移的校验和。
4. 各卡保存源码指纹、命令、退出码、JUnit、fixture/live 区分、费用和失败记录。
   原始资料放 `evidence/harness-v4/<card>/<attempt>/`，公开摘要放本目录后续的 `accept/`。
5. 模型费用和外部写继续使用对应许可及审批记录。已授权工作区内的普通编辑、测试
   由任务范围覆盖；权限不足时提供具体动作弹窗，不把每次工具调用变成确认步骤。
6. 除 H00 冻结的现有问题外，各卡不引入新的 lint/typecheck 错误；MVP 发布前完成
   H00 的基线问题清理与全量门禁。禁止用缩小检查范围掩盖新问题。

## 任务总表

| ID | 任务 | 优先级 | 依赖 | 人日 | 里程碑 |
| --- | --- | --- | --- | --- | --- |
| H00 | 冻结基线、任务类型与验收协议 | P0 | 无 | 1–2 | M0 |
| H01 | Harness 契约与 DeepSeek Harness 复用验证 | P0 | H00 | 2–3 | M0 |
| H02 | 模型能力接口与通用工具调用 | P0 | H01 | 2–3 | M1 |
| H03 | 本机/GitHub 工作区与变更保护 | P0 | H01 | 3–4 | M1 |
| H04 | 统一能力许可、执行 Broker 与隔离 | P0 | H03 | 2–3 | M1 |
| H05 | 带版本校验的增量编辑 | P0 | H04 | 3–4 | M1 |
| H06 | 命令、依赖、测试及应用进程 | P0 | H04 | 3–5 | M1 |
| H07 | 统一编码循环与多种任务验收 | P0 | H02、H05、H06 | 4–6 | M1 |
| H08 | 首页接通真实编码与变更交付 | P0 | H07 | 2–3 | M1 |
| H09 | 完整事件流、运行中消息与交互 | P1 | H08 | 2–3 | M2 |
| H10 | 长任务计划、上下文与分页工具 | P1 | H07 | 3–4 | M2 |
| H11 | 工作区/进程/账本一致恢复 | P1 | H09、H10 | 3–4 | M2 |
| H12 | 冻结任务集、真实 Agent 与持出集验收 | P0/P1 | H08、H09、H10、H11 | 3–5 | M2 |
| H13 | Headless、CLI 与 SDK 契约 | P1 | H12 | 2–4 | M3 |
| H14 | 项目指令、Skills、MCP 与插件接口 | P1 | H01、H04、H11 | 3–5 | M3 |
| H15 | 远程 Runner 配对与本机执行 | P2 | H11、H13 | 4–6 | M4 |
| H16 | 多用户项目隔离与服务端调度 | P2 | H15 | 4–6 | M4 |
| H17 | 按需子 Agent 与效果验证 | P2，可选 | H12、H14 | 3–5 | M4 |
| H18 | 可安装发布、诊断与运维指标 | P1 | H12、H13 | 2–4 | M3 |

H12 的评测用例从 H00 起设计，每张实现卡同步补充测试；H12 是最终执行与汇总，
不是等所有开发结束后才编写验收。测试和可复现环境分别计入各实现卡估时。

<a id="hv4-h00"></a>

## HV4-H00：冻结基线、任务类型与验收协议（DONE）

- **输入**：基线提交、近期 Agent UI 回归、V35 E01/E02/E03 的公开记录。
- **工作**：建立能力清单，区分首页已接入、已有组件、未接线、未实测；清理上一轮
  记录的全库 23 项 lint 与 16 项类型问题，并重新核对数量。定义 inspect、bugfix、
  feature、refactor、run_app 的成功含义；冻结首批开发/持出任务、资源上限和判定器。
- **验收**：全库 lint/typecheck 为 0 新旧错误；现有核心回归可重现。每个基准任务有
  repository/commit、目标、环境摘要和判定器哈希；测试是否缺失与任务失败明确区分。
  旧 live 失败记录原样保留。
- **产物**：`baseline.json`、能力审计表、任务类型契约、评分协议 v1、基线回归日志。
- **主要位置**：`config/verification.json`、`quality_eval/`、现有核心与测试错误所在文件。
- **落地结果**：全库 ruff 23→0、mypy 16→0；24 个任务（12 dev + 12 holdout，6 个真实
  项目，Python/JavaScript/TypeScript/Go）与逐任务判定器哈希已冻结；核心回归集通过，
  全量 922 个测试的运行未完成，已原样记录为 `NOT_COMPLETED`；H00 未执行付费模型调用
  或外部写入。证据：`experiments/harness-v4/`、`docs/harness-v4/accept/HV4-H00/`。

<a id="hv4-h01"></a>

## HV4-H01：Harness 契约与 DeepSeek Harness 复用验证（DONE，已接收）

- **输入**：H00 的状态所有权和任务契约；本规划固定的官方 DeepSeek Harness 版本。
- **工作**：定义 ModelAdapter、ToolRegistry、WorkspaceProvider、ExecutionBroker、
  ContextManager、Verifier、EventSink 的最小接口。将会话/轮次/步骤映射到现有
  Thread/Run/epoch/事件，Task/Finding/Approval 继续使用原有权威。为旧只读分析与
  修复器提供薄适配层。
- **复用验证**：至多投入 2 人日，以受控模型验证 DeepSeek Harness 的 SDK/插件
  接线：模型调用先预算预占、工具先权限检查、最终回执进入 Engine、取消能传递、
  不产生第二份有独立裁决权的业务状态。比较 Python 内核与 DSH 执行后端的接入成本。
- **验收**：同一小任务经公共接口完成“读 → 改 → 测试”，模型和执行器均可替换为
  测试适配器；任何适配器不能绕过权限/预算入口。若 DSH 不满足契约，明确记录缺口，
  选择 Python 路线，不阻塞后续卡。
- **产物**：正式选型 ADR、版本化接口、状态映射、受控接线样例及失败分支记录。
- **主要位置**：建议新建 `src/mgar_v3/harness/` 作为接口与组合层，复用现有服务实现。
- **落地结果**：已冻结 `harness-contract-v1.1`：TaskBinding、CapabilityGrant、
  ModelAdapter、ToolRegistry、WorkspaceProvider、ExecutionBroker、ContextManager、
  Verifier、EventSink、BudgetLedger；参考回路用受控模型适配器完成“读 → 改 →
  真实 Python 测试”，模型/执行器均可替换。v1 独立评审发现未声明工具与只读
  write 工具会进入 handler；v1.1 改为 fail-closed：未声明名称直接拒绝，已声明
  工具按 capability 校验路径/argv。事件映射已对齐 `run_events.event_id` 与
  `safe_payload`；actor 为大小写敏感的受信内部 principal。补充定位并修复了
  `finish_success` 的两事务窗口：run 终态与 `stop_reason/report_hash` 现在同一
  Engine 事务提交，并有确定性 DB 回归。旧 `e1b3214` 探针 FAIL、核心回归旧失败与
  DSH `NOT_RUN_NO_NODE_RUNTIME` 均原样保留。Super-Agent 已独立复验并接收
  H00/H01 v1.1（merge `782f35c`）。
  证据：`docs/harness-v4/adr/HV4-H01-deepseek-harness.md`、
  `docs/harness-v4/HARNESS_CONTRACTS.md`、`experiments/harness-v4/harness-contract-v1.json`、
  `docs/harness-v4/accept/HV4-H01/`。

<a id="hv4-h02"></a>

## HV4-H02：模型能力接口与通用工具调用（DONE，待外部复验）

> 当前：DONE / PASS_EXTERNAL_REVIEW（`169925f`）；真实模型门 PARTIAL。标题内原“待外部复验”是历史快照。

- **工作**：实现统一消息、工具声明/参数增量、工具结果、公开文字、finish reason、
  usage 与取消事件；DeepSeek 为默认适配器，再实现一种不同协议的适配器，协议由
  H01 选定。明确 context/output 上限、token 计数、结构化工具和价格表能力。
- **约束**：同一请求冻结模型和价格版本；换模型适用于后续明确轮次。已知未发送/
  明确拒绝可有界重试，缺 usage/结果不明不得自动切换供应商重发。兼容旧 JSON
  action/report，不要求所有工具交互都塞进一份最终报告 JSON。
- **验收**：两个适配器通过同一工具契约测试；分片 UTF-8/JSON、工具参数中断、
  usage 缺失、上下文超限与取消均有正确状态。缓存结果不重复计费，预占与结算一致。
  受控协议通过和真实供应商通过分别记录。
- **产物**：模型能力清单、适配器、协议样本、计费和失败映射说明。
- **主要位置**：`repository_analysis/model.py`、`models/`、Harness 模型端口与现有账本。
- **落地结果**：新增强类型 `harness-model-v1`，实现 DeepSeek OpenAI-compatible
  SSE 与 Anthropic Messages SSE 两个适配器；统一文字/工具参数增量、finish reason、
  usage、取消、上下文上限与价格版本；缺失 usage 标记 `known=false` 并由 runtime
  保留预占，不按 0 费用结算；已知 429/5xx 同供应商有界重试一次，不切换供应商；
  已知成功结果缓存命中按 0 费用结算。外部复验发现并修复五项问题：HTTP 200 后
  read timeout 不再二次 POST；非法/缺失 usage 始终 unknown；发送后取消与畸形 SSE
  保留预占；多轮工具历史保留原生 `tool_calls`/`tool_use` 形状；CRLF 跨 chunk
  正确解码。第二次外部复验又修复 P1/P2：`error`/`length` 与无最终 finish 的
  响应不再执行已缓冲的工具提案；Anthropic 负数 tokens 映射 unknown 并保留预占。
  受控协议 30 项测试通过。真实 DeepSeek Flash 在授权范围内实际调用 2 次并记录
  （tool_calls + 原生工具历史，按峰值价格上界计算 409 micros，非供应商账单；
  单卡上限 2 USD）；Anthropic 真实调用未授权，记 `NOT_RUN_NOT_AUTHORIZED`，
  不宣称双供应商真实通过。runtime incomplete 仅因 verifier 条件未满足。
  证据：`docs/harness-v4/MODEL_ADAPTERS.md`、
  `experiments/harness-v4/model-capabilities-v1.json`、`tests/test_harness_v4_h02.py`、
  `docs/harness-v4/accept/HV4-H02/`。

<a id="hv4-h03"></a>

## HV4-H03：本机/GitHub 工作区与变更保护

> 当前：IMPLEMENTED；accept 保留 PENDING_EXTERNAL_REVIEW。历史 verify 覆盖 `a803784` / `0bbe935`，同一源码指纹；交接称已接收，但最终独立裁决元数据未归档。见记录对齐摘要中的 h03_historical_record_audit。

- **工作**：项目注册产生 workspace_id；支持固定 Commit 的 URL 工作区和本机项目。
  本机项目以隔离分支/副本承接修改，记录用户已有 dirty/untracked 文件；新增读文件、
  搜索、目录、Git 状态与差异工具。执行、编辑与语言服务使用同一个工作区版本。
- **边界**：浏览器传入绝对路径不会自动获得文件系统权限。本机根目录由操作者明确
  注册；云端服务器访问用户电脑目录属于 H15，不假设 URL 能访问客户端磁盘。
- **验收**：两个任务工作区互不覆盖；同仓库并发冲突可检测。用户原改动不丢失；
  应用回用户目录前校验 pre-image。路径穿越、symlink、`.git`/hooks/filters/submodule
  执行与任务外目录访问测试通过。暂停和重启后可恢复 manifest/tree hash。
- **产物**：工作区 manifest、版本/归属接口、dirty 处理策略、冲突回执。
- **主要位置**：`repository_analysis/workspace.py`、`repair/workspace.py`、`repair/worktree.py`。
- **落地结果**：新增 `src/mgar_v3/harness/workspace.py` 与
  `harness-workspace-v1`；操作者显式注册本机绝对根或固定 Commit 的 GitHub URL，
  产生内容寻址 `workspace_id`；每任务独立副本，记录 tracked/untracked dirty 清单；
  `apply_back` 校验注册路径 hash、基线 hash 与当前 pre-image，冲突零写入；
  路径穿越、symlink、`.git`、hooks、filters、submodule 均拒绝或排除；提供读文件、
  目录、搜索、状态、差异、版本化编辑六类工具；manifest/tree hash 可重开恢复。
  独立复验又修复三项：apply 后持久化 manifest、search 拒绝 symlink 根与嵌套链接、
  校验 baseline 完整性并拒绝 `.mgar` 元数据访问；第二轮复验再修复并发回写丢失
  更新与 local clean filter 执行；第三轮复验修复 staged-only dirty 漏记与
  fsmonitor 执行；第四轮复验修复 `git rm --cached` 双事实漏报与未暂存删除误标；
  第五/六轮复验修复 dirty inventory 与 diff 的叶子/父目录 symlink 读取；第七轮
  复验修复元数据临时文件 symlink 写越界（随机 O_EXCL + 原子替换、NOFOLLOW 读取）。
  dirty 现覆盖 HEAD/index/worktree 全状态。25 项真实文件/真实 Git 进程测试通过；
  H00/H01/H02 58 项回归通过；注册验收 `make verify TASK=HV4-H03` PASS。真实模型
  调用未要求，费用 0；真实 GitHub 网络抓取与发布门未运行。证据：
  `docs/harness-v4/WORKSPACES.md`、`docs/harness-v4/accept/HV4-H03/`。

<a id="hv4-h04"></a>

## HV4-H04：统一能力许可、执行 Broker 与隔离

> 当前：历史 `c707d69` 已 ACCEPTED / PASS_AFTER_INDEPENDENT_RECHECK，engineering/security 为 PASS。下方 security FAIL 与等待复验是旧失败及实现方修复时点记录，继续保留；不覆盖本轮哈希候选。

- **工作**：在账户与预算许可上增加工作区、可读/可写范围、命令执行、网络、时限、
  凭据和外部写能力。工具调用在执行入口强制校验 actor、scope、epoch 与预算；
  内建工具和后续 MCP/插件调用使用同一入口。
- **交互**：预设只调查、在指定工作区修改并测试、单独发布三类权限。旧的“只读分析”
  许可保持只读；用户首次选定可写模式时确认具体范围，范围内操作持续执行。遇到
  新依赖源、保护文件或外部副作用时说明所需能力并提供弹窗/降级路径。
- **隔离**：使用实际容器/OS 边界约束进程、挂载和网络；模型密钥、PG/Redis 与 GitHub
  凭据由 Broker 持有，不交给项目进程。依赖准备与运行阶段分开；外部写继续走
  Action/Approval/Outbox。命令字符串黑名单不是隔离实现。
- **验收**：已授权编辑/测试不逐次弹窗；越权路径、进程和网络均在操作前阻止；
  注入文本/Skill/插件不能扩大许可。命令在沙箱内实际执行并验证挂载、用户、资源限额。
- **产物**：能力许可契约、执行 Broker、策略用例、迁移及兼容说明。
- **历史落地结果（独立 PASS 前的修复快照，原 FAIL 继续保留）**：新增 `harness-capability-lease-v1` 与
  `IsolatedExecutionBroker`；investigate/modify_test/publish 三档预设，旧只读
  grant 不升级，可写模式需显式写路径与批准；执行入口逐次读取当前租约并校验
  actor/workspace/repository/epoch/cwd/命令/预算/撤权/有效期，读写取
  `lease ∩ grant` 最窄范围，超时取三者最小值；真实 Docker 容器使用 pinned R03
  镜像、非 root、只读根、network none、cap drop、cgroup 限额，只挂载批准范围，
  `.git`/`.mgar`/`.env` 等保护路径在读写根内也被 tmpfs 与 `/dev/null` 屏蔽；宿主
  symlink scope 在创建容器前拒绝，内建工具按 realpath 授权，缺少 realpath 解析
  能力时四个内建工具失败关闭；凭据仅
  Broker 持有；超时终止并清理；publish 与依赖准备分别拒绝，留给
  Action/Approval/Outbox 与 H06。独立验收曾以真实容器复现越权读写并判 FAIL；
  修复后 27 项 H04 测试（含范围允许/拒绝矩阵、保护元数据、旧 grant 交集、内建
  工具过期/撤权/预算/epoch 负例、symlink、resolver 缺失失败关闭与真实容器清理）
  与 H00–H03 83 项回归
  通过；注册验收 `make verify TASK=HV4-H04` PASS（commit `c707d69`，source
  `c2fb38ee…`）。`security_gate` 保持 FAIL，等待独立复验；原始失败保留在
  `evidence/harness-v4/HV4-H04/recheck-20261111/original-failure/`。命令
  allowlist 只约束入口 argv[0]，不是进程树隔离；子进程仍受容器边界约束。无
  模型/外部发布/真实密钥，费用 0；未新增迁移，不占用 0033。证据：
  `docs/harness-v4/CAPABILITY_BROKER.md`、`docs/harness-v4/accept/HV4-H04/`。

<a id="hv4-h05"></a>

## HV4-H05：带版本校验的增量编辑

> 当前：历史 `a757a6b` 已 ACCEPTED / PASS_AFTER_INDEPENDENT_RECHECK。下方 PENDING_RECHECK / 不启动 H06 是旧交付快照。

- **工作**：增加小范围替换或上下文补丁协议，使用文件/片段 hash 和唯一上下文定位；
  原子处理跨文件变更。保留原有完整文件协议的兼容适配，不再让大文件小修复必须
  返回全部内容。支持创建/修改；删除/重命名按单独能力约束执行。
- **验收**：在至少 142KB 文件的多个位置完成小修改，模型输出随改动规模增长；
  重复片段、CRLF/UTF-8、旧版本、路径冲突均能确定拒绝或正确应用。第二个文件失败
  时整组回滚，重复提交不会重复插入；Diff 由真实工作树生成。运行失败后保留底稿和
  可复核的候选版本，失效审批不会继续使用。
- **产物**：版本化编辑契约、PatchReceipt、Diff/CAS 产物、对历史截断场景的回归用例。
- **主要位置**：`repair/providers/structured.py`、`repair/worktree.py` 与工具注册接口。

落地结果：`FileEdit` 在保留完整文件 `content` 协议的同时支持
`replacements`（文件前像 sha256 + 片段 sha256 + 唯一上下文/occurrence），
`create/update/delete/rename` 分别受 `allow_new_files`、`max_deleted_files`、
`allow_rename` 约束；多文件整组原子应用，失败回滚且 `candidate()` 保留候选；
`PatchReceipt` 记录前后 sha256 与真实工作树 Diff 的 sha256；CRLF 归一化匹配后
按原换行写回，UTF-8 原样处理；结构化 provider 拒绝原始 diff/hunks/命令，按输出
字节计量预算。14 项 H05 测试与 H00–H04 110 项回归、ruff、mypy 199 files 通过；
注册验收 `make verify TASK=HV4-H05` PASS（commit `a757a6b`，source
`bf29dccb…`）。契约见 `docs/harness-v4/INCREMENTAL_EDITS.md`；验收记录
`docs/harness-v4/accept/HV4-H05/accept.json`。状态 `PENDING_RECHECK`，
等待独立复验，不启动 H06。

<a id="hv4-h06"></a>

## HV4-H06：命令、依赖、测试及应用进程

> 当前：历史 `e52347f` 已 ACCEPTED / PASS_AFTER_INDEPENDENT_RECHECK。下方 PENDING_RECHECK / 不启动 H07 是旧交付快照；后续哈希规则另行复验，pytest/Node 真实执行仍未验收。

- **工作**：提供 run/poll/cancel 命令句柄，记录 argv、cwd、环境摘要、退出码、
  stdout/stderr、超时和产物；首版支持非交互命令及受控后台服务。增加 Python
  pytest/现有 unittest、Node/TS 测试与构建适配，保留 Go 回归。
- **环境**：识别 lockfile 和启动说明，经过 H04 的依赖准备通道安装到隔离环境；
  不可满足的系统包、私有源或离线缺包返回可操作的等待状态。启动应用时给出就绪
  检查和仅限授权范围的访问地址；退出、超时或取消时清理任务所拥有的子进程。
- **验收**：公开测试真实运行；无测试/全部跳过/退出 0 但未运行目标不能冒充通过。
  大日志可分页、流式有背压；超时杀死进程组。Python/JS 项目分别完成准备、测试、
  构建/启动及停止；缺依赖有真实失败回执，不能伪造测试数。终端与编辑可见同一版本。
- **产物**：CommandHandle、执行/测试/就绪回执、语言适配、日志和产物分页接口。
- **主要位置**：`repair/sandbox_executor.py`、`verification/`、Broker 与受控 Runner。

落地结果：新增 `CommandSession`（`run/poll/read_log/wait/cancel/close`）与
`ServiceLaunch`，全部容器仍由 H04 `IsolatedExecutionBroker.launch` 创建并复用
lease/epoch/预算/scope/allowlist；日志按 `page_bytes` 分页、缓冲超限丢弃最旧
字节并报告 `dropped_bytes`；deadline 与 cancel 均 `docker rm -f` 整个容器；
就绪探测在容器网络命名空间内执行，地址仅容器内可达。新增 Python
（pytest/unittest）、Node、Go 适配与 `TestReceipt`/`BuildReceipt`，退出 0 但
无测试、全跳过或无法解析均不判通过。依赖只走 H04 依赖端口，当前一律返回
可操作等待状态。

H06 独立复验（`fd5b57d`）判 FAIL 后按根因修复（实现见 `526423c` 之后的本地
提交）：

- **F1 重复挂载**：读根 + 窄写 + 已存在 `.env` 时，旧实现“展开只读祖先 + 文件
  掩码”对 `/workspace/.env` 挂载两次，`docker create` 报
  `Duplicate mount point`。现在 scope 只 bind 一次（读根只读 + 写子集可写），
  保护路径单独掩码，target 不重复。
- **F2 宿主占位**：可写根下缺失的保护路径，tmpfs 目标由 `docker start` 在宿主
  bind 内创建为 root:root 0755。现在只读 scope 下缺失路径不掩码（只读父本身
  阻止创建）；可写 scope 下由 broker 以工作区用户身份预建 `0700` 空目录作为
  挂载目标，随容器删除清理，Docker 不再改写宿主。
- **文件掩码**：`/dev/null` 只读 bind 仍接受写入，改为 broker 自建 mode 000
  空普通文件，读写均 EACCES。
- **契约内非阻断项**：超 `max_output_bytes` 现在显式 `truncated`/
  `log_truncated`（`dropped_bytes=0` 不再等同完整）；租约撤销/过期/换 epoch
  会终止在跑命令（`revoked`、退出 137）；`run_tests` 先探测 pytest/node，
  缺失返回 `waiting_approval` 而非 `unverified`。
- **第二次复验 F3**：日志截断只报布尔、`total_bytes` 由
  `dropped_bytes + 保留长度` 推算，调用方无法区分削尾与本来就这么短。现在
  `_Record.total_bytes` 保存 broker 返回的真实总量，`LogPage.total_bytes` 与
  `CommandHandle.log_bytes` 均用它；削尾时 `total_bytes > len(保留)`，
  未削尾时两者相等；`eof` 表示保留字节已交付完。新增
  `test_exact_length_log_is_not_reported_as_truncated` 区分两种形态。

新增 9 项结构用例（含 `.env`/`.git`/`.mgar` 的存在与缺失两种形态，逐项比较
宿主清单），H06 共 25 项；H04+H05 41 项、H00–H05 124 项回归、ruff、mypy
201 files 通过；`make verify TASK=HV4-H06` PASS。契约见
`docs/harness-v4/COMMAND_EXECUTION.md`；验收记录
`docs/harness-v4/accept/HV4-H06/accept.json`。状态 `PENDING_RECHECK`，
等待独立复验，不启动 H07。

<a id="hv4-h07"></a>

## HV4-H07：统一编码循环与多种任务验收

> 当前：历史 `2114a87` 已 ACCEPTED / PASS_AFTER_INDEPENDENT_RECHECK。P-13/P-15 后补修复在 `b0ad258` 的第二轮证据中有 PASS，四个 GAP 继续保留；accept 原 FIXED_PENDING_RECHECK 字段作为历史快照保留，详见记录对齐摘要。

- **工作**：将调查、编辑、执行、验证和反馈放入同一任务循环；把计划、未满足的
  验收项与最新工作树绑定。新策略按 Token/费用/时间/执行资源推进；旧固定次数
  许可保持原上限。内部无进展/错误重试策略与用户界面分离。
- **任务类型**：bugfix 校验原失败复现与修复；feature 校验新需求及原行为；refactor
  校验指定行为不变和回归；run_app 校验进程/就绪及清理；inspect 保持证据报告模式。
  模型提出的验收清单由用户目标和受信命令约束，不能通过删测试、改目标变成完成。
- **防循环**：结合验收覆盖、失败签名、工作树变化和新证据识别进展，检测补丁往返、
  同错重跑、文字改写和空搜索。取得实质进展后重置相应保护，不把“累计修复三次”
  当成普通任务的默认结束条件。无法推进时保留检查点、未完成项和下一步所缺信息。
- **验收**：真实测试首次失败后能修复再测；预算内超过旧 3 次尝试仍可成功。新增
  功能与绿色基线重构可完成；已知 bugfix 不能绕过复现。核验失败、预算耗尽和未知
  调用均不生成已完成结果；最终结论绑定实际代码树和验证回执。
- **产物**：任务类型与 AcceptanceChecklist、统一运行图、可追溯反馈、结果契约。
- **主要位置**：`repository_analysis/agent.py`、`repair/repair_agent.py`、`workbench/orchestrator.py`。

落地结果：新增 `src/mgar_v3/harness/coding_loop.py`（`CodingLoop`、
`TaskPolicy`、`AcceptanceChecklist`、`AcceptanceProposal`、`TaskPlan`、
`CodingResult`、`RunGraph`、`LoopCheckpoint`）。仅组合 H01/H02 冻结端口与
H04 Broker / H05 工作区 / H06 命令适配，没有新增执行入口。计划把验收项与
`checkpoint_tree` 绑定，树不一致返回 `HARNESS_PLAN_STALE`；模型提案只能追加
behavior/regression/build 项，不能替换或降低既有 required 项。进展只看工作树
变化与新命令证据，补丁往返、同错重跑、文字改写与重复空搜索不重置保护；连续
两轮无进展 `blocked`，有进展时资源策略可超过旧的 3 次修复上限。删除测试文件、
测试函数/断言站点数下降、`tests_run` 下降均不能完成；断言站点按文件汇总裸
`assert` 与 `self.assert*`，删裸 assert 会被拦截，抽到裸 assert 辅助函数的
等价重构不误杀。`ApplicationPort` 只是 H06 容器生命周期的接缝，旧 agent
接线归 H08。H07 共 29 项测试；H04–H07 95 项、H00–H07 178 项回归、ruff、
mypy 202 files 通过；`make verify TASK=HV4-H07` PASS（29 tests）。契约见
`docs/harness-v4/CODING_LOOP.md`；验收记录
`docs/harness-v4/accept/HV4-H07/accept.json`。独立复验 PASS 后按 P-13/P-15
修复弱化检测漏检/误杀，现状 `ACCEPTED` + `post_recheck_fixes=FIXED_PENDING_RECHECK`，
等待复验方复核，不启动 H08。

<a id="hv4-h08"></a>

## HV4-H08：首页接通真实编码与变更交付

> 当前：历史 `fadea5d` 已 ACCEPTED / PASS_AFTER_INDEPENDENT_RECHECK，浏览器 checks/counts 均15。下方“待独立复验”及 accept 的旧 next_card_status 是历史交付快照。

> 状态（2026-11-11）：本卡实现完成，停在待独立复验。写路径（增量
> `replacements`、H05 错误码、输出字节预算）之外，已接通首页“编码任务”模式、
> `/api/v3/harness/*` 版本化 API、Diff/产物视图与迁移说明；沙箱统一走 H06
> `IsolatedExecutionBroker` + `CommandSession`。浏览器端到端已在一次性数据库
> 上真实跑通（登录 → 选项目 → 修复缺陷 → Diff/命令/补丁）。另做了一次受控
> 真实模型调用：34 KB 文件的单行修改，6 次模型调用 / 922 字节输出 / 17,182
> micros，测试从失败到通过，未触发 `PATCH_LIMIT`/`PATCH_TRUNCATED`。H12 的
> 付费模型验收门仍为 NOT_RUN。详见 `SUPER_AGENT_HANDOFF.md` §28。
>
> 独立复验（2026-11-11）判定 FAIL：F1 `changes[].before_sha256` 被二次哈希
> （已改为直接上报基线摘要，并补修改/新增/删除/未修改断言与只读变异复现）；
> F2 `task_id` 未校验（已限制为 `^h08-[0-9a-f]{12}$` 并补负例）。同时修正
> accept 中浏览器端到端计数为 15/15 并附逐项明细。修复后仍停在待复验。

- **工作**：首页选择项目及调查/编码目标，按 H04 授予工作区范围。接入 H07 的真实
  运行时，在同页展示文件变更、命令/测试、Diff、最终说明与可下载补丁；用版本化 API
  接通现有 Workbench/Handoff，保留原入口与只读会话。模型与 GitHub 凭据继续网页填写。
- **验收**：不访问 legacy 页面就能提交真实项目任务、修改并验证、检查完整 Diff。
  缺环境/许可通过设置或动作弹窗处理，输入保留。使用真实沙箱和受控模型的浏览器
  端到端测试通过；实际付费模型验收在 H12。普通代码编辑不要求发布 GitHub 的权限。
- **产物**：首页编码流程、兼容 API、Diff/产物视图、浏览器记录与迁移说明。
- **主要位置**：`api.py`、`web/agent.*`、Workbench 服务与 Harness 服务。

<a id="hv4-h09"></a>

## HV4-H09：完整事件流、运行中消息与交互

> 当前：历史 `7d8f0ab` 已 ACCEPTED / PASS_AFTER_INDEPENDENT_RECHECK；该版本聚合计数为124/207。哈希边界修复 `096300e` 已独立复验 PASS，零 Run ID 限制继续适用；勘误见记录对齐摘要。

- **工作**：扩展现有 SSE，增加 plan/tool/command/patch/test/approval/artifact 的事件，
  支持持久游标、日志分页和慢客户端；公开进度摘要与实际工具结果，保留内部推理边界。
  提供持久收件箱、ask_user、运行中补充指令、暂停/取消和安全点重新计划。
- **验收**：运行中输入不会被禁用或丢失；消息有 received/applied 状态，重复提交只
  应用一次。新指示在安全点生效，旧版本模型结果不能覆盖新目标。浏览器重连、换
  对话、会话刷新和重复事件无串流；取消 UI 立即确认接收，实际执行停止以 Runner/
  供应商回执为准，不承诺取消后已发生用量自动退款。
- **产物**：事件协议版本、持久 inbox、交互与中断 API、前端状态投影。
- **主要位置**：`api.py`、`repository_analysis/streaming.py`、Engine 事件与 Mailbox、网页。

<a id="hv4-h10"></a>

## HV4-H10：长任务计划、上下文与分页工具

> 当前：原 `5707785` 独立复验 FAIL 保留；`c140dad` 的 F01/F02 修复已获
> PASS_AFTER_INDEPENDENT_RECHECK，限于两项 P1 与列明回归。注册68项、聚合281项、
> 旧上下文15项及静态检查通过，原始复现和12项独立检查通过。
> [独立报告](accept/HV4-H10/repair-c140dad-independent-recheck-20261111T120756Z.json)
> 由 `9b52190` 提交；其他发现继续 OPEN。用户确认后已启动 H11。

- **工作**：复用现有 ContextBuilder/compactor，将用户目标、未完成项、关键决策、
  补丁 hash、测试失败、待收回命令及权限/预算纳入编码检查点。把 CAS 原文分页、
  按需重读、搜索缓存真正暴露为可调用工具；压缩不只留下模型无法再次读取的引用。
- **验收**：强制多次压缩后，模型仍能读取丢出窗口的源文件/日志并完成任务；权限、
  验收项、失败记录及工作树 hash 不丢失。小任务不因记忆流程强制增加模型调用。
  压缩与恢复使用同一上下文版本；摘要中的授权文字不能变成执行许可。
- **产物**：可持久 TaskPlan/工作摘要、分页工具、上下文占用与压缩损失指标。
- **主要位置**：`context/`、`memory/`、`repository_analysis/memory_loop.py` 与工具适配。

<a id="hv4-h11"></a>

## HV4-H11：工作区/进程/账本一致恢复

> 当前：ACCEPTED / PASS_AFTER_INDEPENDENT_RECHECK（仅 H11 恢复工程范围）。见 [独立报告](accept/HV4-H11/independent-recheck-20261111T134513Z.json)；16组自建探针及列明回归通过。实施交付时的历史状态：IMPLEMENTED / PENDING_INDEPENDENT_RECHECK。注册68项、聚合349项、旧上下文15项、既有Engine恢复9项与静态门通过。见 [本轮证据](accept/HV4-H11/accept.json)。起点 `9b52190`，H10 审核源码 `c140dad` 与标准源码指纹已核对。
> [实施任务卡](CODEX_CLI_H11_TASK.md) 与 [恢复协议](RECOVERY.md) 约束本轮范围。
> 用户已批准专用隔离 PostgreSQL 的停启及相关工程测试，并允许必要真实付费调用；
> 本轮使用受控模型响应，付费调用为0。实施时尚无独立 PASS；现已完成上述独立工程复验。其他 OPEN 项未关闭，live 三门 NOT_RUN。

- **工作**：统一恢复 manifest、编辑操作、命令句柄、运行事件和模型调用；明确哪些
  无副作用读取可重放，哪些操作必须对账。通过 epoch 与工作区租约阻止旧 Worker
  再编辑、执行或提交成功；独立 Runner 不成为业务状态写者。
- **验收**：在补丁应用前后、命令启动后、输出保存前、模型结算前后与外部回执丢失
  处注入故障；已知结果复用，未知调用保留预占，补丁不重复应用，子进程无泄漏。
  在隔离测试实例实际重启服务和 PostgreSQL，恢复代码树/产物并校验哈希。取消的
  执行器不能写回新任务；未通过的新树不能沿用旧的审批或测试结论。
- **产物**：故障矩阵、恢复命令、隔离 PG/进程故障日志和逐例状态证据。
- **主要位置**：`coordination/engine.py`、`recovery/`、Runner、CAS、执行与补丁账本。

H11 保持 Engine 为唯一业务写者、PostgreSQL 为任务/epoch/预算/事件的权威；恢复复用
同一个 Run 并取得新 epoch。已知模型/工具结果按原 operation ID 复用；未知已发送调用
保留预占且不能重发；编辑通过预先记录的前后图像对账。原日志继续绑定原生成租约，
不能为了恢复而绑定新租约。最终树、验证树和必需验收项仍受 H10 F01 约束。
真实 SIGKILL、新服务进程与专用 PG 停启属于本卡工程门；不读取 holdout，不启动 H12，
不推送或发布。其他报告的 OPEN 发现和原未跟踪记录见恢复协议，不能借本卡自动关闭。

<a id="hv4-h12"></a>

## HV4-H12：冻结任务集、真实 Agent 与持出集验收

2026-11-15追加授权：R01/R02及纠正后的[R03](CODEX_CLI_H12_R03_TASK.md)均已完成各自
本地工程门禁；R03注册464项、lint/mypy通过，零新增真实调用。见
[R03报告](H12_R03_TOOL_RECOVERY_20261115.md)。本轮停在R03，R04–R08/H13未启动；
原H12质量FAIL及MVP未放行保留。以下为R00当时的授权及交付记录。

2026-11-15仅批准[HV4-H12-R00](CODEX_CLI_H12_R00_TASK.md)：基线冻结、历史证据归档、
零模型诊断夹具。R00完成后停止；R01–R08均未授权实施，不启动新付费实验，不改原分。
R00本地验收已PASS：40项、lint/mypy通过，原输入字节与账本不变，来源差异如实保留。
见[报告](H12_R00_BASELINE_FREEZE_20261115.md)；本轮已停止，H12/MVP仍FAIL。

2026-11-15追加首样本补测已完成：原NOT_RUN三题在新冻结候选`0655b59`得到
2PASS/1FAIL，实际覆盖为主轮21题+补测3题=24/24；不能拼成同候选通过率。Requests
公开611项通过，私有Judge却要求未公开的helper名称，原FAIL保留，基准命名有效性
仍待另行修订。累计813次/保守US$4.211490，未知/预占0；30份新回执均enabled/max，
请求峰值2。后置指标修复253项、lint/mypy通过，无新采样。见
[首样本补测任务卡](CODEX_CLI_H12_FIRST_MODEL_RECOVERY_TASK.md)；H12质量仍FAIL。

2026-11-15新冻结完整诊断：24个入口均尝试，真实模型进入21题；开发6PASS/6FAIL，
持出6PASS/3FAIL/3NOT_RUN，质量FAIL、MVP未放行。199次新增请求均enabled/max，
累计783次/保守US$3.988681，未知/预占0；后17题最多2题并发，实际请求峰值2。
详见[24题完整记录与根因](H12_FULL_MATRIX_THINKING_20261115.md)。3个前置阻塞须
新候选另冻结补测，原NOT_RUN保留；本批持出不再用于后续独立放行。工程112项
通过与零模型24/24就绪不替代真实分数；H13未启动。

2026-11-13更新：用户同意新建基准修订、保留原历史。同一候选源码的v2开发草案12/12
就绪，历史持出目录修订复核12/12就绪，尚非正式冻结或真实模型通过；新独立持出仍待
完成。[执行与后续队列](H12_REVISION_AND_FOLLOWUPS.md)。上下文/UI修复已部署，见
[修复报告](CONTEXT_AND_UI_REPAIR_20261112.md)。

追加计划：[HV4-H12-ENV 原 24 题环境阻塞修复任务书](CODEX_CLI_H12_ENV_UNBLOCK_TASK.md)。
编制时10/24具备执行条件，随后已实施修复；原v4同源预检为21/24（开发11、原持出10）。
追加卡整体仍INCOMPLETE：原冻结基线/资产及公开时间断言三项阻塞保留；工程聚合758项通过，新增真实模型调用0。
具体交付和边界见[环境修复报告](H12_ENV_REPAIR_20261112.md)，原H12质量门未放行。

- **工作**：执行 H00 冻结的开发任务和独立持出集，覆盖 Bug、新功能、重构、环境、
  大文件、多文件和多轮反馈。工程/安全门与真实模型/Agent/外部写门分别记录。复用
  现有评测 Harness，扩展其任务类型和语言，不以固定样例的 ScriptedProvider 代替能力。
- **对照**：先比较旧基线和新运行时；条件具备时用同一 DeepSeek 模型对照 MAGR
  与官方 DSH。Codex/Claude Code 若使用不同模型，只标为整产品对比；不能把模型差异
  归因于 Harness。没有可用授权/访问条件时明确 NOT_RUN。
- **验收**：按 [验收方案](ACCEPTANCE.md) 的预先冻结阈值判定；失败、重跑、费用、
  测试绕过与未知回执全部保留。至少有真实模型驱动的编辑和验证记录才能声明真实
  编码闭环通过；小样本结果不外推为与 Codex/Claude Code 持平。
- **产物**：版本化任务集、评分脚本、逐例证据、结果表和能力边界说明。

<a id="hv4-h13"></a>

## HV4-H13：Headless、CLI 与 SDK 契约

2026-11-18，后续复验通过 42 项 H13 检查和 111 项 Web／仓库分析回归。修正后台调度归属、旧提交回查、末尾事件和重启后的暂停状态。本机 18080 已运行验收源码，6 个既有会话均通过 API、CLI 和 SDK 的导出核对。[集成验收](accept/HV4-H13/integration.json)

2026-11-18，H13 在本地分支 `hv4-h13` 通过 38 项工程验收，涵盖真实 PostgreSQL、Docker 和 HTTP。首次联调发现数据库未呈现 Worker 的暂停状态，修复后验证了同一 Run 的暂停、恢复与取消。[CLI 与 SDK 契约](HEADLESS.md) · [机器验收](accept/HV4-H13/accept.json)

- **工作**：提供与 Web 同源的会话、提交、中断、恢复、事件订阅和产物 API；CLI 支持
  本机项目/headless JSON 输入输出，SDK 复用这些协议。终端交互和 PTY 根据 H06 的
  进程能力扩展，IDE/ACP 适配保持为客户端，不另建 Agent 状态机。
- **验收**：同一任务在 Web/CLI/SDK 看到相同 tree、状态和费用；错误退出码可区分
  未完成、缺依赖和失败，认证/断线/取消按同一契约工作。文档示例可在干净环境运行。
- **产物**：版本化 API、最小 SDK、headless 命令、兼容策略与会话导出格式。

<a id="hv4-h14"></a>

## HV4-H14：项目指令、Skills、MCP 与插件接口

2026-11-18，H14 本地工程验收 181 项、H13 同源码回归 42 项全部通过。已交付
项目规则首写门、固定版本回放、Skills、扩展 SDK、GitHub／知识／数据 MCP。
实站验收记为 `NOT_RUN`。[验收报告](H14_ACCEPTANCE_20261118.md) ·
[操作者使用说明](PROJECT_GUIDANCE_AND_PLUGINS.md) · [原开发交接](H14_HANDOFF.md)

- **工作**：明确系统/操作者策略、用户要求、项目 AGENTS.md、子目录指令与 Skill 的
  加载规则。增加带版本、能力声明和生命周期的插件注册；已有 GitHub/知识/数据 MCP
  适配进统一入口。优先内建或固定版本扩展，第三方执行隔离与插件安装由操作者管理。
- **验收**：插件可新增工具/模型适配而不修改主循环；卸载不会遗留工具与进程。
  项目指令只提供任务上下文，不授予外部写或读取凭据的能力。恶意结果、冲突工具名、
  依赖版本变化和同名覆盖不突破权限；MCP 远程操作仍按副作用类型走正确账本。
- **产物**：扩展 SDK、项目指令规范、MCP 适配、插件锁定与兼容示例。

<a id="hv4-h15"></a>

## HV4-H15：远程 Runner 配对与本机执行

2026-11-18，设备配对与固定项目测试的工程验收已通过。19 项覆盖真实 PostgreSQL、
HTTPS、Docker、重复领取、失联恢复与撤销。Python、Node 和 Go 样例取得真实测试回执。
完整源码 `e9123c9`，迁移 `0056` 已在旧数据库上前向验证。当前远程入口为固定测试，
远程自然语言改码与跨主机部署试跑待完成。[使用说明](RUNNER.md) · [验收记录](accept/HV4-H15/accept.json)

- **工作**：将执行环境作为 Workspace/Process Provider；本机 Runner 主动连接服务，
  以设备身份和短期作用域票据领取任务。网页显示任务实际在哪台机器运行；GitHub
  URL 可以在服务器隔离环境执行，本地目录由所属机器的 Runner 执行。
- **验收**：服务器无法借路径字符串读取用户磁盘；配对、失联、重连、撤销和重复
  领取均有记录。过期 epoch 不能继续执行；不同设备的目录/凭据不混用；不开放无
  认证宿主 shell 或把 Docker socket 交给项目进程。
- **产物**：设备配对、任务传输、远程执行能力端口、离线/恢复操作说明。

<a id="hv4-h16"></a>

## HV4-H16：多用户项目隔离与服务端调度

2026-11-18，H16 本地工程验收 13 项全通过。同源码 H13、H14、H15 均通过回归。
项目成员、工作区、凭据、账本、事件和日志已接入持久调度。生产多机与实站验收记为
`NOT_RUN`。[验收报告](H16_ACCEPTANCE_20261118.md) · [使用与部署](MULTI_USER.md)

- **工作**：在现有单管理员产品之外新增用户/项目 ACL、工作区归属、项目凭据与
  预算、事件/产物隔离。API 前台、控制器与 Runner 分离；先保持一个有效 Engine
  写者，再以有界队列和可重建 Worker 扩展执行并发。
- **验收**：跨用户/项目的 run、workspace、CAS、日志、事件和权限请求均不能越权；
  撤销成员即失去相关访问，独立资源/费用配额生效。两个控制器竞争只允许合法所有者
  工作；负载测试给出排队与恢复能力，不能仅增加 uvicorn workers 并宣称高可用。
- **产物**：多用户迁移、ACL、项目配额、调度/部署拓扑、隔离和负载报告。

<a id="hv4-h17"></a>

## HV4-H17：按需子 Agent 与效果验证（可选）

- **工作**：在主 Agent 质量基线稳定后复用 Experts；子任务拥有收窄的工具/工作区
  能力，费用和 Token 共享总预算，结果是建议或候选补丁，由主流程验证与收敛。
  同文件写冲突串行处理；父任务取消/过期必须传播。
- **验收**：先冻结是否值得启用的准则，再做配对实验，报告成功、费用、墙钟与冲突。
  未证明收益时保持默认单 Agent，不因数量更多而升级默认配置。越权、循环委派、
  超额计费和无主进程测试通过。
- **产物**：委派契约、受限调度、对照报告与默认配置决策。

- **本地交付**：只读仓库专家的工程门通过。受控配对两臂各通过 2/2，默认采用单 Agent。真实模型效果门为 NOT_RUN。[验收与试用](H17_ACCEPTANCE_20261118.md) · [机器验收](accept/HV4-H17/accept.json)

<a id="hv4-h18"></a>

## HV4-H18：可安装发布、诊断与运维指标

- **工作**：提供可复现安装/升级、schema 兼容、项目准备、doctor 与恢复导出；记录
  provider/tool/command/verification 的耗时、费用、排队、断线和失败类型。日志按
  request/run/operation 关联并脱敏，产物有保留/清理策略。
- **验收**：干净 Linux/WSL 安装及旧版本升级成功；配置缺失、PG 口令不一致、沙箱
  不可用和网络拒绝给出准确诊断。备份恢复保留账户、加密密钥、DB 与 CAS 对应关系。
  服务重启、断线和回滚演练可重现；公开文档分开列出已实测平台与未实测平台。
- **产物**：发布包/锁定依赖、升级与故障指南、指标面板/导出、支持矩阵及发行说明。

- **本地交付**：WSL2 安装、Debian 容器安装、旧版本升级、备份恢复及指标导出通过。生产与真实模型为 NOT_RUN。[H18 验收](H18_ACCEPTANCE_20261118.md) · [运维指南](H18_OPERATIONS.md)

- **独立验收（2026-11-19）**。H18 独立工程门 **FAIL**。注册测试和 WSL2、Debian 12 容器安装通过。恢复失败会遗留运行目录，阻塞同目标重试。[报告](H18_INDEPENDENT_ACCEPTANCE_20261119.md) · [机器记录](accept/HV4-H18/independent-20261119.json)

- **恢复重试定向复测（2026-11-19）**。旧版失败已复现，修正版 1/1。恢复工具后原样重试成功，原 Cookie 有效。[定向报告](H18_RESTORE_RETRY_20261119.md) · [机器记录](accept/HV4-H18/restore-retry-20261119.json)

- **修复后独立复验（2026-11-19）**。H18 原恢复重试故障已定向复验通过。独立工程门仍为 **FAIL**，发布途中父目录失去写权限会遗留空目录，阻塞同目标重试。[报告](H18_RESTORE_RETRY_INDEPENDENT_20261119.md) · [机器记录](accept/HV4-H18/restore-retry-independent-20261119.json)

- **发布权限中断定向复测（2026-11-19）**。修正版 1/1。目录发布失败保留数据库导入收据，恢复权限后原命令成功发布，省去再次导入。[定向报告](H18_PUBLISH_RETRY_20261119.md) · [机器记录](accept/HV4-H18/publish-retry-20261119.json)

- **独立关闭（2026-11-19）**。H18 本地独立工程门 **PASS**。发布权限中断后可原样重试，CAS 内容一致，已有目录受到保护。[报告](H18_PUBLISH_RETRY_INDEPENDENT_20261119.md) · [机器记录](accept/HV4-H18/publish-retry-independent-20261119.json)
