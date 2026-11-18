# Codex CLI Harness H10 实施任务卡

状态：NOT_STARTED。交接日期：2026-11-11，Asia/Shanghai。

本卡交给本地 Codex CLI 后，下一张实施任务是 HV4-H10：长任务计划、上下文与分页工具。目标是在现有编码循环里接通有界上下文、多次压缩、原文重读及可持久工作摘要。实现、验证、保存证据并本地提交后，停在 PENDING_INDEPENDENT_RECHECK；独立验收使用同目录 CODEX_CLI_H10_REVIEW_TASK.md。

本次桌面会话只生成交接卡，没有开始 H10 实施。本卡不是付费模型调用、远端写入、推送或后续卡片的许可。

## 1 仓库和可信起点

| 项目 | 已核实的值 |
| --- | --- |
| 工作仓库 | /home/developer/magr-harness，WSL Ubuntu-24.04 |
| Python 工程根 | /home/developer/magr-harness/v3 |
| 交接前 HEAD | 5e0158e6c46e4bfec3e7eacef34a7571be628494 |
| 已复验源码提交 | 096300eed8946086f99e331ae4c8c9c733108ff9 |
| 标准源码指纹 | 915092c7504d95c31b88ea1e8fdb10722506d96cf31c841647944a7da9786331 |
| 起点工作树 | 生成任务卡前干净；任务卡自身可能另有纯文档提交 |
| 环境 | Python 3.12、v3/.venv、uv run --frozen、固定 R03 Docker 镜像 |
| H10 注册验收 | 尚未注册；实施真实测试后再添加 |

先核对本机状态，不要 reset、清理用户改动或把 GitHub main 当作本地实现版本：

~~~bash
cd /home/developer/magr-harness
git status --short
git rev-parse HEAD
git log -5 --oneline
git diff 5e0158e -- v3/src v3/tests v3/config v3/scripts v3/migrations v3/web v3/pyproject.toml v3/uv.lock
cd v3
uv run --frozen python --version
~~~

如果 HEAD 只比 5e0158e 多任务卡文档提交，可以继续；如果源码已变化，先检查并记录新基线及归属，不覆盖他人工作，不沿用本卡指纹声称新版本通过。uv 找不到时检查 /home/developer/.local/bin；不得为此改用未锁定环境。

## 2 开工前必读

以下路径均相对于仓库根；文档中的历史命令、许可和状态不是本轮新的授权。

1. AGENTS.md、v3/AGENTS.md、v31-upgrade/TASK_QUEUE.md、v3/docs/adr/0001-v31-boundaries.md。
2. v3/docs/harness-v4/README.md、TASKS.md 的 HV4-H10、ROADMAP.md、ACCEPTANCE.md。
3. v3/docs/harness-v4/HARNESS_CONTRACTS.md、CODING_LOOP.md、HANDOFF_TO_CODEX.md 第9节。
4. v3/docs/harness-v4/RECORD_RECONCILIATION_20261111.json。
5. v3/docs/harness-v4/accept/HV4-H09/workspace-hash-recheck-20261111.json。
6. v3/docs/harness-v4/SCORING_PROTOCOL.md，以及相关卡 accept 中仍适用的残余限制。

H00 已冻结的任务、判定器与评分阈值保持不变。不得把 holdout 清单、隐藏判定器或答案放入被测 Agent 的上下文；接手调查无需打开它们的内容。

## 3 已完成的工作及继续适用的边界

- H00/H01 已有接收记录；H02 已外部复核通过，但 live_model_gate 仍为 PARTIAL。
- H04–H09 各有历史提交的独立 ACCEPTED 记录。当前共享哈希修复另有独立 PASS，不能把它外推成整个产品或所有 live 门通过。
- 096300e 修复了宽泛 .coverage 前缀吞掉配置的问题。两轮 H04–H09 各130项、H00–H09 各213项，45项独立探针、两种内存变异检测、原 H06 稳定性10/10、lint/typecheck 及 H06/H09 注册验收均通过。这些是已归档的历史实跑结果，不是 CLI 本次运行结果。
- 单一排除规则仍为 contracts.is_workspace_artifact。精确 .coverage 和完整匹配 .coverage.<host>.<数字 pid>.<nonce> 的数据文件被排除；.coveragerc 等配置、coverage.xml、htmlcov 参与复制和树哈希。临时报告放入 .harness-tmp。旧规则物化的工作区需要从源重建，不可静默重签旧 manifest 或沿用旧验证。
- H03 历史 verify 覆盖 a803784 / 0bbe935，25项、源码指纹 e0f2ced7cb8ab0deecbd811aebe30cadcfe189bc91b12fb0eefbb2ef3eea09eb。最终独立接收人的元数据未归档，保留 PENDING_EXTERNAL_REVIEW。本卡不通过修改状态来填补此缺口；若发现实际工作区缺陷，保留复现并先修阻塞问题。
- API 仍有零 Thread/Run UUID、固定初始 epoch、内存预算账本等集成限制；完整一致恢复归 H11。H10 不能把摘要、事件文件或检查点变成第二个授权、预算或业务状态真相。
- 容器内 pytest/Node 的真实运行与依赖准备仍有未验收项；H07 弱化检测仍是启发式；多用户、跨进程总线、真实模型交互和 H12 均未因本轮复验而通过。

