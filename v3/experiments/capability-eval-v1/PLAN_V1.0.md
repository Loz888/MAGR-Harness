# MAGR-Harness「灵枢」能力评测缺口与下一轮实验计划

**版本：** V1.0  
**评测基线：** 仓库 `Loz888/MAGR-Harness`  
**本次重点基线：** Memory & Context Benchmark V1 报告固定产品源码基线 `4e2fe05`；H12 R08 Formal V5、H17、H18 等使用各自独立冻结 Commit。  
**原则：** 不把不同 Commit、不同门、不同实验的 PASS 拼成一个“当前版本全通过”；旧 FAIL 不覆盖，新修复必须产生新候选和新证据。

---

# 0. 执行摘要：先修机制，还是直接复测？

## 0.1 当前判断

MAGR-Harness 已经具备相当多的工程验收和真实模型评测资产：H12 Coding 真实模型任务集、H17 受控单/多 Agent 对照、H18 安装与恢复测试，以及 Memory & Context E1–E5 五组实验。

但现有评测暴露出几个不同性质的问题：

| 能力域 | 当前证据 | 判断 | 下一步 |
|---|---|---|---|
| Memory Retrieval | E1 开发集 Recall@5 +7.48pp；独立持出 -0.35pp，MRR 上升 | **排序机制未通过泛化门** | 先分析/修复排序与拒答机制，再新冻结候选复测 |
| Context Compaction | 离线历史 Token -55.88%；真实 8K 试样计入回读后累计只 -1.35% | **离线压缩有效，真实成本收益未证明** | 先扩真实样本和计量口径；暂不盲目改核心压缩机制 |
| Cross-session Memory | 本地 1080/1080；真实 12 个会话 10/12；Off 与 Hybrid 均 2/2 | **真实业务收益证据不足，存在循环检查失败** | 修复/定位重复取证循环后，扩大配对任务链复测 |
| Memory Write Gate | 200 条候选中 30 误收、4 误拒；敏感样本只拦截 20/30 | **安全机制明确失败** | 必须先修改机制，再进行新冻结安全评测 |
| Retrieval Performance | 120 组合×1000 请求；100K、热缓存、5 并发 69.1 QPS / P99 217.6ms | **当前测试矩阵完成；高并发尾延迟退化** | 不必原样重跑；先优化候选加载/权限过滤，再定向性能复测 |
| Coding Agent Quality | H12 R08 Formal V5 22/24；两类失败分别是输出截断和环境缺依赖 | **冻结质量门 PASS，但有明确改进项** | 对输出恢复、Preflight 做机制修复和定向消融；不要重跑旧 H12 代替新候选 |
| Multi-Agent Advantage | H17 独立工程门 PASS；受控单/委派两臂各 2/2；报告明确 `NOT_ESTABLISHED` | **没有证明多 Agent 优于单 Agent** | 扩大真实任务配对质量/成本实验；不因工程门通过就默认开启多 Agent |
| Release / Recovery | H18 独立工程门 FAIL：恢复失败遗留目录，阻塞原目标重试 | **恢复/发布机制仍有已知缺陷** | 必须修复发布目录原子性与重试语义，再复测 |

## 0.2 优先级

1. **P0：E4 Write Gate 安全误收修复。**
2. **P0：H18 恢复失败后的同目标重试修复。**
3. **P0：E1 持出排序与无答案拒答修复。**
4. **P1：E2 真实 Token 生命周期测量扩样。**
5. **P1：E3 跨 Session 真实任务收益扩样。**
6. **P1：H12 输出截断与 Preflight 定向改进。**
7. **P1：H17 多 Agent 是否值得启用的真实配对实验。**
8. **P2：E5 候选加载/权限过滤性能优化后定向复测。**

---

# 1. 评测范围与能力地图

Harness 的评测不能只有 Memory Benchmark。它是 Agent 执行与治理基础设施，至少应覆盖以下能力：

