# R02 有界输出与安全恢复协议

`OutputPolicy` 在新H12 profile与TaskPlan中冻结；普通HarnessCodingService默认启用。
低层CodingLoop可显式传入策略，归档profile的None保留原协议，不回写历史或自动续测。
实现位于 `harness/output_protocol.py`、`model.py`、`coding_loop.py` 和原持久化适配器。

| 阶段 | 默认completion上限 |
| --- | --- |
| investigate | 4096 |
| implement | 8192 |
| finalize | 4096 |
| continuation | 8192 |

各阶段均取策略值与当前冻结cap上限的较小值；阶段值可以独立配置。协议类型的有界上限
为131072，不代表供应商已实测支持该值、放宽费用许可或自动提高原模型能力。原冻结
DeepSeek/Anthropic能力声明仍为8192，现有网页默认cap仍2048，原H12费用/调用/期限门不变。
测试中的16384请求只经MockTransport，证明配置通路，没有进行供应商能力或质量验收。

`ModelRequest.output_budget` 与Worker的受保护 `resource_budget.output` 同时记录当前
阶段、completion tokens、reasoning/tool-argument/answer字节界限及续写次数。上下文
窗口另在原tokens投影中记录。思考控制沿用 `thinking=enabled`、`reasoning_effort=max`，
不添加供应商未声明的独立thinking token参数。推理文本只进入私有CAS及原provider
continuation，供应商报告reasoning_tokens才记录实际计数，未报告则None，计费仍使用
原completion总数，不重复加收推理tokens。相关供应商接口约束见
[官方思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode/)。

## 状态和工具完整性

每次输出有 `ModelOutputReceipt`：operation、供应商request ID、归一化finish、原生
provider_finish_reason、complete/incomplete/invalid/unknown、known/unknown用量、
私有artifact哈希、工具参数状态、当前阶段预算和续写父引用。例：Anthropic原生
max_tokens保留，同时归一化为length；JSON截断即使归一化error，原length仍在回执。

SSE增量组装UTF-8、工具名和参数。所有工具必须同时满足：显式ID/名称/参数、严格对象
JSON（重复键及NaN/Infinity拒绝）、已声明工具名及本地JSON Schema。Registry不检索
远程schema。显式Anthropic初始input={}可表示空输入；缺失参数不能默认填成{}。
整批校验通过才交Broker，任一并行调用不完整则整批不执行；自定义适配器也有派发前
校验。原路径、命令、网络、额度与epoch守卫继续独立检查。

只有tool_calls finish允许执行工具；length/error/stop携带的工具提案不能执行。
回执的not_executed是捕获时事实，真实执行另由原Tool/Command/RecoveryIO回执证明，
不会把模型提出的命令记成已执行。最终完成仍要求R01绑定和原Verifier/当前树通过。
新Scorer拒绝被改成completed的截断终答、缺回执、改策略、孤立截断或错误续写父引用。

## 私有流证据和已有持久化

原Engine CAS保存16KiB原始SSE片段和有界语义快照，Run事件索引其哈希。包括未闭合的
SSE JSON和参数，异常或usage缺失也保留已收字节。单帧最多1MiB、单请求wire最多2MiB；
每份结构化快照最多1.6MB，正文/参数/推理另有字节界限。checkpoint仅引用回执/CAS，
不再内嵌重复原始流。未收字节、SIGKILL前尚未flush的小片段不会伪造为已保存。

私有输出不注册为可读源码或测试日志，知道CAS哈希不赋予artifact_page权限。公用
历史/最终报告不展示推理正文或凭据。HTTP拒绝保存状态和request ID元数据；500类不
自动重发，原显式429的有界拒绝重试保留，H12真实评测适配器仍固定单次物理尝试。

所有事实仍经原Engine持锁连接/短事务、Run事件、usage_calls、harness_operations
和DurableContinuation；没有新表、独立业务状态或结算算法。文件写入和网络IO不放进
业务事务。Observer透过Metered/Product守卫传递，崩溃后索引和未知预占仍在原PG中。
结果摘要的调用数、已结算费用、预占与usage_known读取同一原任务账本，重启不丢失
前一进程用量。`metered_process` 和取消回执中的current_process_meter仅记录当前
进程，不冒充累计账单；未知费用不会因本地meter归零而消失。

## 续写和未知边界

默认每Run最多2次，策略硬界0–4；不是重发原operation。允许条件为：原输出明确
length或JSON/schema invalid、用量已知、CAS/任务/模型/版本/树/契约/epoch一致、
没有待确认进程或共享未知用量，并有原账本剩余调用与时间。新请求仍须原预算预占、
context和Broker授权，不能借续写增加总calls/tools/费用/期限。

`OutputContinuation` 记录原operation/root、原provider ID、CAS哈希、当前计数、
树/Acceptance hash、既有命令/工具证据ID和只读剩余预算。Worker收到有界的原文/参数
预览及明确not_executed事实，必须重新提出完整调用；不在服务端拼接JSON后执行，
不要求整文件重写，优先H05 apply_edits。既有回执继续使用，不重复写入或命令。

未知usage、流中断、未知副作用及未核实的结果不自动续写/退款/清空预占，先经原核账
流程确认。重启复用原PG已结算回复和RecoveryIO回执；旧epoch不可写。达到次数或
资源界限时保留partial artifacts并返回未完成/预算耗尽/阻塞，不能PASS。后续合法
完整回复可以完成同一任务，但原截断回执永远保留未完成状态。

工程验证：`make verify TASK=HV4-H12-R02`、`make lint`、`make typecheck`。
真实模型质量、外部发布、R03–R08与H13不由本卡放行。