## 4 H10 实现范围与代码入口

先做简短代码调查，写出接线方案和不变量，再直接实施本卡，不把交付停留在设计说明。

| 入口 | 当前状态与本卡工作 |
| --- | --- |
| v3/src/mgar_v3/harness/adapters.py 的 SimpleContextManager | 仅提供基础 system/user 消息；以可替换的 H10 适配接入现有组件，保留旧端口兼容 |
| v3/src/mgar_v3/harness/coding_loop.py | 当前只在启动时调用 context.build，然后累加 history；在后续模型安全点执行有界构建和必要压缩，而不只替换初始化对象 |
| 同文件 TaskPlan、LoopCheckpoint | 持久化计划及工作摘要，绑定任务、目标/验收修订、工作树、证据和上下文版本 |
| v3/src/mgar_v3/context/builder.py | 复用 ContextBuilder、ContextRequest、ContextBundle、指标和 revision 校验 |
| v3/src/mgar_v3/context/compactor.py | 复用 compact_observations、ContextInvariants、validate_compaction、ArtifactPaginator |
| v3/src/mgar_v3/evidence/artifacts.py | 复用 CAS 原文；索引和摘要不能替代可取回的原文 |
| v3/src/mgar_v3/harness/service.py 的 CodingToolRegistry 和循环组装 | 把分页、重读、搜索缓存接成模型真实可调用的工具，并实际接入 API 使用的运行时 |
| v3/src/mgar_v3/repository_analysis/memory_loop.py、v3/src/mgar_v3/memory/ | 复用已有检索与失败记忆约定，避免新增平行记忆系统 |

交付至少包括：

1. **结构化工作状态。** 保留用户目标、任务类型、必需验收项与缺口、关键决策、失败签名、补丁/产物哈希、当前 tree、待收回命令、待答问题及权限/预算引用；字段的权威来源与版本明确。
2. **有界上下文。** 压缩前后检查不变量，正确保留工具调用与结果的对应关系。旧 revision/目标/树的上下文不得覆盖新结果；中途指示和压缩共享清晰的版本规则。
3. **可取回原文。** 长源码和日志保存到 CAS，提供模型可调用的分页与按需重读；搜索缓存必须绑定 workspace/tree/查询条件，树变化后失效或重新验证。
4. **权限仍逐次校验。** 分页 cursor、artifact hash 和摘要不授予访问权。CAS 页面读取需要核对任务/工作区归属和当前授权，不得凭猜中的 hash 读取其他任务或保护内容。不要借新工具绕过现有 Registry/Broker。
5. **持久化与度量。** 版本化计划、摘要及上下文引用可读取和校验，记录 token/字节、压缩次数、原文引用与损失检查结果。涉及重启后账本、进程、未知调用的完整对账留 H11，不用简单 JSON 重载冒充已解决。

按现有公共端口做加法扩展；如确需改契约，说明版本兼容与调用方迁移。共享 Engine、API 或 schema 的变动必须先核对所有权；迁移编号以当时实际仓库为准，不套用旧交接中的空闲编号。

## 5 必须验证的行为

| 场景 | 通过条件 |
| --- | --- |
| 强制多次压缩 | 同一任务至少触发三次真实压缩；被移出窗口的源码/日志能由模型经分页工具再次获取，并完成可验证任务 |
| 不变量 | 目标、必需验收项、失败记录、tree、待收回命令及权限/预算引用保留；伪造或丢失关键项不能生成完成结果 |
| 版本变化 | 指令、验收修订或树变化后，旧上下文/缓存被拒绝或正确重建；保持 H09 旧模型结果整体丢弃语义 |
| 分页完整性 | 分页合并与原文逐字节/有效文本一致，包含 UTF-8 边界、非法 cursor、篡改 hash、缺失产物、越范围访问的负例 |
| 摘要注入 | 原文或摘要中的授权声明不能扩大工具、目录、网络、发布或费用能力 |
| 小任务开销 | 同一小任务不压缩时，不因记忆流程多发模型请求；不能调用付费模型来“验证”这一工程性质 |
| 搜索缓存 | 同一版本可复用；改树后不沿用旧结果或旧文件摘要，不把缓存命中变成新的进展证据 |
| 持久化 | 计划/摘要/revision/原文引用保存再读取一致；损坏或不匹配失败关闭，不重签旧树和旧许可 |
| 接线与回归 | API 所用循环确实走新上下文和分页工具；现有增量编辑、弱化守卫、暂停/取消/inbox、权限/预算及哈希回归保持通过 |