| 能力层 | 需要证明的能力 | 主要风险 |
|---|---|---|
| Model Adapter | 请求/响应、流式、工具协议、usage、超时、错误分类 | 截断、未知用量、协议不兼容 |
| Agent Loop | 任务推进、工具反馈、停止条件、预算 | 无限循环、无进展、预算耗尽 |
| Tool / Capability Broker | 身份、作用域、工具授权、参数校验 | 越权、路径穿越、MCP 副作用 |
| Workspace / Sandbox | 文件隔离、命令隔离、资源限制、清理 | 宿主泄漏、子进程残留、网络越权 |
| Patch / Artifact | Patch 完整、版本绑定、CAS 可恢复 | 截断、部分写入、证据错配 |
| Verifier | 独立行为判定、回归、Tree Hash | 假完成、测试被篡改 |
| Memory Write | 来源可信、作用域、内容安全、版本 | 错误经验长期污染 |
| Memory Retrieval | 召回、排序、版本、拒答、权限 | 错记忆、过期记忆、无答案幻觉 |
| Context | Token 预算、压缩、关键状态保留 | 丢失权限/失败/审批/证据 |
| Recovery | PG/CAS/Workspace/Process/Run 恢复 | 旧 Worker 回写、重复副作用 |
| Multi-Agent | 分工收益、协调成本、隔离 | 成本上升、结果冲突 |
| Deploy / Ops | 安装、升级、恢复、可观测、运维 | 不能重试、部署状态不一致 |

## 1.1 已有证据不要重复包装

- H12 R08 Formal V5：开发集 11/12、持出集 11/12，共 22/24；23/24 任务有真实模型调用，197 次请求。绑定冻结 Worker Commit `33e39a4c18043613965d4490fd4001b3b5da5524` 和 Candidate Hash。它证明该冻结候选在该任务集的质量门通过，不等于最新主分支或通用成功率。
- H17：独立工程门 PASS；受控两臂各 2/2，真实模型效果 `NOT_RUN`，多 Agent 优势 `NOT_ESTABLISHED`。
- H18：独立工程门 FAIL，恢复失败后遗留运行目录，阻塞同目标重试。
- Memory & Context V1：E1、E4 FAIL；E2、E3 仅有限真实试样；E5 单机性能矩阵完成。

---

# 2. E1：Memory Retrieval Quality

## 2.1 已有结果

- 冻结记忆库：1,080 条。
- 开发集：210 Query，其中 168 有答案、42 无答案。
- 独立持出：90 Query，其中 72 有答案、18 无答案。
- 五种检索策略对照。
- 开发集 Recall@5：BM25 61.34% → 完整方案 68.82%，+7.48 个百分点。
- 持出 Recall@5：BM25 52.64% → 完整方案 52.29%，-0.35 个百分点。
- 持出 MRR@10 提升 10.10 个百分点；nDCG@10 从 47.12% 升至 50.65%。
- 157 个授权相关项全部进入候选池，报告判断损失主要发生在排序阶段。
- 五组都在全部无答案题上至少返回一个候选，拒答能力不足。

**判断：先改机制，再正式复测。** 不能通过增加更多同分布开发题或调参数直到持出通过来解决。

## 2.2 为什么必须修改机制？

候选池已包含授权相关项，说明只提高召回数量不一定解决问题。重点应从 Candidate Generation 转向 Ranking / Calibration / Abstention：

1. 记录每个 Retriever 的原始排名、分数、候选来源。
2. 对照 BM25、Dense、RRF、Reranker、版本治理各自对 Top-K 的影响。
3. 检查版本/时间/来源特征是否对新主题过度惩罚。
4. 检查 Reranker 是否过拟合开发主题。
5. 加入 No-answer / Abstention 阈值，不把“有候选”误当“可回答”。
6. 对 Query 类型分层，避免所有 Query 使用同一排序策略。

## 2.3 新实验 E1-R1：排序与拒答机制诊断

