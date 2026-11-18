# H12-R05 独立回归与评分协议

版本：`harness-independent-regression-v1`；解析器：`harness-test-results-v1`。
这是R05工程协议，不改变原H12题目、质量阈值、费用账本或历史评分。

## 冻结及执行

新`contract-v1`评测profile冻结`independent_regression_plan`及其内容hash。计划来自
公开的`acceptance.regression`，沿用原公开Verifier的命令规范化与固定环境配置，
不以模型的目标测试命令代替全回归。未知npm脚本仅可从固定commit的公开
package.json或原始快照解析；无法确定报告格式时保持NOT_RUN/BLOCKED。

Reporter仅扩展观测：pytest verbose及关闭提前停止、unittest verbose、Go JSON、
Mocha JSON及关闭bail、AVA/Node TAP。保留原测试路径、选择条件及显式Go重复数。
Go重复身份目前不能证明完整比较，诚实阻断；不把重复次数降低为1来通过评分。
未声明的pytest deselection、skip/xfail/xpass、空套件、编译/collection错误、
未知reporter和不完整身份清单均不能证明完整回归。必要的项目配置仍由原冻结测试
清单校验，包括配置布局及package测试配置等虚拟校验项。

独立基线在模型派发前于原树执行。最终阶段必须有原Engine的封存树和
task/run/epoch，再复制同一只读源码树；权限、镜像、网络、命令、日志及资源
仍经过原Broker/CommandSession。没有新增模型可调用工具或写权限。
普通回归与公开目标探针分别运行、分别存储；模型期baseline/run_tests只供诊断。

每条冻结回归都产生独立回执。某条断言失败或超时后仍收集后续冻结命令；取消则
停止并标记缺少完整证据。使用唯一归属临时目录与容器，仅清理本次资源。
已确认的report-ref可核验复用；已派发但没有完整回执时阻断并等待核对，不盲重跑。

## 回执与CAS

[七种JSON Schema](protocol/r05/)由实际Pydantic模型生成：RegressionPlan、
IndependentTestReceipt、IndependentRegressionReport、IndependentComparison、
JudgeAttestation、IndependentEvaluationEvidence、DimensionReceipt。

命令回执包含catalog task/experiment、原执行task/run/源epoch、baseline/final、
suite/role、计划hash、最终源码tree与执行副本前后tree、冻结公开资产hash、
原公开argv与实际reporter argv、cwd、实际环境digest/环境规格hash/镜像ID、
command/handle ID、状态/退出码/时间、完整原始日志CAS/hash/字节数/截断标记，
以及逐项测试身份、通过/失败/跳过和失败ID。receipt_id为除自身外规范内容的SHA256。
日志与逐命令结果独立保存于CAS；checkpoint和最终索引不内嵌重复大日志。

计划与manifest列明全部预期suite。Scorer逐个验证其数量、顺序、唯一性、原argv、
scope和tree，读取CAS验证hash，再从原始日志重解析；缓存计数与重算不一致即阻断。
内容hash提供完整性，原Engine选中的result_hash提供来源权威；调用方自行传入或
重新封装CAS引用不能代替原PG选择。receipt_id不是独立的身份签名。

基线与最终阶段的环境规格、镜像、实际环境、冻结测试清单、每条命令的完整测试
身份集合必须相同。`new_regression_failures`为最终失败ID集合减基线失败ID集合的
大小，身份带suite前缀；不能用失败数相减。仅两阶段完整、解析成功且可比较时
允许整数，包括0。未运行、日志/回执缺失、跳过、解析失败、身份变化、树/Run/
环境不一致或必需独立证据缺失时为null。

基线已有失败单独保存；最终仍有失败，即使新增为0，公开回归状态仍为FAIL。
目标测试全绿也不能覆盖`test_light_imports`等完整回归失败。

## 五项判定与旧结果

| 字段 | 判定来源 |
|---|---|
| target_behavior | 有独立冻结公开probe时必需且重新执行；无独立计划时NOT_RUN，并说明组合oracle仍在Judge中 |
| public_regression | 全部冻结公开回归与基线的原始证据比较，始终必需 |
| independent_judge | 原Judge逻辑，保存源tree/私有执行副本tree及原始日志CAS，始终必需 |
| structural_constraints | 未声明独立结构计划时NOT_RUN，不从组合Judge猜测结构PASS；公开结构语义审查属于R06 |
| execution_integrity | 原Engine选中结果、同一最终源码tree、Run/epoch与完整必需证据，始终必需 |

Judge执行副本可能包含冻结私有测试，因此其副本hash单列；Judge和公开回归均绑定
同一封存源码tree。这里只增加来源元数据，不改Judge断言、任务类型基线规则或
公开结构要求，也不将私有日志/测试内容传给模型。

每项保存状态、required、tree、receipt_id和证据CAS引用。所有必需项PASS、原运行
终态合格且原有验收/输出/生命周期/预算守卫通过，才可计PASS。原model-phase计数
保留`model_phase_`前缀，不能影响权威回归数。模型期真实baseline计数修复也只改善
诊断，不是独立验收证明。

新协议聚合记录不能省略五项回执或必需项，也不能用未知计数参与“总失败为0”；
有未知任务时总数为null，并单列已验证小计。旧协议历史文件保持原样。
旧summary-only日志中的失败名称仍可用于诊断，但不能升级为R05完整范围证据。

`append_regression_audit`仅向历史目录之外的新目录写追加审计：记录原score
SHA256、旧状态、新重算结果及Scorer指纹。原score不覆盖、运行不恢复、模型不调用，
没有旧证据时保留null/NOT_RUN。本地追加审计不改变原H12/MVP状态。

本协议验证列明的固定reporter及合成公开场景，不声称覆盖所有测试框架、任意
项目脚本或恶意项目的所有行为。真实24题的新候选冻结和付费评分须按R08另行验收。