三次压缩是本卡为“多次”设置的最小确定性工程用例，不改变 H00 冻结的任务集、资源上限或 H12 发布阈值。受控模型只能证明接线与契约；live 三门未运行仍记 NOT_RUN。

## 6 验证和证据

新增真实的 tests/test_harness_v4_h10.py，并在 config/verification.json 注册 HV4-H10；禁止空门禁、跳过失败、删除旧测试或缩小 lint/typecheck 范围。

~~~bash
cd /home/developer/magr-harness/v3
make lint
make typecheck
make verify TASK=HV4-H10

# 这些旧组件的相关回归需调查依赖后运行。
uv run --frozen pytest -q tests/test_v33_context_compaction.py tests/test_r4_context.py

# H00–H10 聚合回归；计数必须以实际 JUnit 为准。
uv run --frozen pytest -q \
  tests/test_harness_v4_h00.py tests/test_harness_v4_h01.py \
  tests/test_harness_v4_h02.py tests/test_harness_v4_h03.py \
  tests/test_harness_v4_h04.py tests/test_harness_v4_h05.py \
  tests/test_harness_v4_h06.py tests/test_harness_v4_h07.py \
  tests/test_harness_v4_h08.py tests/test_harness_v4_h09.py \
  tests/test_harness_v4_h10.py
~~~

每次运行用新 evidence/harness-v4/HV4-H10/<UTC时间与用途>/ 目录，保存完整命令、cwd、退出码、起止 HEAD/标准源码指纹、JUnit、原始日志、模型/工具调用数、费用及真实/受控/未运行分类。标准源码指纹使用 scripts/verify.py 的 source_fingerprint，其他指纹必须另名记录。

Docker 回归串行运行。pytest 使用唯一的 /tmp/magr-h10-<唯一值> basetemp，不复用已有目录；H06 helper 会向上调整目录权限直到 /tmp，不能将 basetemp 放在仓库或证据目录。不要清理其他任务的临时目录或容器。

如测试依赖 PostgreSQL，用隔离的真实 PostgreSQL；不得用 SQLite、fake PG 或跳过用例冒充集成通过。读取配置时不要输出 .env、token 或凭据。缺环境、权限或依赖时，保存可复现回执与等待项，不伪造通过。

make verify 会更新 evidence/HV4-Hxx/acceptance.json 的最新指针；先保存旧指针副本，原 attempt 目录永不覆盖。保留失败、基础设施错误及重跑，不能只汇报最好成绩。现有哈希独立探针在 evidence/harness-v4/hash-recheck-20261111T084848Z/final/，重用前先阅读脚本和参数，记录新运行。

## 7 完成产物和停止点

- 可工作的 H10 接线、相关回归和必要的版本兼容说明。
- 新的 H10 设计/使用说明及 accept/HV4-H10/accept.json；工程门如实记录，review_status=PENDING_INDEPENDENT_RECHECK。
- 当前源码提交与标准指纹、完整证据路径、逐项验收映射和残余限制；README/TASKS/plan 的状态只更新到实际阶段。
- 本地提交，只暂存本卡文件；仓库缺提交身份时使用明确的代理身份并仅对单次提交生效，不冒用历史作者或改全局配置。
- 输出变更、命令/计数、失败与未跑项、提交、指纹和下一位独立验收者的入口，然后停止。

一次只完成 H10；不启动 H11/H12，不推送、不创建 PR、不合并、不部署。付费模型、真实外发和渠道操作需用户另行明确授权，不沿用历史试验额度。代码中的项目执行仍只走 H04 Broker/H06 CommandSession，不新增旁路 subprocess、socket、docker 或 eval 执行入口；这不禁止开发者通过既有 Make/pytest 命令运行工程回归。

H10 的工程 PASS 不能由实现方升级为独立 ACCEPTED。独立复验应在另一会话读取 CODEX_CLI_H10_REVIEW_TASK.md 并针对已冻结的源码执行。