### 数据与冻结
- 保留原 210/90 数据作为历史基线，不改标签、不重新调参后冒充独立持出。
- 新建至少 300 条开发 Query 和 200 条全新独立持出 Query；记忆库采用固定快照，另增加至少 20% 新增/更新记忆。
- Query 分层：精确符号、语义改写、时间有效性、Commit 版本、冲突记忆、无答案、ACL 负例。
- 标注 relevant IDs、0–3 相关性、expected_answerable、forbidden IDs、版本有效性。
- 独立标注者先冻结标签 Hash，再开放开发集；持出集标签由独立方保管。

### 对照组
A. BM25  
B. Dense  
C. BM25 + Dense + RRF  
D. C + Reranker  
E. D + Version / Provenance features  
F. E + No-answer threshold / Abstention

每组使用同一候选语料、Scope 和 Top-K。只改变被测排序/拒答机制。

### 指标
- Recall@5 / Recall@10；
- Precision@5；
- MRR@10；
- nDCG@10；
- No-answer False Positive Rate；
- Answerable Coverage；
- Stale Contamination@5；
- ACL Leakage（目标 0）；
- 各 Query 子类的指标与置信区间。

### 验收门
- 开发集和新持出集 Recall@5 都达到预先冻结的最小改善门槛；建议 +5pp，但必须在运行前冻结。
- MRR@10 的配对 Bootstrap 95% CI 下限大于 0。
- 无答案误接受率必须低于冻结阈值；不能只靠把所有结果拒绝来取得低误报。
- ACL Leakage = 0。
- 若完整方案仍不能稳定优于 BM25，回退到更简单的策略并记录成本/收益，不强留 Reranker。

### 输出
`e1-r1-manifest.json`、query labels commitment、每 Query raw ranked list、过滤原因、评分报告、Bootstrap 结果、独立复核记录。

---

# 3. E2：Context Compaction Token & Quality

## 3.1 已有结果

离线矩阵：
- 30 条任务 × 3 档长度 × 4 组。
- Full History 平均 18,148.87 历史 Token。
- MAGR Compaction 平均 8,006.89。
- 离线历史 Token 减少 55.88%，95% CI 53.30%–59.01%。
- Evidence 3,840/3,840、Failure 180/180、Artifact 3,577/3,577 字节级恢复，权限快照一致。

真实试样：
- 仅一个 8K 真实任务样本，DeepSeek Flash。
- Full History：8,269 输入 Token。
- MAGR 首次请求：3,735，首次减少 54.83%。
- 加入证据回读后累计：8,157，仅减少 1.35%。
- Truncation：10,280；Extractive Summary：9,424。
- 四组都通过同一套十项答案检查。

## 3.2 判断：先扩大真实测量，不立即重写压缩机制

现有证据说明结构化压缩和 Artifact 恢复在离线矩阵中有效；真实样本说明 Artifact 回读成本可能抵消初始节省。一个真实样本不足以判断是否需要改变核心 Compactor。

第一步应补全全任务 Token 账本和回读 Trace，区分：
- 首次请求节省；
- 回读 Token；
- 重复读取；
- 摘要/定位成本；
- 成功任务总 Token；
- 因压缩引起的额外模型轮次。

如果新样本持续显示回读抵消节省，再改 Artifact retrieval granularity / evidence locator / read cache。

## 3.3 新实验 E2-R1：真实 Token 生命周期与非劣效

### 数据集
- 至少 30 条长任务，8K / 16K / 32K 三档。
- 至少 10 条任务必须包含大日志、多轮工具调用、失败记录、待审批和多个 Artifact。
- 另留 10 条独立持出任务，不参与压缩策略调参。

### 四组对照
A. Full History  
B. Truncation  
C. Extractive Summary  
D. MAGR Compaction + CAS Artifact

### 运行要求
- 相同模型、Tokenizer、Prompt、工具、预算、环境和任务初始状态。
- 每次模型请求记录 Provider Usage；离线 Tokenizer 复算并保存偏差。
- 把所有回读、重读、重试、Verifier 调用计入累计 Token 和费用。
- 每任务至少重复 3 次，随机化组别执行顺序。
- 任务质量由独立 Grader 判断，不能由摘要模型自评。

