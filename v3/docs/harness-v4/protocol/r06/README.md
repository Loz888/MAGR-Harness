# R06 实际 Schema 与公开合成示例

这些 Schema 由当前 Pydantic 类生成，不是手工声明的替代验证器。

- `TaskBaselinePolicy.schema.json`：任务类型、两阶段计划、公开要求与结构规格。
- `BaselineAssessment.schema.json`：真实独立基线证据的类型解释。
- `JudgeTraceabilityManifest.schema.json`：私有逐断言清单、文件和执行命令绑定。
- `SharedHelperSpec.schema.json`：公开共享 helper 的有限结构检查规格。
- `SemanticRevision.schema.json`：控制器私有路径与内容 hash，不传给模型。
- `IndependentEvaluationEvidence.schema.json`：R05 根证据加基线/结构 CAS 引用。
- `JudgeAttestation.schema.json`：R05 Judge 回执加私有清单 CAS 引用。

`public-synthetic-policy.json` 和 `synthetic-traceability-example.json` 来自公开
`Box` 测试夹具，仅说明接口和身份关联，不包含新持出或实际私有 Judge 答案。
实际执行时须以本题的全部私有源码和固定命令重新生成并审查，不能复制示例身份。

`benchmark-review.json` 记录已发布旧 24 题的公开规格、历史状态/hash、三项已知
问题和重新冻结要求。状态为审查完成且必须修订，不表示旧 Judge 全部通过 R06
语义认证，也不表示新 24 题就绪或真实模型通过。

见[两阶段协议](../../H12_R06_BASELINE_JUDGE_PROTOCOL.md)和
[审查报告](../../H12_R06_BENCHMARK_REVIEW_20261116.md)。
