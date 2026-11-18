# H12-R02 有界输出与安全恢复验收（2026-11-15）

结论：R02本地工程 **PASS**。最终注册347项全部通过，零失败/错误/跳过；Ruff、
mypy通过。新增真实供应商调用与费用为0。H12真实质量仍FAIL，MVP未放行。
仅执行用户授权的R02，完成后停止，R03–R08和H13未启动。

最终源码提交 `89639adff0a6756956bb4238844577b987f4f0dc`，其父实现为 `320d2e6`。
分支 `hv4-h12-r02-20261115`；起点R01记录提交
`647435d458d566b419da5c84ca98a48133067079`、起点指纹
`e0f38c877dea8be31435de9866401d76621bc7403210f4c89d0fd6b112251729`。
最终验收前后指纹均为
`d8f5e637166eefd7bfba85eb5eea29ac4a208e3068a8e63c77cccdb7fdddf5bb`。
仅本地提交，未合并主线、推送、部署或重新运行付费质量矩阵。

## 实现

完整协议见[OUTPUT_RECOVERY.md](OUTPUT_RECOVERY.md)。

- 新profile和TaskPlan冻结按阶段的OutputPolicy；实际请求和Worker预算视图记录
  completion上限、思考/参数/正文的本地字节界限、上下文窗口及剩余续写次数。
  阶段值受冻结模型cap约束，不自动提高原8192能力声明或费用许可。较大cap只做受控
  传输验证，不能据此声称供应商已实测支持。DeepSeek仍开启thinking及max强度。
- UTF-8 SSE、工具名和JSON增量组装后做严格JSON及本地schema校验。重复键、非有限
  数、缺字段、缺名称/参数或任一并行坏调用整批不执行；自定义适配器也做派发前检查。
  显式空输入与缺失参数区分。原Broker/路径/命令/网络/epoch守卫继续独立检查。
- 原Engine CAS与Run事件保存有界wire片段、私有语义快照、provider request ID、
  原生及归一化finish、usage状态、工具ID与捕获时not_executed事实。length及无效
  JSON不会变成可执行调用，原partial记录不会因后续成功而覆盖。
- 仅已知usage、匹配CAS/任务/模型/契约/树/epoch、无未确认进程/共享未知用量且有
  原额度时允许有界续写。默认每Run最多2次，硬界0–4；使用新operation并关联原请求、
  原输出、既有回执和剩余额度。要求重新提出完整调用，不拼接JSON直接执行。
- 原PG已结算回复与RecoveryIO回执跨重启复用；未知用量/副作用保留预占、不盲重发。
  原显式429有界拒绝重试保留，500类不自动重发。新Scorer拒绝截断终答、缺回执、
  改策略、孤立partial或错误续写父引用被写成completed/PASS。
- 封存时发现旧本地meter在重启后只显示新进程调用/费用；已改为从原任务账本投影
  累计数、settled、reserved与usage_known，局部meter单独标注。受控恢复样例为3次/
  21微美元，当前进程只有1次/7微美元；该合成金额不计入真实H12消费。

无Engine/API/Settings接线、SQL迁移、BudgetLedger/结算算法、ToolAuthority或
RecoveryIO算法修改。已逐字节核对这些边界文件。依赖仍为原109个package resolution，
108个非本项目lock记录完全不变；只把原已锁定的jsonschema/referencing声明为直接依赖。
旧profile省略默认新增字段，归档R01/legacy配置不被自动升级或重算原分。

## 注册验证

`make verify TASK=HV4-H12-R02`：347 PASS、0 FAIL、0 ERROR、0 SKIP，88.866秒。
其中新增R02流协议21项、恢复/核账/评分13项，共34项；其余包含原R01、H02、H07、
H10/H11、真实PG/隔离IO、产品模型、上下文与H12计分/核账兼容回归。
`make lint`通过，`make typecheck`的258个源码文件通过。

全部模型流使用MockTransport或明确的免费本地适配器，真实供应商传输受测试守卫
阻止。集成使用独立一次性PG与原隔离命令；未停止原共享离线容器。