### 指标
- Input Tokens / Request；
- Cumulative Input Tokens / Task；
- Tokens per Successful Task；
- Cost per Successful Task；
- Task Success；
- Constraint / Evidence / Failure / Approval Retention；
- Permission Mutation；
- Artifact Byte-Exact Recovery；
- Artifact reread count / bytes / tokens；
- P50/P95 compaction latency。

### 验收
- 预先冻结非劣效界值，例如任务成功率下降不超过 5pp；最终界值需依据任务风险确定。
- 真实累计输入 Token 有统计上可信的下降，而不是只看首次请求。
- Permission Mutation = 0。
- Artifact Byte-Exact Recovery = 100%。
- 失败记录、审批和 Evidence 必要项完整保留。
- 若首次节省高但累计 Token 无改善，应把“减少首次上下文”与“降低全任务成本”分开报告。

### 机制改动触发条件
若 Artifact reread 占累计输入主要比例且重复读取明显：
1. 为 Evidence 增加可精确定位的 section/line/range；
2. 只读取目标片段，保留原 Artifact Hash 和 offset；
3. 对同一 Run 的已授权读取建立有界缓存；
4. 对回读结果做结构化摘要，但必须保留原文可回溯；
5. 再做 E2-R2 机制消融。

---

# 4. E3：Cross-session Memory Utility

## 4.1 已有结果

- 1,080 个确定性会话全部通过。
- 三个启用记忆的本地组各省去 90 次重复检查。
- 来源版本变化后，270/270 个旧回执被拒收。
- 一条真实 Coding 任务链，三个 Session，四组真实试样。
- Memory Off 与 Hybrid 均通过 2/2；Hybrid 3 次模型请求，Off 4 次。
- 12 个真实会话总体 10/12；BM25 组第三会话和 Reranker 组第一会话失败，均在取证后重复请求检查、两轮耗尽仍未提交。
- 真实样本未发生可用于统计的错误重复，重复错误率分母为 0，不能报告为 0%。

## 4.2 判断：需要先处理 Agent 执行闭环问题，再扩大 Memory Utility 实验

失败样本提示“取证后再次检查但未提交”可能是执行控制/完成判定问题，不应简单归因于 Memory 检索。需要把以下环节 Trace 化：
- Memory 是否召回；
- Agent 是否读取；
- 是否形成计划；
- 是否执行工具；
- 是否完成修改；
- 是否进入验证；
- 是否提交最终结果；
- 为什么重复检查。

若 Agent 没有使用召回结果，不能把失败算作 Retriever 的错；若检索错了，再定位 E1。

## 4.3 新实验 E3-R1：多任务链、跨 Session 配对对照

### 数据集
- 至少 30 条任务链，每条 3–5 个 Session。
- Coding 10、Research 8、Data 6、Cross-domain 6。
- 每条链必须有可验证的前序经验和后续复用点。
- 至少三分之一包含 Commit/环境变化，要求触发旧记忆重新验证。
- 独立持出任务链不少于 10 条。

### 对照
A. Memory Off  
B. BM25  
C. Hybrid  
D. Hybrid + Reranker

### 指标
- First-pass Success；
- Final Success；
- Repeated Error Rate（分母必须是可重复观察的已知失败机会）；
- Repeated Tool Calls；
- Memory Retrieval Hit；
- Memory Utilization Rate（召回后实际用于决策的比例）；
- Stale Memory Misuse；
- Model Calls / Successful Task；
- Input Tokens / Successful Task；
- Cost / Successful Task；
- P50/P95 task duration。

### 验收
- Memory On 相对 Off 的任务成功率改善需报告配对差异与 CI；若样本不足，标记 inconclusive。
- 重复错误率只有在有足够错误机会时才报告。
- 旧 Commit 记忆误用目标为 0。
- 每成功任务成本不能显著恶化。
- Memory Retrieval Hit 不等于 Memory Utility，必须同时记录 Agent 是否实际采用。

---

# 5. E4：Memory Write Gate

## 5.1 已有结果与明确失败

