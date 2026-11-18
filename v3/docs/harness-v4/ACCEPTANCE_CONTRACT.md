# R01 验收契约

实现为 `mgar_v3.harness.acceptance.AcceptanceContract`，schema 为
`harness-acceptance-v1`。它约束验收内容的完整传递，不授予工具、网络、写入或费用权限。
Engine、原 PG Run 事件和 CAS/continuation 仍是原有持久化通道；没有新增 SQL 迁移。

| 字段 | 来源和约束 |
| --- | --- |
| acceptance_id | Run/task 派生的稳定 UUID，重规划不变 |
| acceptance_revision | 1–256 的递增整数，与旧 catalog_revision 字符串区分 |
| acceptance_hash | 对完整规范 JSON 去掉本字段后，使用现有 domain.digest 计算 |
| required_checks | 可信任务策略、VerificationRequirement 和公开 required；每项有稳定 ID |
| required_artifacts | 按任务类型推导，包含 verification_report，以及 patch 或 evidence_references 等 |
| scope | 当前 grant 的工作区、仓库、读写范围、执行、网络、固定命令；不产生新授权 |
| baseline_commit | 冻结公开基线；普通本地任务使用注册项目的 base_commit |
| task_type | inspect / bugfix / feature / refactor / run_app |
| objective / catalog_revision | 完整公开目标和现有题集版本 |
| verification_requirements_hash | 原可信命令/行为要求的完整摘要 |
| previous_hash / change_reason / affected_check_ids | 修订链、原因及受影响项，保留旧契约事件 |

`TaskPlan` 保存 Planner 契约与绑定，完整契约同时写入既有 Run 规划/修订事件。
派发前要求匹配该可信事件，重新计算哈希也不能凭空制造已批准的修订。
`WorkingState` 固定完整契约；Compactor 对其
做精确投影校验，模型派发前另检实际消息里的契约。普通 ContextManager 以独立 system
消息承载同一契约。缺字段、删除 required、篡改 hash/revision、弱化产物要求及阶段不一致
会抛出明确的 `ACCEPTANCE_*` 错误；模型调用前发现时不预占、不调用模型。

每个阶段以 `AcceptanceBinding` 记录 stage、id、整数 revision、hash、全部 required IDs
及 check_evidence。运行结果同时携带当前任务快照、完整契约、Planner/Context/Worker/
Verifier 回执和必需产物索引。新 H12 profile 使用 `contract-v1`；Scorer 对冻结公开
profile、任务、checklist、终态树、验证回执和产物再核对，缺契约不能 PASS。

用户追加指令经既有 Owner inbox 应用后生成新契约，旧 required 保留，新公开要求加入
稳定 ID，记录 previous_hash；旧模型回复整批丢弃。新目标的已结算模型回复随原 continuation
恢复，仍受当前 epoch 校验，不重发已知请求。checkpoint 目标若与已应用的 PG inbox
事实不一致，恢复在新调用前拒绝，要求核对；不会用旧目标覆盖当前指令。

模型建议只允许原有的加法检查，不允许替换、删除或降低 required。重复压缩可以移除
非必需历史解释，完整契约保持。分页上限不超过输入窗口的四分之一，为保护内容预留空间；
页仍经过原路径/权限/哈希校验，UTF-8 字节和续页引用保持完整，无法容纳时明确 BLOCKED。

完整性与行为判定分开：公开自然语言检查的 `evidence_check_ids` 关联现有可信检查，
最终回执沿其稳定 ID 链接实际 receipt。该关联不宣称每条自然语言要求已有独立行为测试。
入口、Ready、独立全回归和 Judge 语义覆盖留给 R04/R05/R06/R07。本卡不启动真实题目质量评分，
不向模型加入隐藏断言或答案，也不改变原 H12 的 FAIL/MVP 未放行状态。

历史兼容：旧字段默认不序列化，原 EvaluationProfile 哈希输入保持；历史核账明确使用
归档协议，旧档案可以读取。旧记录的 acceptance_integrity 为 NOT_RUN，不能冒充新契约验收。
新的运行入口不能依靠缺失契约执行，不能用 legacy 参数绕过新 H12 冻结与派发守卫。

注册复验：`make verify TASK=HV4-H12-R01`，另运行 `make lint`、`make typecheck`。
测试使用受控免费模型、一次性真实 PostgreSQL 与原有隔离命令，不使用真实供应商传输。
