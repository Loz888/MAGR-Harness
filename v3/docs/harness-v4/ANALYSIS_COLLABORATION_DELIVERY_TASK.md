# GitHub 持续协作与受控代码交付

状态：ENGINEERING_PASS，2026-11-13；真实模型与GitHub发布/合并为NOT_RUN。
依据用户要求：思考中能继续发消息、修正过早
AGENT_STALLED、缺信息时等待回答并继续，以及接通调试、测试、修复、PR和显式合并。
本卡不改H12历史、基线、阈值或SA验收，不使用其付费许可。

## 范围与权威

- 分析目标仍绑定既有固定仓库/commit。运行中补充仅作为用户输入，不修改预算、
  工具或授权；终态主动追问沿原Thread记忆及会话剩余额度。
- 分析专属输入在原Engine持锁连接的PG事件中持久化、幂等和定界。每次模型请求
  使用冻结的输入快照；后来消息只在下一安全点进入，不改变已发送请求。
- 新等待问题保存原Run/checkpoint；回答继续同Run，旧epoch不得执行。旧历史
  partial/unknown记录保留，未知调用不自动重发或退款。
- 代码修复转入既有Harness隔离工作区、项目许可、命令计划和可信验证，不把写工具
  或凭据交给只读分析模型。仓库内容不能授予权限或任意宿主路径。
- 原Engine仍是唯一业务写者；费用沿原账本。远程写通过有审批的Action/Outbox与
  Dispatcher，不在业务事务中联网。未知写只读对账，不能盲重试。
- SA12独占0044，依据SUPER_AGENT_HANDOFF第37节独立确认`298d4c5`。
  本卡仅新增0045_harness_github_delivery及app.harness_github_*；不改SA树、
  Engine组合/租约接线或历史SQL。旧Workbench的Draft-PR-only契约保持。

## 任务与验收

1. 运行时输入保持可编辑。单一按钮：有文本提交补充/回答，无文本中止；停止后
   继续基于已保存上下文。发送失败保留文本及幂等ID，不制造重复输入。
2. 连续空搜索/同范围重读触发重规划，检查manifest、入口、测试与未读文件。
   无法通过现有工具解决的具体缺口才提出问题，等待同页回答，不伪造完成。
3. 修正GitHub标准搜索metadata的owner重复拼接；合法同仓命中保留，异仓继续
   拒绝。搜索指针不能直接用作源码证明，仍须读取固定commit文件。
4. 从当前固定仓库进入编码项目，显式项目授权后复用隔离执行、回归测试与完整Diff。
   缺少工具链、凭据或权限时显示可纠正的配置入口，不宣称已真实调试或发布。
5. 发布前审查完整Diff/测试、固定base、目标仓库/分支及payload hash。独立写凭据
   与项目发布许可缺一不可；精确审批后创建Draft PR，幂等记录各子步骤及回执。
6. 合并是单独动作，绑定真实PR及精确head、当前权限/CI/保护状态与新审批。
   head改变、检查未通过或写结果未知时拒绝/对账，不自动合并。
7. 真实一次性PG覆盖并发输入/终态竞争、checkpoint恢复、epoch、授权/撤销、
   Action/Outbox与未知远程写对账；受控GitHub/模型，无付费或真实PR。
   浏览器覆盖同框运行中补充、等待回答、发送重试及Diff/发布/合并交互。
8. 完成lint/typecheck与相关注册验收。保存首次失败、修复版本和精确复验范围；
   工程、受控远端协议和真实平台结果分开记录，不将fixture成功冒充live。

## 已确认的问题证据

只读核对主库运行`9a429aae-ac51-4d72-8008-7fcabc6d6ec8`：9次模型调用、
7次远端工具调用，另有重复CAS源码读取，checkpoint的no_progress_rounds=4，
停止码AGENT_STALLED。预算与权限不是该停止码的触发条件。原始历史保持。