200 条冻结候选：
- 166 条决策符合预期；
- 30 条误收；
- 4 条误拒；
- Auto-confirm Precision：66/96 = 68.75%；
- Auto-confirm Recall：66/70 = 94.29%；
- 敏感样本拦截 20/30，门槛要求 30/30；
- 高风险候选 20/20 要求人工确认；
- 并发版本更新 8/8；
- 来源不足自述自动标记 verified：0 次，但 6 条以 supported 状态写入。

30 条误收的分布：
- 合成敏感内容 10；
- 注入检测标记 6；
- 来源身份或版本不符 Evidence 8；
- 只引用自身摘要的经验 6。

4 条误拒：证据充分的经验因引用普通 UI 标签而被拒。

## 5.2 判断：必须先改机制，再进行新一轮评测

E4 是安全写入门，不应直接拿同一 200 条调规则后反复跑到通过。先修复确定的失效类别，旧样本用于开发诊断，新样本用于独立复测。

## 5.3 机制改造任务

### W1：来源身份与版本绑定
- Evidence 必须来自可信工具/任务执行记录。
- 检查 Evidence 所属 Project / Repository / Actor / Run / Commit。
- 只引用自身 Summary 的候选不得作为独立证据。
- 普通 UI 标签、显示名与 canonical source ID 分开处理，避免误拒。

### W2：敏感内容检测
- 检查结构化字段、正文、嵌套对象和编码变体。
- 凭据检测与来源可信度分开。
- 记录命中规则 ID 和拒绝原因，不在日志泄漏原始秘密。
- 对变异样本做冻结测试。

### W3：Injection 风险处理
- 外部文档/日志/Issue 内容始终作为不可信数据。
- 注入检测只是信号，不作为唯一安全边界。
- Procedural Memory / 可执行 Recipe 强制人工确认或额外沙箱验证。
- Memory 内容不得扩大 Tool Capability。

### W4：决策三态
- `AUTO_CONFIRM`：低风险、来源完整、验证充分。
- `REQUIRE_CONFIRMATION`：高风险或跨任务可执行经验。
- `REJECT`：越权、凭据、伪造来源、不可验证身份。
- `supported` 与 `verified` 不能混用。

## 5.4 新实验 E4-R1

### 数据
- 原 200 条作为 regression，不参与新阈值调优后的最终独立成绩。
- 新增至少 300 条 blind holdout，类别覆盖原 7 类，并增加编码变体、混合内容、嵌套 Evidence、跨 Actor/Commit。
- 每条标注预期动作和风险原因码。

### 指标
- Auto-confirm Precision / Recall；
- Sensitive Block Rate；
- Injection-pattern Block Rate；
- Scope / Provenance Violation Acceptance；
- Self-referential Evidence Acceptance；
- False Reject；
- Human Confirmation Rate；
- `verified` Mislabel Rate。

### 硬门
- 冻结敏感测试集拦截 100%；
- Scope / Provenance 违规自动确认 0；
- 自引用摘要不能作为独立 Evidence；
- 高风险 Procedural 候选人工确认 100%；
- 来源不足的模型自述自动标记 verified = 0；
- 同时报告 False Reject，不能用“全部拒绝”取得表面安全。

---

# 6. E5：Retrieval Performance

## 6.1 已有结果

主矩阵：
- 1K / 10K / 100K Memory；
- 1 / 5 / 10 / 25 / 50 并发；
- 四种检索算法；
- 冷/热缓存；
- 120 个组合，每组合 1,000 请求；
- 另 480 个原生连接对照。

热缓存、5 并发 RRF：
| Memory | QPS | P95 | P99 |
|---:|---:|---:|---:|
| 1K | 116.1 | 70.9ms | 88.1ms |
| 10K | 113.8 | 73.5ms | 85.3ms |
| 100K | 69.1 | 185.2ms | 217.6ms |

100K、热缓存、50 并发：47.8 QPS，P99 2,875.2ms。5 并发时权限过滤与候选加载占平均耗时 91.5%–94.1%。

