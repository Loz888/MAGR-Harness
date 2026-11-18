# Harness V4 模型能力与协议 v1（HV4-H02）

- 协议版本：`harness-model-v1`
- 代码：`src/mgar_v3/harness/model.py`
- 机器可读能力表：`experiments/harness-v4/model-capabilities-v1.json`
- 默认供应商：DeepSeek；第二协议：Anthropic Messages
- 真实供应商调用：`NOT_RUN_NO_AUTHORIZED_PAID_CALL`

本层只实现 `ModelAdapter`/`StreamingModelAdapter` 的模型端口，不改变 Engine 的
预算、审批、Finding 或最终完成权威。旧 `repository_analysis/model.py` 的 JSON
action/report 路径保持兼容，不要求把所有工具交互塞进一份最终 JSON。

## 1. 能力表

| 能力 | DeepSeek Chat | Anthropic Messages |
| --- | --- | --- |
| 协议 | OpenAI chat-completions SSE | Anthropic Messages SSE |
| 模型 | `deepseek-chat` | `claude-sonnet-4-5` |
| context tokens | 131,072 | 200,000 |
| max output | 8,192 | 8,192 |
| 工具声明 | 支持 | 支持 |
| 并行工具调用 | 支持 | 支持 |
| 流式 | 支持 | 支持 |
| 结构化输出 | 支持 | 支持 |
| token 估算 | 保守字符数/4 | 保守字符数/4 |
| 价格版本 | `deepseek-2026-11` | `anthropic-2026-11` |
| 输入价格（micros/1M） | 300,000 | 3,000,000 |
| 输出价格（micros/1M） | 1,200,000 | 15,000,000 |

同一请求冻结 `provider`、`model_id` 与 `price.version`。请求中的模型或价格版本
不匹配时在发请求前拒绝；切换模型只能由后续明确轮次重新构造请求。

## 2. 统一消息、工具与事件

- 消息：`ModelMessage(role=system|user|assistant|tool, content, name, tool_call_id)`。
- 工具声明：`ToolSpec(name, description, input_schema, capability)`。
- 工具调用：`HarnessToolCall(call_id, name, arguments)`；参数 JSON 由增量片段
  组装，模型适配器只在完整后产出。
- 流事件：`ModelStreamEvent`，类型 `text_delta`、`tool_call_start`、
  `tool_call_delta`、`tool_call_end`、`usage`、`finish`。
- 最终响应：`ModelResponse` 含公开文字、工具调用、`finish_reason`、`usage`、
  `provider`、`model_id`、`price_version`、`error_code`、`cached`。
- `usage.known=false` 表示供应商未给出可用 usage；runtime 调用
  `BudgetLedger.mark_unknown`，保留预占，**不会按 0 费用结算或自动重试**。
- DeepSeek 的 `prompt_tokens`/`completion_tokens` 任一非法时整体 usage 为
  `known=false`；Anthropic 必须同时收到 `input_tokens` 与最终 `output_tokens`，
  缺一即 unknown。

### 2.1 多轮工具历史

模型→工具→模型回路保留协议原生形状：

- DeepSeek：assistant 消息包含 `tool_calls`（id/type/function.name/arguments），
  下一条 `tool` 消息包含 `tool_call_id`，模型适配器不再发送“tool_call ...”普通文本。
- Anthropic：assistant content 为 `text`/`tool_use` block 列表，工具结果作为
  同一 user 消息中的 `tool_result` block，`tool_use_id` 与 `tool_use.id` 对应。
- 多个连续工具结果合并进一个 user 消息的 `tool_result` 列表，避免非法协议形状。
- 回归测试覆盖两个协议的第二次模型请求实际形状，而不是只测单次解析。

## 3. 计费、缓存与预算

