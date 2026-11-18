# H12 新候选完整开发准备与真实复测

状态：IN_PROGRESS。起点86e1717；0308789的最终128项独立工程PASS保留，原质量
0PASS/3FAIL/9NOT_RUN、独立持出模型12NOT_RUN、MVP未放行不变。

## 本轮任务

1. 仅从公开goal/acceptance/源文件编写三道原失败题的正式公开probe；补齐Go未知
   旗标题的同类公开复现缺口。绑定正式materializer产出的原树（含冻结公开tests），
   固定资产/计划/公开依据/资源哈希，不读取私有Judge或持出实现。
2. 修正准备阶段将bugfix原suite一律要求绿色的矛盾：目标probe仍须严格断言red；
   原suite仅接受冻结且声明的公开缺陷测试失败，import/collection/基础设施错误
   不得冒充red；候选终态仍须原suite全绿。Go复现用现有镜像Pythonrunner执行
   固定内嵌Go小程序，临时文件在scratch，不改原Go源或测试。
3. 完整12题准备执行实际runtime、公开回归、适用复现和准确启动计划；run_app
   未实现入口明确AWAITING_IMPLEMENTATION，不伪称原树目标服务已就绪。
4. 新建dev-only冻结桥接入v2评分器，绑定计划/完整准备/源码/profile/镜像，避免
   开发控制器读取持出清单。子集READY不能替代完整准备；缺失或变化均在派发前拒绝。
5. 新增定向工程验证，首次失败保留，修复仅复验失败；不自动重跑已PASS128项。
   源码稳定后，在新候选执行完整12题零模型准备并冻结。
6. 前置通过后复测原三题，再根据冻结10/12门槛决定其余开发题与独立持出评分。
   每次新尝试独立记录，不覆盖旧FAIL，不更改任务资源/阈值/判定器。

原共享H12 PG账本与Engine仍唯一预算权威（12USD，总调用限制和每题冻结资源
不变）；不得新建另一份真实预算或重发unknown。凭据不入日志；真实模型仅在
新冻结与完整准备验证后派发。只操作本轮owned PG/Docker对象，在线主线与SA
接线不动，不推送、不启动H13。正式完成情况、费用、未完成项随后追加。


## 已确认的新衔接缺口与修正

- 原准备/独立判定器显式scratch256MiB，实际Harness默认64MiB，Go编译因此可
  在准备后失败。新public-runtime-v1显式将新候选两端对齐256，原64及旧profile
  序列化保持历史。模型/工具/Token/费用/期限上限不变，不伪称sandbox资源没变。
- 只读原树上的pytest cacheprovider会触发warning-as-error。新investigate命令
  在保留镜像原PYTEST_ADDOPTS的同时追加cache_dir=/scratch/pytest-cache，纳入
  环境哈希；不改项目测试。Click失败attempt保留，定向复验已取得严格公开断言红。
- Got feature原profile将tsc当作测试，完整准备却另跑npmtest。新候选显式拆成
  public_build_argv与public_test_argv，编译exit0仍为0tests，不能代替真实测试。
  原协议默认空字段省略，保持旧profile哈希；实际服务、PG冻结、权限与恢复共用
  固定build ID、精确argv和purpose，不给任意shell入口。
- 完整准备的公开缺陷红分支已补exit1检查；真实FAILED日志搭配exit137不得就绪。
  修前负例和只复验该失败节点的证据分别保留。

正式开发模型前置尚在进行中。四份公开计划（3Python+1Go）已绑定正式物化原树，
均已真实probe红；Go原公开回归195通过、两Requests原回归通过、Click声明缺陷
公开测试原树1失败。它们是原树工程事实，不是模型修复完成结果。


## 实际准备进展

