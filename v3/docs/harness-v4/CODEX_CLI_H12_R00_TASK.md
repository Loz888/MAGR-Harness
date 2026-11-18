# HV4-H12-R00：基线冻结与诊断夹具

状态：DONE / PASS（本地基线冻结与工程证据核验）。用户于2026-11-15明确授权“只执行R00，完成基线冻结和证据核验后停止”。
本卡为本次唯一获准的H12-R实施项；R01–R08不注册、不实施、不启动付费矩阵。

## 依据与范围

原始任务书为用户Downloads中的HV4_H12_R_Systematic_Repair_Taskbook.md/docx。
MD SHA256为`d7c4bc793b25b894fd98d1169e042a1bddedd3eff08e69a0cc52339ab7bc4d35`；
DOCX SHA256为`f8cf22c0cd52d52b61806aee14b03e1e03d5e059b0184fde240967f92cc266d6`。
二者R00条款一致。全文含已见私有判定信息，只放控制器私有证据归档，不复制进公开诊断夹具。

修复起点为`3804a401c2d52fd18291990c9a886b4772db420a`，源码指纹
`81f17c419139c832ee20199d9390b3f941445079af55978ffdc3bbdb69134cc8`，
分支`hv4-h12-r00-20261115`。业务代码、历史任务/源码/评分、旧FAIL/NOT_RUN、调用与费用
保持原样。已有持出与补测只用于已见诊断，不成为新的独立持出。

## 工作及产物

1. 记录Git提交、标准源码指纹、工作树、依赖锁与已安装包、H12隔离PG迁移版本。
2. 只读导出同一PG权威账本和H12 Run/模型/工具事实；复制原CAS、score、供应商回执、
   工具/命令/测试/Judge日志、补丁与核账记录。原输入只读，新归档对象设置只读并校验SHA256。
3. 按版本建立题目索引，包含任务ID、仓库commit、类型、原始状态、根因类别和证据引用；
   不把不同候选记录合成新分数，不改原模型代码，不重采样。
4. 在公开synthetic夹具中复现契约丢项、输出截断、无效工具JSON、路径拒绝、Ready缺项、
   回归漏跑、Feature红基线、Refactor提前要求新结构。夹具不能复制私有答案或Judge实现。
5. 在`evidence/harness-v4/HV4-H12-R00/<attempt>/`保存baseline.json、
   h12-case-index.json、source-manifest.json、diagnostic-fixtures/、evidence-manifest.json，
   以及commands.json、test-results.json和accept.json。失败尝试独立保留。

## 验收

- `make verify TASK=HV4-H12-R00`必须真实执行已注册的非空测试；另执行lint/typecheck。
- 标准源码指纹从不可变Git字节重算，与原主轮、补测、当前起点三份记录一致。
- 原始文件逐份hash核对；归档篡改、缺失、可写对象、路径逃逸、错误schema不能PASS。
- NOT_RUN表示未执行，BLOCKED表示有阻塞或缺证据，FAIL表示已执行检查失败；不得默认PASS。
- 公开夹具正反例稳定、模型侧仅受控传输；私有原始PG/CAS/Judge不进入模型上下文。
- 共享账本核对813次、保守US$4.211490/US$12、未知/预占0；R00前后相同，新增付费0。
- 缺少或未核实的资料明确记NOT_RUN/BLOCKED，不能补写通过、零失败或新质量结论。

R00只新增离线归档/核验脚本、夹具、测试及注册/导航，不改Engine/API/schema、预算算法、
Worker/工具运行时或业务源码。原工程/真实模型/Agent/发布门分别保留，MVP未放行，H13未启动。
完成R00证据交付后停止，后续卡须另有明确批准。

## 交付

R00实现提交`f3390e6367b3232cf54e3c0fa572b3df591ef8fa`；注册40项、lint、mypy通过。
已校验99,971条归档来源记录、35,782个只读内容对象及4,774条类型化引用；128份原评分
按版本保留，题目索引所需元数据完整。新增付费0，累计813次/保守US$4.211490，未知/预占0。
四次R00失败尝试及草案审计均保留；原输入文件字节不变。

[完整报告与限制](H12_R00_BASELINE_FREEZE_20261115.md) ·
[验收记录](accept/HV4-H12-R00/accept.json) ·
[完整题目索引](accept/HV4-H12-R00/h12-case-index.json)

任务书旧README引用缺本地对象，远端对比为NOT_RUN；另两份旧blob与当前基线不同，
差异已登记。该限制未折算PASS。原H12/MVP仍FAIL，R01–R08与H13均未启动；本轮已停止。