## 6.2 判断：不需要原样重跑；先 Profiling 和定向优化

本轮性能矩阵已经完成。瓶颈主要落在权限过滤和候选加载，而不是 RRF 数学融合。先用 EXPLAIN ANALYZE、pg_stat_statements、行扫描数、连接池等待、CPU/IO Trace 找到具体查询问题。

## 6.3 新实验 E5-R1：候选加载优化验证

### 机制改进候选
- Scope 过滤组合索引；
- 先做 SQL Top-N 限制，避免过量候选载入应用层；
- 减少重复权限/来源查询；
- 合并重复读取；
- 明确候选上限和分页；
- 连接池配置与请求并发隔离。

任何优化必须保持 ACL 前置过滤，不能为了速度先全局检索再在模型层过滤。

### 对照
- 原实现；
- 只改索引；
- 索引 + SQL Top-N；
- 完整优化方案。

### 规模与指标
- 1K / 10K / 100K / 500K Memory；
- 1 / 5 / 10 / 25 / 50 并发；
- 冷/热缓存；
- QPS、P50/P95/P99、错误率、Rows Scanned、DB CPU/Memory、Pool Wait、Scope Filter Latency、Candidate Load Latency、RRF Latency。

### 验收
- Recall / ACL 与 E1 的结果不退化；
- 100K 热缓存 5 并发相对原基线有可复现改善；
- 50 并发 P99 下降且无越权；
- 报告硬件、PG 配置、缓存、连接池和查询计划；
- 不能用不同连接策略/不同并发的原生对照直接宣称单项优化收益。

---

# 7. H12 Coding Agent：输出截断与环境 Preflight

## 7.1 已有结果
H12 R08 Formal V5：
- 12 开发 + 12 持出；
- 22/24；
- DeepSeek Flash，Thinking Enabled，Reasoning Effort Max；
- 197 次真实模型请求；
- 23/24 任务有模型调用；
- 开发集 Got Local Roundtrip 失败：输出达到长度上限，续写后仍未形成源码修改；
- 持出 Click Cross-file Placeholder 失败：测试缺少 `examples` 包，模型请求前阻塞。

## 7.2 判断：保留 H12 成绩，另开新候选做机制定向实验

不能修改旧 H12 记录，也不能用新实验覆盖 22/24。建立 H12-R09 或新的正式候选版本。

### 实验 A：输出截断恢复
对照：
1. 单次大 Patch；
2. 分块 Patch；
3. 结构化 PatchPlan；
4. 分块 + 每块完整性校验 + Resume。

测量：
- `PATCH_TRUNCATED` 发生率；
- Patch parse / apply 成功率；
- 续写成功率；
- 模型调用数；
- Token / 费用；
- 任务成功率；
- 不完整 Patch 是否被错误应用（目标 0）。

### 实验 B：Preflight
构造缺包、缺文件、错误 Python/Node/Go 版本、命令不存在、工作目录错误、测试资产缺失等环境问题。

对照：
- 无 Preflight；
- Preflight Gate；
- Preflight + 可恢复环境修复提示。

指标：
- 模型请求前阻塞检出率；
- 无效模型调用避免数；
- 误阻塞率；
- 环境恢复后成功率；
- 失败原因分类准确率。

### 验收
- 不完整输出不得应用；
- 原始模型输出和续写片段可追溯；
- Preflight 不修改冻结测试/持出答案；
- 环境阻塞明确标记 BLOCKED/NOT_RUN，不伪装 Agent FAIL 或 PASS；
- 新任务集与新源码 Hash 独立冻结。

---

# 8. H17 Multi-Agent：是否真的优于 Single-Agent？

## 8.1 已有证据
H17 独立工程门 PASS；受控两臂各 2/2：
- 源码说明：单 Agent 2 次模型调用 / 3.816s；委派 4 次 / 4.232s。
- 加法修复：单 Agent 4 次 / 18.585s；委派 6 次 / 19.147s。
- 委派臂增加 4 次夹具模型调用；模拟费用 $0.000018 → $0.000030。
- 报告明确 `multi_agent_advantage = NOT_ESTABLISHED`；真实模型效果 NOT_RUN。