- 预占：`HarnessRuntime` 在调用适配器前执行 `BudgetLedger.reserve`。
- 结算：适配器按冻结价格计算 `usage.cost_micros`，runtime 调用 `settle`。
- 超出预占：适配器把 usage 标为 `known=false`，避免低估费用。
- 缓存：`CachingModelAdapter` 仅缓存已知成功响应；键为
  `model_id + price_version + max_output_tokens + messages + tools`。
  命中时返回 `cached=true`、`usage.known=true`、`cost_micros=0`；runtime 正常
  结算 0，不重复计费。
- 缓存不缓存 missing usage、协议错误或 `finish_reason=error` 的结果。
- 账单与工具可执行性分开判断：权威完整 usage 可以在 `error`/`length` 响应上
  据实结算，但该响应中的工具提案一律不可执行；未知费用保留预占，不重发。

## 4. 重试与失败映射

| 场景 | 行为 | 状态/错误码 |
| --- | --- | --- |
| 请求发出前取消 | 不发送 | `HARNESS_CANCELLED` |
| HTTP 200 后读取超时/中断 | 不重发，返回 error + unknown usage | `MODEL_STREAM_INTERRUPTED`；runtime `unknown` |
| 收到生成文字后取消 | 不重发；保留预占等待对账 | runtime `unknown`，reservation `unknown` |
| 429/500/502/503/504，尚未收到内容 | 同供应商、同模型有界重试 1 次 | 第二次仍失败则 `MODEL_PROVIDER_REJECTED` |
| 400/401/403/404/422 | 不重试 | `MODEL_PROVIDER_REJECTED` |
| 流传输中断/没有 finish | 不重发、不切换供应商 | `UnknownModelOutcome`，runtime `unknown` |
| 缺 usage 或 usage 非法 | 不按 0 计费 | `usage.known=false`，runtime `unknown` |
| 工具参数 JSON 不完整/非法 | 不产出工具调用 | `finish_reason=error`，`MODEL_TOOL_ARGUMENTS_INVALID` |
| `error`/`length` 响应带工具提案（含并行） | 不进入任何工具 handler；权威 usage 可结算 | `finish_reason=error/length`；runtime `incomplete`，`tool_calls=0` |
| 无权威完成标志（无 finish + 传输中断） | 不执行已缓冲的工具提案；不重发 | `MODEL_STREAM_INTERRUPTED`；runtime `incomplete`，`tool_calls=0` |
| Anthropic `input_tokens`/`output_tokens` 为负数 | 不构造 `ModelUsage`；映射为 unknown | `usage.known=false`，runtime `unknown`，预占保留 `unknown` |
| 上下文超限 | 发请求前拒绝 | `MODEL_CONTEXT_EXCEEDED` |
| 模型/价格版本漂移 | 发请求前拒绝 | `MODEL_VERSION_MISMATCH` / `MODEL_PRICE_MISMATCH` |
| SSE UTF-8/JSON 非法（已发送） | 不重发；保留预占 | `MODEL_PROTOCOL_ERROR` → runtime `unknown` |

不存在“缺 usage 后自动换供应商重发”的路径。旧 JSON action/report 仍由现有
`repository_analysis/model.py` 处理，H02 不删除或替换它。

## 5. 受控协议样本与验收

- 测试：`tests/test_harness_v4_h02.py`（30 项）。
- 两个适配器使用同一工具契约测试：文本、`read_file` 参数、finish reason、
  usage、provider/model/price 元数据。
- 分片测试：SSE 按 2–3 字节切片，覆盖多字节 UTF-8 与分片 JSON；CRLF 按单字节
  切片必须得到 1 个事件。
- 负向测试：上下文超限、版本漂移、缺 usage、工具参数中断、无 finish、
  已知 401 不重试、429 同供应商重试一次、取消、非法 SSE、缓存不重复计费。
- 多轮工具历史：DeepSeek `tool_calls`/`tool_call_id` 与 Anthropic
  `tool_use`/`tool_result` 的原生形状均有回归。
