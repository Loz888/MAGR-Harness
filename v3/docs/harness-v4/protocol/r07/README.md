# R07 实际接口 Schema

这里的 JSON Schema 由当前 Pydantic 类直接生成：

- `PublicFeedbackPolicy.schema.json`：显式启用与冻结的停滞阈值。
- `PublicTestFeedback.schema.json`：公开命令身份、真实树/Run/来源 epoch、日志及有界失败信息。
- `ExecutionReceipt.schema.json`：原回执加可选公开反馈，原字段与旧序列化保持。
- `TaskPlan.schema.json`：原计划加可选冻结策略；恢复不接受替换策略。
- `WorkingState.schema.json`：固定保存当前树/任务语义下的最新公开反馈；旧版本省略空字段。

这些是结构/作用域约束，不是权限或成功证书。公开工具反馈不能代替独立 Judge、
最终树与完整回归证据。来源 epoch 来自原命令 provenance，恢复时不能改为新的执行
epoch；当前 Run epoch 继续由原 Engine/fencing 控制。

详见[公开反馈协议](../../H12_R07_PUBLIC_FEEDBACK_PROTOCOL.md)及
[公开边界矩阵](../../H12_R07_PUBLIC_BOUNDARY_MATRIX.md)。