工程代码提交678d21e，源码指纹
e3e2d7b779674a4a5cedce07d5ff4bc30c351d5bbbabd8550443938391c29759。
70项新增工程节点分阶段有通过证据（不伪称最终整套重跑）；全局Ruff/mypy245通过。
源码稳定后，首次subset-v1因Click计划绑定Git原树，而正式_source优先选已有
冻结V35快照，被strict tree校验拒绝，0模型。核对公开依据源码/tests字节全相同，
仅原树文件清单不同；旧计划保留，新增snapshot-v2 manifest，source选择算法不改。
修订preparation-v2绑定四计划；subset-v2真实4/4 PUBLIC_SUBSET_READY，源码前后同指纹。
full-dev-v1实际12题零模型预检进行中，未封正式付费冻结。

真实H12账本只读复核：305次调用、保守1316981micros/12000000，预占0、未知0。
现有原共享state只读绑定复用，没有新建第二真实预算数据库；未修改在线主线。


首轮完整full-dev-v1在e3e2...源码上实跑12题，8READY/4NOT_RUN，新增模型0；
源码前后指纹相同，完整失败保留。两Got由于XO扫描.git遇原000空保护目录而拒绝，
Click style由于旧tests/test_utils.py不存在，Click multifile由于公开新功能目标原树红
被错作环境失败。已分别修正：保护目录仍遮掉真实内容、仅空只读目录可遍历，
文件遮罩000/权限/保护cwd/源inode不变；refactor只选原已声明全回归，不造新命令；
feature原树红须冻结公开inventory精确hash、真实FAILED路径、exit1与完整日志。
新4个工程节点分阶段通过，旧70/128未整套重跑。共用Broker挂载逻辑已变化，
因此必须在最后候选重新执行完整12题物理准备，不把旧8READY冒称新源码执行结果。


e952bf5候选（source93336b...）重新实际准备：subset-v3四题READY；full-dev-v2
持续执行中，先前4个阻塞已不再出现。另以真实modify_test+仅src文件可写的窄
scope诊断发现pytest缓存追加此前只覆盖investigate，实际服务根目录仍只读，
会再次PytestCacheWarning。诊断0模型，日志保留；等待该完整报告完成保留同源
结果后，将覆盖modify_test并做真实PG/容器窄范围服务用例，再重新冻结和完整准备。
此处不以只读环境READY冒充真实Agent可执行，也未派发付费调用。


93336b...源码full-dev-v2最终12/12 FULL_DEV_READY、source前后相同；subset-v3
也4/4 READY。预检均0模型。此后针对实际modify_test窄scope的真实服务补丁将
缓存策略同时覆盖investigate与modify_test；新1PG/Docker节点首次PASS，原树/最终
suite均1pass、publicprobe红→绿、真实缓存路径为scratch，5次受控适配器调用/6工具，
实际provider0。investigate与publish环境hash保持原值，仅modify_test变更。
为保持实际模型候选与完整准备同源，最后源码需重新冻结并再次生成两份完整报告；
不将前一源码的12/12抄成最后源码结果。工程新增累计75不同节点分阶段通过。


## 本轮最终接收

最后实现1e68aef/source494ad6...实际subset-v4四题READY、full-dev-v3十二题READY，
最终FROZEN3190e0...并成功复用原PG初始化新实验。首题第一笔派发未取得可信用量，
因此新质量1UNKNOWN/11NOT_RUN，后续付费停止。账本306次尝试（含1UNKNOWN）、
已结算保守US$1.316981、预占US$0.069831；不是费用上限耗尽。操作启动脚本遗漏
代理变量已修正，但旧请求无supplier ID/usage，不能凭连通性诊断冲销预占。
完整因果、等待供应商对账证据与下一步见
[本轮接收报告](H12_PUBLIC_DEV_ACCEPTANCE_20261113.md)及
[机器记录](accept/H12-FULL-DEV-REPAIR-20261113/accept.json)。本轮不把环境READY
或新工程PASS写成模型通过，原历史FAIL、MVP未放行及独立持出NOT_RUN保持。
