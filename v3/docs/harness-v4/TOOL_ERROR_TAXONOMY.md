# R03 工具错误、纠正与授权边界

适用版本：`harness-tool-errors-v1`。仅新建的受控服务及新冻结的contract-v1评测
profile默认采用；归档profile缺省为None，恢复时保持原策略，不重写历史计分。

## 回执与错误分类

公开结构见[JSON Schema](../../config/harness_tool_error_receipt.schema.json)；源码为
`src/mgar_v3/harness/tool_errors.py`。回执包括工具与operation/call ID、八类错误、
任务/Run/workspace/epoch/tree/验收hash、原请求hash、策略hash、合法相对根、参数
约束、固定命令ID、可恢复性、剩余纠正次数、下一步及授权/核对要求。

| 分类 | 处理 |
| --- | --- |
| PATH_OUT_OF_SCOPE | 拒绝该路径；仅提示原TaskGrant与当前Lease交集，允许有界的新调用 |
| INVALID_ARGUMENT | 拒绝该参数；提示声明的类型/约束，固定命令或合法分页续页方式 |
| SYMLINK_ESCAPE | 立即拒绝，不重试；原canonical、保护路径和竞态检查继续生效 |
| MISSING_DEPENDENCY | 阻塞并保留准备问题；不自动安装、联网或增加挂载 |
| CAPABILITY_NOT_GRANTED | 进入既有Ask/inbox，等待正式授权；本任务仍无新增权限 |
| RESOURCE_LIMIT | 阻塞；纠错额度不能扩展原调用、费用、期限和资源限制 |
| EPOCH_STALE | 拒绝旧执行者；由原控制器接管，不以纠正方式绕开fencing |
| NON_RECOVERABLE_POLICY_DENIAL | 保护数据、身份、撤权、外来CAS来源等立即停止；未知副作用先核对 |

`executed=false`表示被拒绝的目标操作未执行；授权检查、参数检查及已授权分页首页的
有界纠错读取可以发生。`executed=null`表示无法证明无副作用，不能据此自动重试。
真实执行过的失败测试继续保留原Command Receipt、失败摘要和原始日志引用，不改写成
未执行的参数错误。未知/未列入白名单的失败不能因为错误文案看似可恢复就继续。

## 有界纠正

`ToolErrorPolicy.max_corrections`默认3，可信配置范围0–8。它表示首次拒绝之后的额外
纠正机会；默认连续回执显示3、2、1、0，第四次可纠正错误后停止。不同工具、不同路径、
不同错误或一次成功均不能重置本Run计数。每次尝试仍占用原工具额度，也仍受模型调用、
费用、上下文及期限限制。配置在TaskPlan/profile中冻结，不能在恢复时增加。
该计数针对已知拒绝；合法的辅助读取、编辑和测试由原账本约束，不算新的参数错误。

每个被组装的工具尝试有独立原operation ID。预检拒绝也先走原额度预留及RecoveryIO，
以原PG操作日志和CAS保存已知回执；进程重启复用已知拒绝，不重复预检或执行。
成功纠正的原工具回执另以`tool_correction_recorded`及结果索引指向前次拒绝。计数、
关联和open questions随原continuation保存，属于观察索引，不替代授权或预算账本。

无进展检测仍逐批执行；可纠正错误不是进展，仅在还有纠正机会时允许继续下一批。
成功但重复的读取/命令仍受原无进展守卫限制；纠正额度耗尽时明确保留待补充信息。

Schema不完整或类型错误的整批模型输出先由R02阻止执行。R03只在原模型operation的
恢复提示里补充INVALID_ARGUMENT和声明参数约束；不伪造未发生的工具operation。
该层使用R02冻结的输出纠正次数；重新生成的完整工具调用才进入R03/Broker。

## 安全反馈与权限询问

模型反馈不包含底层错误中的宿主绝对路径、密钥或环境值。相对根取TaskGrant与当前
同身份/同workspace/同epoch Lease的交集，保护路径继续由Broker排除。只能推荐原已许可
的`tests`/`public_probe`/`build`命令ID，不能推荐任意诊断命令、额外挂载或新网络权限。
授权分页错误仅返回当前已颁发路径/hash对应的合法retry/next参数；外来hash、路径、
epoch、Lease或来源不因参数错误而放宽。

底层诊断最多16000字符保存到原私有CAS，回执仅带`diagnostic_hash`。该诊断不是向模型
颁发的source/log artifact；即使模型知道hash，也不能通过artifact_page读取。真实命令
的完整原始日志仍沿用原日志CAS分页。没有新业务表、新授权服务或第二套结算机制。

新能力使用既有`_TaskInteraction`、EngineInbox及Ask事件，实际等待时通过原PG inbox
投影waiting_input。服务不再把本地status缓存作为等待状态的唯一来源，且PG的暂停、
取消和终态优先。回答“批准”只是inbox数据；本Run的TaskGrant、Lease及冻结验收范围
不改变。需要扩权时通过原控制器另建正式授权任务；当前任务保留BLOCKED。取消等待
尊重原取消token，不能因文字回复变成执行许可。

## 验证范围

R03新增测试覆盖宿主/Windows/遍历/编码路径纠正、0/1/3/8上限、跨工具及成功后不重置、
原工具预算耗尽、多调用协议配对、同读无进展、保护数据、撤权/epoch、Ask文字不授权
与取消、符号链接和check/open替换、分页/外来hash、私有诊断隔离、原PG拒绝复用和
次数恢复，以及原Broker的固定命令修复闭环。实际注册和结果见
[验收记录](accept/HV4-H12-R03/accept.json)。

这是工程协议验收，不是dev-12的真实模型PASS，也不是H12质量或MVP放行。后续任务卡
及新付费矩阵需要相应授权；本次只完成R03。
