# DeepSeek 最高推理强度配置修复

依据用户2026-11-15明确要求：后续每次DeepSeek API调用均开启思考，使用供应商
支持的最高推理强度。此要求覆盖此前显式关闭思考的实现选择。

## 核查结论与实施范围

原项目助手与H12适配器、调查、结构化修复和路由客户端显式发送
`thinking.type=disabled`；仓库分析对当前产品模型也显式关闭，其他路径未统一指定。
原代码没有设置`reasoning_effort`。旧H12的`h12-model-v1.json`明确记录关闭思考，
历史评分不能描述为最高推理强度的结果。

现在所有新DeepSeek请求共用`models/deepseek_policy.py`，发送：

```json
{
  "thinking": {"type": "enabled"},
  "reasoning_effort": "max"
}
```

覆盖通用Harness、项目助手、H12、GitHub仓库分析、调查、结构化修复、Router、
V3.5付费客户端及V3.4 CLI。产品能力描述也报告开启思考和`max`，保持既有模型与
价格配置。官方支持的最高档为`max`，`high`为默认档，不能把两者等同。
来源：[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)与
[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)。

## 工具历史、恢复与计量

带tools的请求需要回传此前assistant消息的完整`reasoning_content`。流式客户端
现在收集分块思考内容，仅存私有CAS；消息、响应及checkpoint只保存哈希与字节索引。
后续请求按Task/Run/Workspace/Actor/模型及原epoch范围核验，再取回原字节；
缺失、损坏、外部范围或未来epoch在发送前拒绝。已确认的同Run旧epoch上下文可在
Engine恢复后继续，不能据此恢复执行权限或重发已结算模型调用。

工具调用和未调用工具的assistant答复均保留私有续接引用；压缩按完整历史消息或
工具交换执行，保留消息的思考内容不会单独截断。公开事件、报告和前端不输出思考
文本；没有tools的JSON协议继续只读取正式content，不向下轮添加私有思考。

私有引用展开后的字节计入上下文压力、输入上限与产品预算预占；供应商
`completion_tokens`已经包含思考Token，结算按原总数一次计费。既有输出上限、
单题调用上限、累计费用与未知用量规则保持；H12/产品的8192输出上限包括思考和
正式输出，不因`max`自动增加。达到输出上限仍按原未完成规则处理。

## H12与验收

旧观察文件、冻结、分数和账本保留。新增`experiments/harness-v4/h12-model-v2.json`，
控制器绑定新观察哈希与带思考配置的profile。旧候选不能在新源码上继续派发；新模式
的真实质量评分需新的完整准备和冻结，不复用之前的PASS或把这次工程通过当作质量通过。

注册验收：`make verify TASK=HV4-DEEPSEEK-REASONING`。受控HTTP传输对所有DeepSeek
请求检查`enabled + max`，专项覆盖完整思考字段回传、所有assistant历史、私有存储、
预算、压缩与真实PG恢复；工程用例禁止访问真实DeepSeek HTTP传输。

主线注册专项305项、产品回归211项通过；H12独立工作树注册专项317项、质量修复
回归375项通过，各组有重叠。两份源码的Ruff/mypy通过，各注册组前后指纹一致。
H12首轮17项失败因缺旧R5/R6本地沙箱锁与AST工具；核对原构建源码、镜像ID和
工具哈希后补齐相同资产，同一源码的完整317项复跑通过，原失败保留。

本地18080服务在原PG确认无活动Run后优雅重启；健康检查与认证模型配置接口均
返回200，当前Flash及Pro目录均显示开启思考和`max`。本次只读验证配置，不发送
付费探针。H12旧候选在新profile下被拒绝，不会静默切换模式继续旧评分；原账本
仍584次、保守US$2.632213/US$12，未知/预占0。

详见[机器接收记录](accept/DEEPSEEK-REASONING-20261115/accept.json)。本次新增模型
采样0；新模式真实质量仍NOT_RUN，H12的旧FAIL、MVP未放行和H13未启动状态保留。
