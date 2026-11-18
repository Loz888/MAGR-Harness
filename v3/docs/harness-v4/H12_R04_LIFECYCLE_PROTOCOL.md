# H12-R04 入口与生命周期协议 v1

本协议是本地工程核验；不构成真实模型质量通过或 Coding MVP 放行。仅新冻结的
`LifecyclePolicy(version="harness-lifecycle-v1")` 启用，旧 profile 的空策略不改写。
默认入口观察10秒、SIGTERM观察5秒、强制清理观察5秒；合成反例使用更短的显式冻结值。
每个新 profile 保存完整策略及固定控制资产哈希，运行与恢复时校验。

## 入口清单与实际加载

最终工作树中新增或修改的 Python/Node 可执行模块、冻结范围内缺失的新文件、
显式服务脚本、npm 简单脚本、改变的 `main/module/exports` 均进入清单。跳过测试、
verification 和静态 web/public/assets 目录。每批最多32个目标，超限保持阻断。
目录清单有独立 hash；没有目标只表示清单为空，不能描述为已执行模块测试。

Python 使用包/模块 import，支持显式 cwd、根目录和 src 布局。Node 使用实际
require/import，遵循项目的 CJS/ESM 语义。不能用旧回归套件成功替代新模块加载。
公开要求模块导入不绑定端口时，加载器另外观察本进程监听 socket。入口文件及
cwd 必须是工作区相对路径，遍历、宿主绝对路径和运行时不匹配继续拒绝。

`EntryPointReceipt` 保存 task/run、执行源 epoch、operation、spec、tree、
acceptance hash、loader hash、真实退出码、日志CAS hash、清理回执及结果。
清单批次记录所有目标和回执，只有全体成功、观察完整且最终树未变才为PASS。

## Ready 与 HTTP 事实

`ReadyReceipt` 保存时间、task/run、执行源 epoch、command/handle、环境与最终树
hash、宿主/实际端口、PID/进程组、快照、HTTP观察和验收版本。观察开始、耗时与
冻结时限同时保存；超过时限即失败，不能因为晚到的成功响应转成Ready。

HTTP 模式要求 stdout 中恰好一条含实际端口的Ready行、项目进程树确实持有该
监听端口，以及规定的独立GET探针通过。动态端口记录实测端口，不保存默认配置
冒充实际值。只请求同一已授权容器内的 loopback；不发布宿主端口，不携带浏览器
凭据，不跟随重定向。响应有界，保存实测status、Content-Type、body hash和body CAS。
只从明确的公开GET契约构造期望；语义不清的需求不能凭猜测补写私有测试答案。
失败回执可保留尝试核验的端口，不能把它当作已证实的监听地址；真实监听见
snapshot.listeners，只有ready=true的地址完成了端口归属和HTTP核验。

selfcheck 需真实观察本地客户端响应，再在关闭之前由独立容器探针复查同一端口、
路径与响应体，随后释放关闭屏障。要求退出0且 stdout 打印实测状态；stderr访问
日志和npm版本横幅不算状态输出。Node hook按客户端PID区分npm父进程；Python
使用标准HTTP客户端/服务器 hook。`printed_statuses` 保存匹配事实。

`ServiceEvidence.stdout_hash` 是Ready/selfcheck观察阶段的原始stdout摘要；
`raw_log_hash` 是清理后的完整控制日志CAS摘要。日志以带输出流标记的帧封装，
原stdout/stderr字节使用base64，不丢弃失败栈和清理日志。

## 独立清理事实

固定 supervisor 保留根进程的退出状态，并作为subreaper跟踪脱离原进程组的后代。
`ProcessPortReceipt` 同时记录SIGTERM时间与冻结宽限、前后PID/PPID/PGID/SID、
进程启动时间、状态/退出码、监听socket所有者、端口可重新bind/listen的观察、
孤儿PID、是否强杀以及容器是否移除。

先向项目进程组发SIGTERM，等待有界退出；孤儿或不退出的进程仅在相同已核实
supervisor后代范围内强制清理。删除容器前独立检查进程不再执行、监听消失和
端口复用。容器消失本身不等于优雅退出或已核实端口释放。撤权后仍可停止原
精确归属的子进程；缺少独立观察时字段为null，不能宣称PASS。

HTTP启停核验在已预占的start操作内完成清理，不依赖下一次模型回复或额外工具
额度。stop操作仅确认原清理事实；中断、超时、取消、失败也执行清理。R01最终树
稳定性守卫继续生效，旧树的成功回执不能用于变化后的最终树。

## 原权威、持久化和恢复

继续使用原Engine、持锁PG连接、operation journal、预算预占/结算、RecoveryIO和
CAS。Ready/cleanup及入口探针按原tool单位计费记账，不增加模型工具或隐藏预算。
检查器不写Engine/API/Settings/schema，不增加任务的路径、网络、凭据或挂载。

内部`RuntimeControl`由可信组装代码构造，不能由模型工具参数传入。原argv仍经
原Broker授权。entry模式实际执行固定的import/load控制器，原argv是授权锚点，
**不代表原测试命令被执行**；RuntimeControl/spec/控制资产hash明确记录差异。
入口探针进一步收窄为只读挂载。HTTP/oneshot在同一原授权容器执行原固定命令，
附加固定的观察与关闭屏障；不接受任意控制代码。

已知回执经原PG/CAS复用，不重启项目命令；恢复保留原执行源epoch，不把它改成
新epoch。只有同task/run、验收、树、目标、资产且源epoch不晚于当前epoch才能复用。
新的执行仍需当前Lease/epoch。未知结果沿原精确归属命令核对并保留unknown，
不能盲重放，也不能补造Ready或优雅清理事实。新评分器额外校验生命周期完整性；
旧profile的生命周期完整性为NOT_RUN，历史评分不改写。

这些观察覆盖本卡列明的Python/Node合成用例，不是对恶意同UID项目的通用远程
证明，也不是所有语言或HTTP客户端的完整支持。无法观察的selfcheck保持失败/阻断；
真实质量仍需后续新冻结候选与独立Judge。
