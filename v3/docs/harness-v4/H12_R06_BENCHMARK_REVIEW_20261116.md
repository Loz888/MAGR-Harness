# H12-R06 Benchmark Review（2026-11-16）

结论：已完成 R06 所需的类型规则和审查机制，旧基准存在必须另版修订的问题。
本报告不认证原 24 题的私有 Judge 全部合格，也不把旧成绩重算为 PASS。实际新题集
的完整公开准备、逐断言人工复核和重新冻结留给后续卡；旧 H12 质量 FAIL 保留。

审查来源是 R00 冻结索引、已经发布的 2026-11-15 完整矩阵报告，以及其公开目标/
要求和历史状态。没有读取或创建新的独立持出答案，没有调用供应商模型。机器清单
[benchmark-review.json](protocol/r06/benchmark-review.json)逐题记录来源 hash、公开
要求的 R01 ID、类型规则和后续要求，避免把机制测试当作实际题目质量分数。


历史基准审查发现特性基线、重构边界和未声明内部名称等问题。

## 机械检查与人工审查的分工

机制负责拒绝：缺失/多余/重复的断言、未知公开 ID、行列或表达式改变、源码 hash
改变、未公开符号名、空清单、动态/外部判定器冒充盘点，以及执行命令改变或只运行
已审查测试的子集。冻结前、派发前和最终核验分别检查；原 PG 中没有合法结果引用
时，调用方自报 PASS 不能通过 Scorer。

基准负责人还须逐项确认断言的**实质语义**确实来自公开要求；AST 和 hash 不会
自动证明自然语言。旧 24 题目前未提供 R06 完整清单，因此本报告不会伪造“所有
历史断言已审查”结论。其他语言的私有 Judge、复杂结构数据流必须采用可审查的新
适配器；不支持时保持 BLOCKED。

真实工程闭环使用独立的公开合成项目，不含人工解题补丁或新持出答案。完整旧行为
通过和结构通过是两个必要条件：无用 helper 即使行为/Judge 绿仍 FAIL；合法 helper
改变 Unicode 旧行为也 FAIL。Run App 原基线不启动新服务，最终继续要求 R04 证明。

## 旧 24 题的审查处置

下表仅引用原完整矩阵的原始状态。所有题在采用 R06 时都须重新建立实际私有逐断言
映射、公开类型计划、执行命令和新修订 hash；“另版冻结”不表示已完成题目重新准备。

| 原编号 | 公开任务 | 类型 | 历史状态 | 处置 |
|---|---|---|---|---|
| dev-01 | `hv4-v3-dev-click-help-name` | bugfix | PASS | 另版冻结、完整清单审查 |
| dev-02 | `hv4-dev-click-short-help` | bugfix | FAIL | 另版冻结、完整清单审查 |
| dev-03 | `hv4-v2-dev-requests-quoted-content-type` | bugfix | PASS | 另版冻结、完整清单审查 |
| dev-04 | `hv4-dev-pflag-unknown-flag` | bugfix | FAIL | 另版冻结、完整清单审查 |
| dev-05 | `hv4-dev-requests-http-url-helper` | feature | PASS | 另版冻结、完整清单审查 |
| dev-06 | `hv4-dev-express-healthz` | feature | FAIL | 另版冻结、完整清单审查 |
| dev-07 | `hv4-dev-got-header-value` | feature | PASS | 另版冻结、完整清单审查 |
| dev-08 | `hv4-dev-requests-encoding-refactor` | refactor | PASS | 另版冻结、完整清单审查 |
| dev-09 | `hv4-dev-click-style-refactor` | refactor | PASS | 另版冻结、完整清单审查 |
| dev-10 | `hv4-dev-express-server-lifecycle` | run_app | FAIL | 另版冻结、完整清单审查 |
| dev-11 | `hv4-dev-got-local-roundtrip` | run_app | FAIL | 另版冻结、完整清单审查 |
| dev-12 | `hv4-dev-click-multifile-placeholder` | feature | FAIL | 另版冻结、完整清单审查 |

后续顺序保持任务书：R07 完善公开反馈与覆盖，R08 在新冻结候选上执行完整开发门和全新独立持出门。本轮停在 R06，不新增真实采样或改历史分数。