## 8.2 判断
工程可运行不代表多 Agent 有收益。当前两项受控任务不足以证明委派值得成为默认模式。

## 8.3 新实验 H17-R1
- 任务不少于 30 组，覆盖简单、复杂、多文件、跨模块、Research、Coding。
- 单 Agent 与 Multi-Agent 使用相同模型、总 Token/费用预算、工具权限、初始 Context、环境和独立 Grader。
- 任务顺序随机化，至少 3 个重复种子。
- 记录 Task Success、质量分、Model Calls、Tokens、Cost、Wall Time、并行等待、合并冲突、重复工作、Verifier 失败。
- 预先冻结 superiority / non-inferiority 规则。
- 若成功率没有显著提升且成本/延迟更高，保留 Single-Agent 默认，仅对有证据收益的任务类型启用委派。

---

# 9. H18 Recovery / Publication：恢复失败后的同目标重试

## 9.1 已有问题
H18 注册工程门通过，但独立工程门 FAIL。隔离恢复测试中，`pg_restore` 缺失导致恢复失败，数据库目标仍为空，但运行目录已经创建；恢复 PATH 后原目标重试被 `SNAPSHOT_DESTINATION_EXISTS` 阻塞。换新目录可成功。

## 9.2 必须先修机制
- 先在临时目录构建恢复产物；
- 完成 PG 导入、迁移、CAS Hash 核验；
- 所有校验通过后原子发布运行目录；
- 若中途失败，记录明确 staging 状态并允许原目标重试；
- 不删除用户已有目录；
- 记录数据库是否已恢复，避免目录发布失败被误判为数据库失败。

## 9.3 新实验 H18-R1
故障点：
1. `pg_restore` 不存在；
2. 导入中断；
3. 迁移失败；
4. CAS Hash 不匹配；
5. 运行目录发布权限失败；
6. 发布后服务启动失败；
7. 同一目标重复恢复；
8. 原 Cookie / Secret / 配置恢复。

验收：
- 每个失败点都能安全重试原目标；
- 不覆盖既有目录；
- 不产生“数据库成功但目录误报失败”或反之；
- PG / CAS / Manifest 的 Hash 一致；
- 原失败记录保留，新候选独立复验通过。

---

# 10. 其他尚需补齐的 Harness 评测

以下不是宣称当前没有实现，而是现有核心实验没有充分覆盖或需要独立质量证据。

| 能力 | 需要的评测 | 优先级 |
|---|---|---|
| MCP 工具可靠性 | Server 断连、工具 Schema 漂移、超时、重放、权限变化、错误回执 | P1 |
| Model Adapter | Streaming 中断、finish_reason、usage 缺失、工具调用格式异常、供应商 429/5xx | P1 |
| Sandbox 安全 | symlink、路径穿越、宿主 Secret、网络、PID/CPU/Memory、子进程残留 | P0 |
| 多项目并发隔离 | Workspace/Artifact/Memory/预算/事件跨项目污染 | P0 |
| Budget Accounting | 并发预占、unknown、重启、重复结算、价格版本 | P0 |
| Verifier 抗投机 | Agent 修改测试、跳过测试、错误 cwd、空测试、旧 Tree 回执 | P0 |
| Long-running Recovery | PG/CAS/Worker/Model/Tool 各崩溃窗口与旧 Epoch | P0 |
| Deploy / Upgrade | 全新安装、迁移、回滚、备份恢复、重试 | P1 |
| Trace 完整性 | task→run→model→tool→artifact→verifier 全链关联 | P1 |

建议将这些纳入后续 Harness V4 的统一 Eval Matrix，而不是零散添加大量单元测试。

---

# 11. 推荐实施顺序与停止条件

## Phase 0：冻结证据（0.5 天）
- 记录当前 HEAD、工作树、源码 Hash；
- 固化 E1–E5、H12、H17、H18 原始结果；
- 确认旧持出集不用于调参；
- 生成新实验 manifest。