另外，标准GitHub search payload同时含repository.full_name及owner.login时，
原代码把owner/repo拼成owner/owner/repo并过滤。四个受控metadata案例首次2失败/
2通过，标准返回格式缺陷已确定；历史过滤前远端结果未保存，不能断言该历史四次
空搜索均由此造成。证据位于`evidence/harness-v4/ANALYSIS-INTERACTION-20261113/`。

只读依据：根及v3/AGENTS、v31-upgrade/TASK_QUEUE、ADR0001、
HARNESS_PRODUCT_TASK、ANALYSIS_CONTINUATION_AND_PROGRESS、现有Engine/
分析/产品/Action/Outbox实现及SA12固定提案。后续验收与实际限制另附，未完成前
不标本卡PASS。

## 实际交付与验证

已实现上述交互、受控代码交付及恢复链，新增迁移0045，SA0044保持独占。
等待问题使用原Run/checkpoint；运行中输入以PG事件引用保存，所有已接收用户约束
从PG取回并进入受保护上下文，包括中断前尚未应用的消息。四轮无进展先重规划，
有未读路径则继续调查；真正需要用户事实时保留任务等待。未知用量仍不自动重发。

验收详见[机器记录](accept/AGENT-COLLABORATION/accept.json)：

- `make verify TASK=AGENT-COLLABORATION`：52/52 PASS、无skip，注册起止源码
  指纹均`7723839d959db69fec0a257e1d15405dcd92c06bb57d409056e55402757864ba`。
- `make verify TASK=AGENT-UI`：首次109PASS/1FAIL。唯一失败是旧测试未先启动
  Run便构造受保护输入；仅修测试启动前置，所有原约束断言保留，单独复验1/1 PASS。
  不重跑109个已通过项，原注册FAIL完整保留，兼容覆盖110/110、无剩余失败。
- 最终指纹`4f4489bc6f5f219134cdc8f5bd008904abb2a1d67efbdce039042c713b8ad20f`。
  注册门之后唯一变化是上述测试前置；应用源码未变。重建修复前文件可精确恢复
  原注册指纹，见`source-repair-proof.json`，不伪称旧注册门在新指纹重跑过。
- 两集合不重叠，共162个工程用例覆盖通过；浏览器57项覆盖通过，包含最终定向
  分析16/16与交付13/13，以及原项目20/20、资源8/8；未重复无变化的已通过脚本。
- 全局lint、typecheck（238源码文件）及差异检查通过；真实一次性PG与Docker、
  受控模型和GitHub端点，不计为真实供应商/平台质量验收。

确认本机无queued/running任务后，仅通过原迁移器安装0045并重启owned服务。
`http://127.0.0.1:18080`首页、health、ready、新接口与带内容哈希的资源均正常；
历史迁移checksum、模型调用条数、已核算/预占合计不变，旧AGENT_STALLED及unknown
保留，主动追问仍可沿Thread继续。独立GitHub写凭据尚未配置，界面显示可纠正的
配置路径，不创建真实PR。本轮真实模型调用与GitHub写入均为0，H12成绩及全局
ENGINEERING_INCOMPLETE保持。原失败与首次检查脚本Decimal/JSON比较失配均保留。

发布范围为普通文本新增/修改，最多16文件、128 KiB Diff；其余变更可导出补丁，
不会伪造可发布。Draft PR须在GitHub审查、转ready并实际通过CI后单独批准合并。
GitHub REST只能原子绑定head，无法原子冻结base；发送前复查目标/权限/CI，
结果父提交与获批版本不符记unknown，不宣称已准确合并或自动重试。

2026-11-13界面补充：仓库/模型/模式顶部栏吸顶，窄屏仍显示模型名；最近对话新增
运行圆圈，随SSE及原轮询更新，等待/暂停/终态移除，尊重减少动画设置。桌面与手机
滚动、选中与后台会话状态的46项浏览器检查及lint/typecheck通过，见
[专项记录](accept/TOPBAR-HISTORY-20261113.json)。仅修改静态前端，本机已提供新版
资源，无需重启或迁移，无模型调用/外写；原后端验收不重复、不改写。
