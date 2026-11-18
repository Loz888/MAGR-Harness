# ADR HV4-H02：模型协议与默认适配器

- 状态：**Accepted**
- 日期：2026-11-10
- 决策卡：`HV4-H02`
- 协议版本：`harness-model-v1`
- 关联：[MODEL_ADAPTERS.md](../MODEL_ADAPTERS.md)、
  [harness-contract-v1.1](../HARNESS_CONTRACTS.md)

## 决策

1. **默认适配器采用 DeepSeek OpenAI-compatible chat-completions SSE**。它沿用现有
   DeepSeek 凭据、价格模型与工具调用习惯，是首版默认路径。
2. **第二协议采用 Anthropic Messages SSE**。它与 OpenAI chat-completions 在消息
   结构、system 字段、工具 schema、流事件命名和 usage 分布上不同，足以验证模型
   端口不是只适配一家供应商。
3. 两个适配器都输出同一 `ModelStreamEvent`/`ModelResponse`，由
   `HarnessRuntime` 统一预占、结算、取消和工具调度。
4. **不自动切换供应商**。一个请求冻结 provider/model/price version；缺 usage、
   流中断或工具参数不完整时不重发、不换供应商。只有请求发出前的已知 429/5xx
   可在同一供应商内重试一次。
5. **不替换旧 JSON action/report 路径**。H02 的适配器是新增模型端口；旧
   `repository_analysis/model.py` 继续服务既有分析/修复流程。
6. **工具可执行性只由权威 `tool_calls` 完成标志决定**。`error`/`length`、无
   finish 的传输中断以及负数 tokens 的响应都不得进入工具 handler；账单与工具
   可执行性分开判断，权威完整 usage 可据实结算，未知费用保留预占且不重发。

## 原因

- 两种协议覆盖“OpenAI 风格 tool_calls delta”和“Anthropic content block /
  input_json_delta”，能暴露统一事件层的真实差异。
- 首版不引入 Node/DSH 运行时，保持 H01 已接收的 Python 路线。
- 供应商切换会改变价格、上下文与错误语义，必须由后续明确轮次重新授权，不能用
  自动 fallback 掩盖未知结果。

## 后果

- H03–H07 可以在 `harness-model-v1` 上增加工作区、Broker、编辑和编码循环。
- 真实供应商调用仍需付费授权；在获得许可前，`live_model_gate` 保持 `NOT_RUN`。
- 新增协议适配器只需实现 `stream` 与 `complete`，不修改 runtime 的权限/预算顺序。
- 失败/截断响应即使携带完整工具 JSON 也只作为账单依据，不产生工具执行；新增
  适配器必须保留 `finish_reason` 的权威性，不能把缓冲提案当作完成。
- 迁移编号不变；Sources `0030` 归 Codex，Harness 未来共享迁移先对齐编号。

## 未采用

- DSH 作为模型/执行后端：H01 已记录 `NOT_ADOPTED_H01` / `NOT_RUN_NO_NODE_RUNTIME`。
- 单一供应商硬编码：无法验证统一工具契约，也不利于后续模型对照。
- 自动 fallback 到另一供应商：会破坏价格版本、授权和未知用量语义。