| 条件 | 实测结果 |
| --- | --- |
| JSON/Tool Call中间截断 | 保留原参数及length，0次提案执行，PASS |
| 增量Patch截断 | 嵌套replacement中断原样保存，0次执行，PASS |
| 缺字段/名称/参数、重复键、NaN、错误类型 | 严格JSON/schema拒绝，PASS |
| 并行坏调用 | 原生/自定义适配器均在第一条执行前拒绝整批，PASS |
| 增量名称/参数和原生finish | 正确重组；Anthropic max_tokens和归一化length同时保留，PASS |
| usage缺失/流中断 | PG标unknown、非零预占保留、私有CAS可读、POST仅一次，PASS |
| 未闭合SSE JSON | 已收wire逐字节留存，未知结果不重发，PASS |
| 续写前有工具回执 | 父引用携带旧回执，新请求不重复既有读取/命令，PASS |
| 真实补丁后截断再重启 | 旧补丁写入仅一次，原失败/最终通过测试回执保留，累计5次调用，PASS |
| 已结算续写回复恢复 | 原operation不重发，3次/21微美元与PG一致，PASS |
| 次数0/1/2耗尽及调用额度耗尽 | 明确未完成，保留partial，无无限循环，PASS |
| 阶段配置与视图 | 实际上限、Worker投影、frozen cap一致，PASS |
| 评分完整性 | 截断终答、改策略、错父引用、孤立partial、缺回执不能PASS，PASS |
| 旧历史与原账本 | 128份原score哈希不变，813次/US$4.211490/unknown0/reserved0不变，PASS |

四组PG合成回执（已知续写、写入后恢复、缺usage、断流）、Schema、原始JUnit和
完整CAS引用索引位于机器记录指向的新证据目录。工具的not_executed仅为捕获时状态，
真实执行以原Tool/Command回执为准，不将提案当作执行事实。

## 失败与修订记录

证据根为 `evidence/harness-v4/HV4-H12-R02/`，每次重试单独目录，旧日志全部保留。

| 尝试 | 结果与修复 |
| --- | --- |
| 001 | 53通过/3失败及mypy1失败；旧流夹具未声明工具、旧无trace缓存兼容、Optional缩窄问题 |
| 002 | 79项通过，mypy通过；lint拒绝测试中的宽泛Exception断言，改为具体异常 |
| 003 | 84通过/1失败；旧合成评分夹具把网页2048输出cap写成H12的8192，修正夹具声明 |
| 004 | 注册346通过，但该候选mypy的dict_values类型失败；修正后补充阶段投影/孤立partial/原生finish检查 |
| 005 | 320d2e6注册347及静态检查通过；封存发现旧meter累计漏报，保留此候选和问题记录，不作最终交付 |
| 006 | 仅相关恢复/服务/评分23项及mypy通过，使用非零合成费用检查新投影 |
| 007 | 89639ad最终注册347项、lint/mypy、历史/预算/依赖核验通过 |

依赖锁初次离线解析曾提出不相关websockets降级；未作为最终环境采用。恢复原全部
package resolution，仅补直接依赖元数据，`uv lock --check --offline`通过，已安装
websockets仍17.4。没有隐式升级/降级第三方运行包。

## 范围与交接

部分流按有界片段落盘，SIGKILL前尚未flush的尾部小片段不会被伪造为已保存；原
operation/预占会保留为待核对。思考字节界限是本地保护，不冒充供应商独立token参数；
供应商未报告的reasoning计数保留None。未用新真实模型评估代码求解质量。

SA20协调请求另在Harness主副本单独确认：提交
`1c4f494442d2d5fc8d4c1d35856cc91c922cc70e`，
`/home/developer/magr-harness/v3/docs/harness-v4/SUPER_AGENT_HANDOFF.md` 第46节。
仅该文档进入确认提交；0053/四表/三文件/11接口的精确范围无已发现冲突。
未应用SA SQL/patch、未改SA活动文件或导入H12；SA准备证据不升级为真实能力验收。

原H12质量FAIL、MVP NOT_RELEASED；新付费矩阵、独立新持出、真实发布与R03–R08/
H13均未启动。交付：[任务卡](CODEX_CLI_H12_R02_TASK.md)、
[协议](OUTPUT_RECOVERY.md)、[机器验收](accept/HV4-H12-R02/accept.json)。