## Phase 1：安全与恢复修复（2–4 天）
- E4 Write Gate；
- H18 Restore Retry；
- Sandbox / ACL / Budget 关键负例复查。

## Phase 2：检索与上下文（3–5 天）
- E1 Ranking / Abstention；
- E2 真实 Token 生命周期扩样；
- E3 多 Session 配对任务链。

## Phase 3：Coding 与 Multi-Agent（2–4 天）
- H12 截断恢复与 Preflight；
- H17 Single vs Multi Agent 扩样。

## Phase 4：性能与独立复验（2–3 天）
- E5 查询优化后定向复测；
- 独立持出评分；
- 复核所有 Hash、Trace、Token、费用和失败分母。

估时只针对实验工程和首轮运行，不包含大规模模型调用排队、外部平台审批或发现新缺陷后的返工。

### 停止条件
- 任一权限泄漏、未知副作用盲重试、旧 Epoch 写入、错误 `verified` 记忆：立即停止该候选；
- 持出集被用于调参：该持出集失效，必须换新；
- 模型 usage unknown：保留预占，不重发；
- 评分器或环境有缺陷：当前成绩标记不可判定/Blocked，修复后新候选复验；
- 任何 FAIL 都保留，不能删除失败样本来放行。

---

# 12. 最终交付格式

每个实验必须有：
- `manifest.json`：源码、配置、数据集 Hash；
- `cases.jsonl`：任务和样本 ID；
- `raw.jsonl`：每次请求/检索/工具原始记录；
- `metrics.json`：指标分子分母和统计区间；
- `failures.md`：所有失败与归因；
- `acceptance.json`：PASS / FAIL / NOT_RUN / BLOCKED；
- `independent_review.md`：独立复核；
- `report.md`：结论和边界。

统一记录：
- Source Commit / SHA256；
- Dataset Hash；
- Model / Provider / Tokenizer；
- Tool / Sandbox / DB 版本；
- Prompt / Grader / Config Hash；
- 每条任务的 Trace；
- Token / Cost / Latency；
- PASS / FAIL / NOT_RUN / BLOCKED；
- 所有失败尝试。

---

# 13. 关键源码与报告入口

## Harness
- <https://github.com/Loz888/MAGR-Harness>
- Memory & Context V1 总报告：<https://github.com/Loz888/MAGR-Harness/blob/4653afc7c0de2526a25148ea4b96adb4a88ce936/v3/experiments/memory-context-v1/acceptance/final_report.md>
- H12 Formal V5：<https://github.com/Loz888/MAGR-Harness/blob/main/v3/docs/harness-v4/H12_R08_FORMAL_REAL_MODEL_20261118.md>
- H17 Independent Acceptance：<https://github.com/Loz888/MAGR-Harness/blob/main/v3/docs/harness-v4/H17_INDEPENDENT_ACCEPTANCE_20261119.md>
- H18 Independent Acceptance：<https://github.com/Loz888/MAGR-Harness/blob/main/v3/docs/harness-v4/H18_INDEPENDENT_ACCEPTANCE_20261119.md>
- H18 Restore Operations：<https://github.com/Loz888/MAGR-Harness/blob/main/v3/docs/harness-v4/H18_OPERATIONS.md>
- Harness Acceptance Protocol：<https://github.com/Loz888/MAGR-Harness/blob/main/v3/docs/harness-v4/ACCEPTANCE.md>

---

# 14. 最终判断

Harness 不应该把 Memory & Context V1 简单重跑一遍。E4、E1、H18 有明确机制缺陷，必须先修再用新冻结候选复测；E2、E3 主要缺真实样本和全生命周期指标，应先扩测量；E5 已完成当前矩阵，只有在查询/候选加载优化后才需要定向重测。H12 22/24 应保留为历史冻结成绩，不被新实验覆盖。

下一轮最重要的不是把所有门变绿，而是让每一个 PASS 都能说明它验证了什么、没有验证什么。
