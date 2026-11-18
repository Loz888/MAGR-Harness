# Codex CLI Harness H11 实施任务卡

当前独立复验：PASS_AFTER_INDEPENDENT_RECHECK（仅 H11 工程范围），见 [独立报告](accept/HV4-H11/independent-recheck-20261111T134513Z.json)。

实施交付历史状态（保留）：PENDING_INDEPENDENT_RECHECK（工程门 PASS）。日期：2026-11-11，Asia/Shanghai。本轮仅实施 HV4-H11；实现、真实
工程验证、保存证据并本地提交后，停在 PENDING_INDEPENDENT_RECHECK。不启动 H12，不推送。

## 1 仓库与前置核对

| 项目 | 本轮可信值 |
| --- | --- |
| 仓库 / 工程根 | `/home/developer/magr-harness` / `v3` |
| 本轮起点 HEAD | `9b52190f43dd7573b65de486cbd23a9e3e87a649` |
| H10 已审核源码 | `c140dadf5aff517527c3c5e107843e215cc76556` |
| H10 标准源码指纹 | `38a02725fe2a5b5967e2604951a0749d69aeea1c5b1640e6dc65901d10d10de8` |
| H10 独立结论 | F01/F02 与列明回归 PASS；其他报告发现未关闭 |
| 环境 | Python 3.12、现有 `.venv`、`uv run --frozen`、已有固定 Docker 镜像 |

已核对 [H10 独立报告](accept/HV4-H10/repair-c140dad-independent-recheck-20261111T120756Z.json)
与外部副本一致，68/15/281 项及12项独立检查通过；`c140dad..9b52190` 无源码变化。
本卡不把这些历史计数写成本轮 H11 结果。

读取根与 `v3/AGENTS.md`、`v31-upgrade/TASK_QUEUE.md`、ADR0001，以及本目录 README、
TASKS 的 H11、HARNESS_CONTRACTS、CODING_LOOP、LONG_CONTEXT、COMMAND_EXECUTION、
SCORING_PROTOCOL 和 [恢复协议](RECOVERY.md)。核对新迁移编号与 Engine 所有权后实施。
不得 reset 或清理用户改动；保留 H10 原 FAIL、补充报告与非本轮创建的未跟踪复验记录。

~~~bash
cd /home/developer/magr-harness
git status --short
git rev-parse HEAD
git log -5 --oneline
git diff c140dad -- v3/src v3/tests v3/config v3/scripts v3/migrations v3/web v3/pyproject.toml v3/uv.lock
cd v3
uv run --frozen python --version
~~~

## 2 范围与权威

把现有首页/API 实际使用的 HarnessCodingService 接入既有 Engine/PostgreSQL，不新增
平行业务写者。任务、真实 Thread/Run 身份、epoch、审批范围、预算、事件、收件箱及
checkpoint 选择由 PG 权威保存；CAS/manifest/摘要只保存内容和校验材料。

恢复仍是同一 Run，获取新 epoch，保留原截止时间与账本。统一循环 continuation 保存
原计划、原始 ModelRequest/历史、基线与回执、验收缺口、失败/修复计数、指令水位。
恢复原批次不能改 ID 重新发模型；部分工具已知时复用原结果，只由对账后的可信树解释
旧请求树变化。模型发送未知保留预占并阻止重发；prepared 与 known 必须区别处理。

编辑在副作用前持久化 intent/前后图像，真实多文件部分应用可对账而不重复；冲突不覆盖。
命令沿用 H04/H06，受管容器绑定任务/Run/operation/epoch/workspace，保存原日志后只清理
完整身份匹配的 owned 容器。原日志保持原生成租约，不因重启重绑新租约。
接管与文件 mutation 遵守相同锁顺序，旧 Worker 不能编辑、执行、结算、写事件或完成。

不读取 holdout/隐藏判定器内容，不改冻结任务和阈值，不执行 H12，不增加项目模型可调用
的 subprocess/Docker/socket/eval 入口。开发者独占 PG/故障进程脚本仅供工程验证。

## 3 必须实跑的验收

| 场景 | 通过条件 |
| --- | --- |
| 服务真实 SIGKILL 与新进程恢复 | 新 Engine 重开同 PG，Run 不变，epoch 增长，持久选择可核对 |
| prepared / known 模型 | prepared 同请求继续；已知响应和结算后崩溃复用，无额外供应商发送 |
| unknown 模型与缺回执 | 预占保留、账本可审计、不退款不盲发、不冒称 completed |
| 补丁前后与多文件部分应用 | 对账后树正确、每项物理变更一次；第三种内容冲突关闭 |
| 半工具序列与 known 读取/交互 | 原顺序和结果复用，原模型请求完整，已答问题不重复等待 |
| 命令启动/输出保存前后 | 日志原 lease 来源不变，owned orphan 被收回，其他容器不受影响 |
| epoch 与终态树 | 旧 Worker 各类写入被拒绝；验证/清单/diff/context/最终树一致 |
| inbox 与 checkpoint | 水位提交原子、断点前后指令只进入恢复目标一次；终态接收竞态有专门证据 |
| PostgreSQL 真实停止与启动 | 只操作本轮专用实例，记录不可连接及恢复，任务/预算/事件不丢失 |

受控模型只证明故障语义与接线，不替代真实 Agent/模型能力门。用户已批准专用 PG 停启和
相关测试，并允许必要真实付费调用；本轮计划与实际故障验证使用 controlled 模型，付费为0。
若后续确需真实付费，保存具体目的、预算、发送次数与费用，不能从环境开关推定授权。
不启动共享 PG 的故障测试，不输出 `.env`、令牌或连接凭据。

## 4 检查、证据与停止点

实现真实测试后注册 `HV4-H11`，沿用 `make lint`、`make typecheck`、
`make verify TASK=HV4-H11`；运行 H00–H11 聚合与旧上下文15项相关回归。
测试计数以本轮 JUnit 为准，不硬编码成功数量。PG 必须是真实隔离 PostgreSQL，
不得用 SQLite/fake PG/skip 冒充；Docker 故障测试串行，使用本轮唯一 `/tmp` 路径。

每次 attempt 保存完整命令、cwd、退出码、起止 HEAD、标准源码指纹、JUnit、原始/脱敏
日志、CAS hash、Run/epoch、操作/费用状态与 owned 清理结果。保留失败与重跑，原敏感
日志只在本地受限保存；make verify 更新最新指针前保存旧指针，历史 attempt 不覆盖。

恢复使用既有 `/api/v3/harness/tasks/{task_id}/resume`，命令示例见恢复协议；不增加业务
执行入口。最终交付 H11 源码/迁移/测试、故障矩阵与 accept 记录，工程门、live 门、
未关闭发现分别记录。README/TASKS/plan 仅推进到实际阶段；实现方不得写独立 ACCEPTED。

只暂存本卡拥有的文件并本地提交，确认原未跟踪报告仍原样保留；输出提交、标准指纹、
实跑计数、失败/未跑项与独立复验入口后停止。不推送、不创建 PR、不合并、不部署、不启动 H12。