- 外部独立探针修复后结果：HTTP 200 read timeout POST 1 次且 unknown；非法
  DeepSeek usage 与 Anthropic 缺 output usage 均为 known=false；生成后取消与
  畸形 SSE 均保留 10000 micros 预占；两个协议 tool roundtrip 均成功；
  CRLF 单字节分片解码 1 事件。
- 第二次外部复验修复后结果：`content_filter`、Anthropic `refusal`、无 finish
  后超时三例均 `proposed_tools=0`、`executed_tools=0`、POST 1 次；权威 usage
  仍据实结算；`length` + 并行工具提案同样不执行；Anthropic 负数
  `input_tokens`/`output_tokens` 返回 `unknown` 且预占保留为 `unknown`，
  不再抛 `ValidationError`。
- 受控协议：**PASS**；真实供应商：DeepSeek Flash **PASS**（见 5.1），
  Anthropic `NOT_RUN_NOT_AUTHORIZED`；`live_model_gate` 记为 `PARTIAL`。

### 5.1 真实 DeepSeek Flash 实验（2026-11-10）

- 授权范围：仅 `deepseek-flash`；单卡累计上限 2 USD（2,000,000 micros），
  含本卡全部重试/实验；不授权 Anthropic 或其他供应商。
- 调用前核对：`GET https://api.deepseek.com/models` 返回
  `deepseek-flash`、`deepseek-v4-pro`；官方价格页（2026-11-10 读取）确认
  `deepseek-flash` 对应 `DeepSeek-V4.1-Flash-1017`，峰值 cache-miss
  $0.3/M、output $1.2/M（off-peak 减半）。仓库价格观察文件
  `docs/repairs/R6-HOST-20261105/v2-current-official-pricing.json` 仍在 7 天
  有效期内，费率一致。
- 固定提交：`169925f`；endpoint `https://api.deepseek.com/v1/chat/completions`；
  price version `DeepSeek-V4.1-Flash-1017-peak`。生产适配器未改；真实探针仅在
  实验子类中关闭 thinking，以固定输出与费用边界。
- 硬预算：调用前预检最坏费用 1884 micros；每轮预占 100000 micros；账本硬上限
  2,000,000 micros。预占失败或预计超额时不发请求。
- 结果：2 次真实 POST（无重试）；第 1 轮 `tool_calls` 并真实执行 `read_file`
  1 次，第 2 轮 `stop`；input/output tokens 分别为 407/45、476/74；按峰值
  价格上界计算 177 + 232 = 409 micros（`$0.000409`），不是供应商账单实际
  扣费，低于本卡上限 0.03%。
- 证据边界：只保存本地 `operation_id`、请求路径与归一化 usage；未保存供应商
  原始 request ID、原始 model/usage 回执或账单，因此不声称独立账单核验。
  runtime `incomplete` 仅表示合成任务 verifier 条件未满足，不等于编码任务完成。
- 记录：`experiments/harness-v4/h02-live-probe.py`、
  `experiments/harness-v4/h02-live-results.json`；证据副本
  `evidence/harness-v4/HV4-H02/live-flash-20261110T151946Z/`。
- Anthropic 真实调用：`NOT_RUN_NOT_AUTHORIZED`；第二协议只保留受控测试，
  不宣称真实通过。`live_model_gate` 记为 `PARTIAL`（DeepSeek Flash PASS，
  Anthropic NOT_RUN）。


## 6. 兼容与边界

- H02 对 H01 `harness-contract-v1.1` 是加法扩展：新增 `ModelStreamEvent`、
  `StreamingModelAdapter`、`ModelUsage.known`、`ModelResponse.cached/provider/
  model_id/price_version/error_code`；未移除 H01 已接收字段。
- 未修改 Engine 预算表、数据库迁移、审批或发布语义。
- 未执行真实模型、Agent 或发布门；真实编码闭环仍由 H07/H12 验证。
- Sources `0030` 迁移归 Codex；H02 无迁移。
