# Harness V4 任务须知（交接给 Codex）

> 本文档只陈述可核对的事实、约束与未闭合项。凡是仓库记录不足以证明的内容，标为“未核实”。
> 信息时点：2026-11-11（UTC+8）。本文档自身会以一次独立本地提交加入仓库，因此接手时
> 请以 `git rev-parse HEAD` 为准；该提交会使下面记录的 HEAD 与 ahead 数各前进 1。

## 1. 项目坐标

| 项 | 值 |
| --- | --- |
| 仓库路径 | `/home/developer/magr-harness`（WSL2 Ubuntu-24.04） |
| Python 工程根 | `/home/developer/magr-harness/v3` |
| 当前 HEAD（生成本文前） | `98d3042`（`Unify workspace tree hash exclusion rule`） |
| 上一提交 | `0878307`（`Record H09 independent recheck PASS`） |
| 分支 | `main` |
| 领先 `origin/main` | 75 个提交 |
| 远端状态 | 未推送（本地 ahead；未执行任何 `git push`） |
| 工作树 | 干净（`git status --short` 无输出） |
| 运行环境 | Python 3.12；`v3/.venv`；`uv run --frozen`；Docker 固定 R03 镜像（H06 容器执行） |
| 未使用 | HV4 验收不依赖 PostgreSQL/Redis；live/付费模型调用未运行 |

## 2. 卡片进度

### 2.1 H04–H09

状态、门与复验覆盖 commit 均以各卡 `accept.json` 为准。`accept.json` 比 `TASKS.md`
中的历史状态行新；`TASKS.md` 里 H02/H05/H06/H07 的“待复验/等待复核”文字未同步
（见 2.3）。

| 卡片 | 状态 | engineering_gate | security_gate | 独立复验覆盖 commit | 验收记录 | 关键证据 |
| --- | --- | --- | --- | --- | --- | --- |
| HV4-H04 能力许可与执行 Broker | ACCEPTED / PASS_AFTER_INDEPENDENT_RECHECK | PASS | PASS | `c707d69` | `docs/harness-v4/accept/HV4-H04/accept.json` | `evidence/harness-v4/HV4-H04/independent-recheck-20261111/`；原始失败 `evidence/harness-v4/HV4-H04/recheck-20261111/original-failure/` |
| HV4-H05 版本化增量编辑 | ACCEPTED / PASS_AFTER_INDEPENDENT_RECHECK | PASS | PASS | `a757a6b` | `docs/harness-v4/accept/HV4-H05/accept.json` | `evidence/harness-v4/HV4-H05/independent-recheck-20261111/`；另见 `evidence/HV4-H05/independent-recheck-20261111T011025Z/` |
| HV4-H06 命令/依赖/测试/应用进程 | ACCEPTED / PASS_AFTER_INDEPENDENT_RECHECK | PASS | PASS | `e52347f` | `docs/harness-v4/accept/HV4-H06/accept.json` | `evidence/HV4-H06/independent-recheck3-20261111T1100Z/`（前次 `independent-recheck-20261111T1000Z/`） |
| HV4-H07 统一编码循环与弱化守卫 | ACCEPTED / PASS_AFTER_INDEPENDENT_RECHECK | PASS | PASS | `2114a87`；复验后补丁 `cef7482` | `docs/harness-v4/accept/HV4-H07/accept.json` | `evidence/harness-v4/HV4-H07/independent-recheck-20261111T1200Z/`；补丁后第二轮 `evidence/harness-v4/HV4-H07/independent-recheck2-20261111T1400Z/` |
| HV4-H08 API/页面与变更交付 | ACCEPTED / PASS_AFTER_INDEPENDENT_RECHECK | PASS | PASS | `fadea5d` | `docs/harness-v4/accept/HV4-H08/accept.json` | `evidence/harness-v4/HV4-H08/independent-recheck-20261111T1400Z/` |
| HV4-H09 事件流与运行中交互 | ACCEPTED / PASS_AFTER_INDEPENDENT_RECHECK | PASS | PASS | `7d8f0ab` | `docs/harness-v4/accept/HV4-H09/accept.json` | `evidence/harness-v4/HV4-H09/independent-recheck-20261111T1600Z/` |

H09 之后、尚未被独立复验的提交：

| commit | 内容 | 复验状态 |
| --- | --- | --- |
| `0878307` | 将复验方生成的 H09 `accept.json` 纳入版本库，未改结论 | 文档提交，无需功能复验；内容已 JSON 校验 |
| `98d3042` | 统一工作树哈希排除口径（`contracts.is_workspace_artifact`），并补 H06 两个回归测试与契约说明 | **尚未独立复验**（见 3.1） |

当前各卡测试数量（`pytest --collect-only`，合计与回归一致）：H04=27、H05=14、H06=27、
H07=29、H08=19、H09=10，H04–H09 合计 126；H00=10、H01=18、H02=30、H03=25，
H00–H09 合计 209。

### 2.2 H00–H03（历史状态来源）

| 卡片 | accept 中的状态 | engineering | security | live_model_gate | commit | 是否需要补核 |
| --- | --- | --- | --- | --- | --- | --- |
| HV4-H00 | PASS | PASS | PASS | NOT_RUN | `ede3bc6` | 无待办记录；`review_status` 为空 |
| HV4-H01 | PASS / ACCEPTED_BY_SUPER_AGENT | PASS | PASS | NOT_RUN | `418d0c2` | 无待办记录 |
| HV4-H02 | PASS / PASS_EXTERNAL_REVIEW | PASS | PASS | **PARTIAL** | `169925f` | live 门仍为 PARTIAL；后续 live 覆盖情况未核实 |
| HV4-H03 | PASS / **PENDING_EXTERNAL_REVIEW** | PASS | PASS | NOT_RUN | `a803784` | accept 自记待外部复验；`evidence/HV4-H03/h03-independent-review/` 存在，但是否构成独立复验结论**未核实**，建议补核 |

来源均为 `docs/harness-v4/accept/HV4-H00…HV4-H03/accept.json`。`TASKS.md` 对 H02 写
“待外部复验”，与 H02 accept 的 `PASS_EXTERNAL_REVIEW` 不一致；以 accept 为准，文档
状态待同步。

### 2.3 每卡核心交付与定义性不变量

- **H04**：统一能力租约 `harness-capability-lease-v1` + `IsolatedExecutionBroker`。
  不变量：每次执行入口逐次校验 actor/workspace/repository/epoch/cwd/命令/预算/撤权/
  有效期；读写范围取 `lease ∩ grant` 最窄值，超时取三者最小；容器非 root、只读根、
  `network none`、cap drop、cgroup 限额、只挂批准范围；`.git`/`.mgar`/`.env` 等保护
  路径在读写根内也被掩码；宿主 symlink scope 在创建容器前拒绝；allowlist 只约束
  argv[0] 入口。
- **H05**：版本化增量编辑。不变量：`replacements` 以文件前像 sha256 + 片段 sha256 +
  唯一上下文/occurrence 定位；多文件整组原子应用，失败回滚并保留 `candidate()`；
  `PatchReceipt` 记录前后 sha256 与真实工作树 Diff 的 sha256；树变化报
  `WORKSPACE_STALE`，全文件 pre-image 不匹配报 `WORKSPACE_EDIT_STALE`，片段 pre-image
  不匹配报 `PATCH_STALE`。
- **H06**：命令与服务生命周期。不变量：`CommandSession.run/poll/read_log/wait/cancel/
  close` 与 `ServiceLaunch` 全部复用 H04 Broker，不新增执行入口；deadline/cancel 用
  `docker rm -f` 终止整个容器；日志分页与截断显式报告；退出 0 但无测试/全跳过/无法
  解析不判通过；pytest/node 缺失返回可操作 `waiting_approval`。
- **H07**：统一编码循环。不变量：计划与 `checkpoint_tree` 绑定，树不一致返回
  `HARNESS_PLAN_STALE`；模型只能追加 required 验收项，不能替换或降低；进展只看工作树
  变化与新命令证据；连续两轮无进展 `blocked`；弱化守卫按文件统计裸 `assert` 与
  `self.assert*`，测试文件/函数/断言数或 `tests_run` 下降不得完成。
- **H08**：Web/API 接通真实编码。不变量：`/api/v3/harness/*` 版本化入口；变更项
  `before_sha256` 直接上报基线摘要（F1，不做二次哈希）；`task_id` 全匹配
  `^h08-[0-9a-f]{12}$`（F2，服务层与 HTTP 层都校验）；Diff 来自真实工作树；普通
  编辑不需要 GitHub 发布权限。
- **H09**：持久事件流与运行中交互。不变量：`harness-stream-v1` 追加式日志 + 单调
  持久 cursor；`after`/`Last-Event-ID` 分页无重复无缺口；reasoning 类键落盘前递归
  剥离；inbox `received/applied/ignored` + `message_id` 幂等，同 id 不同内容 409；
  `before_model`/`after_model` 安全点生效，旧模型结果整体丢弃；取消回执只声明投递
  （`refund=not_promised`），实际停止以 Runner/容器回执为准。

## 3. 未闭合项（按优先级）

### P0：`98d3042` 哈希口径修复尚未独立复验

- 现状：复验方确认的缺陷（`broker.workspace_tree_hash` 只跳过 `.git/.mgar`，而
  `workspace._scan` 跳过 `__pycache__`）已修复；同一 `contracts.is_workspace_artifact`
  被 `workspace._scan`、工作区复制/搜索与 `broker.workspace_tree_hash` 共用。
- 影响面：相较修复前，broker 侧新增排除 `__pycache__`/`*.pyc`/`*.pyo`、
  `.pytest_cache`、`.mypy_cache`、`.ruff_cache`、`.venv`、`node_modules`、`.tox`、
  `.harness-tmp`、`htmlcov`、`coverage.xml`、`.coverage*`；workspace 侧新增
  `.pytest_cache`、`.ruff_cache`、`.tox`、`htmlcov`、`coverage.xml`、`.coverage*` 等。
  因此 `PATCH_STALE` 依赖的 `expected_tree` 覆盖面发生变化，**需要复验方确认没有
  削弱守卫**，而不是由实现方自证。
- 实现方证据：复现用例连跑 10/10 通过；新增两个测试
  （`test_container_bytecode_caches_do_not_change_tree_hash`、
  `test_runtime_caches_are_ignored_but_real_edits_still_trigger_patch_stale`）；
  只读变异（内存换回旧规则）下两个测试均失败；H04–H09 两轮各 126 passed，H00–H09
  两轮各 209 passed；`make verify TASK=HV4-H09` PASS（commit `98d3042`）。
- 证据：`evidence/harness-v4/HV4-H09/workspace-hash-fix-20261111T082028Z/`；
  `evidence/HV4-H09/20261111T082855Z-397e510e/`。该目录被 `.gitignore` 忽略。
- 注意：H09 `accept.json` 的 `residual_limits` 仍写着“broker 未排除 `__pycache__`”，
  描述的是 `7d8f0ab` 复验时状态；`98d3042` 之后该条已过时，但不应由实现方改写复验
  记录。是否在 accept 中追加“已修复、待复验”注记，由复验方决定。

### P1：`run_id` 恒为零 UUID

- `service.py` 以 `TaskBinding(run_id=UUID(int=0))` 建任务，API 驱动任务的所有事件
  `run_id` 都是零 UUID，不能用于事件归属/关联。隔离仍由每任务独立 `events.jsonl` +
  严格 `task_id` 校验保证。未做低成本修复（会改变既有语义），已在
  `docs/harness-v4/HARNESS_CONTRACTS.md` §8 与 H09 `accept.json` 的
  `deviations`/`residual_limits` 中如实记录。

### P2：文档与历史状态同步

- `TASKS.md` 的 H02/H05/H06/H07 状态行落后于 `accept.json`（见 2.1/2.2）。
- H03 的 `PENDING_EXTERNAL_REVIEW` 是否需要补核，未核实。

### P3：PARTIAL / NOT_RUN 项

- H02 `live_model_gate = PARTIAL`；其余卡的 live 三门均为 `NOT_RUN`。
- `pytest` / Node 真实执行：R03 镜像缺运行时，只验证了 `waiting_approval` 路径，
  真实执行 NOT_RUN（H06/H07 记录）。
- H12 冻结开发/持出任务集、真实 Agent、付费模型验收：NOT_RUN。
- 真实模型驱动的运行中交互 / `ask_user`：NOT_RUN（H09 为脚本化 provider）。
- SSE 多实例/跨进程事件总线：未实现、未验证。
- H04 accept 记录的 C16b（读根 + 严格写子集时容器可能创建失败）：H06 改了挂载
  规划（单次 bind + 掩码），但文档未明确声明 C16b 已闭合，**未核实**。

## 4. 已验证 / 合成 / 未跑

| 类别 | 内容 |
| --- | --- |
| 真实 | H04/H06 真实 Docker 容器执行（固定 R03 镜像、非 root、无网络）；H09 真实 FastAPI + 令牌认证 + HTTP/SSE 路由、真实取消与容器清理；H08 一次性数据库上的浏览器端到端（确定性适配器）；H08 一次受控真实模型调用（deepseek-chat，34 KB 文件单行修改，6 次调用 / 922 字节输出 / 17,182 micros；不构成 H12 验收） |
| 合成/契约 | H07/H08/H09 的模型侧为脚本化 provider；H09 并发写入、reasoning 注入、marker 隔离、`ask_user` 超时为构造场景；H05 编辑协议为确定性用例；H04 负例为真实容器上的构造请求 |
| 未跑 | `live_model_gate`、`live_agent_gate`、`live_publication_gate`；H12 冻结任务集与持出集；容器内 pytest/Node 真实执行；真实模型下的 `ask_user`/运行中交互；多用户按用户隔离；多实例/跨进程事件总线；真实 GitHub 发布 |

不得用合成结果冒充真实结果；live 门未跑就写 `NOT_RUN`。

## 5. 残余限制清单

按卡片顺序，均为当前仍成立或需后续卡片处理的限制（`accept.json` 中已记录的失败与
偏差不覆盖、不删除）：

1. **H04**：allowlist 只约束入口 argv[0]，不约束进程树；缺失保护路径可能被 Docker
   建立空占位（只读 `/dev/null` 场景）；依赖准备固定拒绝；未接真实 MCP/插件；未证明
   容器逃逸/内核/跨主机隔离；C16b 是否由 H06 闭合未核实。
2. **H05**：输出预算不约束模型回复整体体积；`revert_patch` 不检测外部改动；一次性
   落盘不是文件系统事务；重命名不计入 `max_deleted_files`；软链存在 TOCTOU；混合
   换行只做精确匹配。
3. **H06（F1/F2/F3 处置结论）**：F1 重复挂载已修（scope 单次 bind + 保护路径单独
   掩码）；F2 宿主占位已修（可写 scope 下由 broker 以工作区用户预建 0700 空目录，
   随容器删除清理）；F3 `total_bytes` 已修（记录 broker 返回的真实总量，削尾时
   `total_bytes > len(保留)`）。仍存：SIGKILL 时容器与 0700 空占位目录残留；日志
   轮转/清理后 `total_bytes` 不可重建；`exec_probe` 接受任意容器名；仓库内代码可
   伪造测试摘要；pytest/Node 真实执行 NOT_RUN；服务地址仅容器内可达
   （`host_reachable=false`）；新增 `/scratch` 可执行 tmpfs。
4. **H07**：弱化检测是 AST 计数启发式。已闭合裸 `assert` 漏检（P-13）与把
   `self.assert*` 抽到裸 assert 辅助函数的误杀（P-15）；仍存：断言跨测试函数移动
   （Q-03）、同数量同义反复如 `assertTrue(True)`（Q-04）、断言只留在未被调用的辅助
   函数（Q-10）时总数不变；多条断言折成元组断言会被判 weakened 并 blocked（Q-05，
   fail-closed）；非 Python 测试只靠 `tests_run` 与文件存在性；`tests_run` 取
   `max('Ran N tests','N passed')`，失败数不并入总数（只会少算，不会多算）。
   进展判定以树/回执为单位，只读或只改文字判无进展（有意为之）。三个 live 门
   NOT_RUN。
5. **H08**：写路径只能编辑已存在的文件；新增/删除只来自命令副作用或外部变更；删除
   上报为 modified + `after_sha256=sha256("")`，重命名呈现为一次新增 + 一次删除；
   整文件重写在真实模型下未做规模验证；`PATCH_LIMIT`/`PATCH_TRUNCATED` 只有合成
   触发；增量 Diff 遇非 UTF-8 变更以 `WORKSPACE_BINARY` 失败关闭（外部变更或未来
   可写挂载会阻断结果生成）；仍是单操作者本地模型，持令牌者可读 harness 根下所有
   任务。
6. **H09**：`run_id` 零 UUID；取消只保证投递与容器终止、`refund=not_promised`，
   不承诺预算回滚或 GPU/外部资源回收；SSE 断线检测依赖客户端断开与 5s 重认证窗口；
   `ask_user` 600s 超时，超时按工具错误返回；真实模型交互未验证。
7. **跨卡**：pytest/Node 真实执行、SIGKILL 残留、allowlist 入口级、占位残留、弱化
   检测启发式、单操作者无按用户隔离、live/H12 未验收均继续适用。

## 6. 工作纪律

1. **单卡制**：一次只做一张卡；实现到“需要独立验收”即停手汇报，不自行开始下一张卡。
2. **独立复验**：只读、单独会话执行；实现方不得代替复验方下结论，不得把
   `PENDING_RECHECK`/`FAIL` 自行改回 `PASS`；旧失败与新结果分开保存，不覆盖。
3. **不推送**：只在本地提交；不执行 `git push`；不创建远端 PR。
4. **范围**：只动 `/home/developer/magr-harness`；不碰其它活动树、不修改用户其他仓库。
5. **付费调用**：任何真实模型/付费调用先说明目的与预估费用，获得明确许可后再执行；
   未跑就记 `NOT_RUN`，不为凑门额外调用。
6. **执行入口**：H04 Broker 是唯一执行入口；H06 的 `CommandSession` 复用 lease/
   epoch/预算/scope/allowlist；不得为绕过限制新开子进程、socket、docker 或 eval 入口。
7. **证据**：保留原始日志与计数，不裁剪失败记录；合成与真实分栏标注。

## 7. 复现命令

```bash
cd /home/developer/magr-harness/v3

# 单卡注册验收（H00–H09 已注册；H10 尚未注册）
make verify TASK=HV4-H00
make verify TASK=HV4-H04
make verify TASK=HV4-H05
make verify TASK=HV4-H06
make verify TASK=HV4-H07
make verify TASK=HV4-H08
make verify TASK=HV4-H09

# 聚合回归（当前口径：126 / 209）
uv run --frozen pytest -q tests/test_harness_v4_h04.py tests/test_harness_v4_h05.py \
  tests/test_harness_v4_h06.py tests/test_harness_v4_h07.py \
  tests/test_harness_v4_h08.py tests/test_harness_v4_h09.py
uv run --frozen pytest -q tests/test_harness_v4_h00.py tests/test_harness_v4_h01.py \
  tests/test_harness_v4_h02.py tests/test_harness_v4_h03.py \
  tests/test_harness_v4_h04.py tests/test_harness_v4_h05.py \
  tests/test_harness_v4_h06.py tests/test_harness_v4_h07.py \
  tests/test_harness_v4_h08.py tests/test_harness_v4_h09.py

# 静态检查
make lint
make typecheck
```

证据目录约定（`v3/evidence/` 已被 `.gitignore` 忽略）：

- `make verify`：`v3/evidence/HV4-Hxx/<UTC>Z-<8 位哈希>/`，含 `junit.xml`、`command.log`。
- 独立复验：`v3/evidence/harness-v4/HV4-Hxx/independent-recheck-<时间>/`，由复验方创建。
- 实现方回归/修复证据：`v3/evidence/harness-v4/HV4-Hxx/<用途>-<时间>/`。
- 不覆盖历史 attempt；每次运行新建目录。

## 8. 下一步建议

1. **先做 `98d3042` 的独立复验（P0）**：只读复跑 H04–H09 与 H00–H09 各至少两轮、
   H06 复现用例多次、两个新增缓存测试与只读变异；重点确认排除清单扩大后
   `PATCH_STALE` 没有被削弱（真实源码编辑仍改变树哈希、仍被拒绝）。
2. 复验通过、并明确 H09 accept 中过时的 `residual_limits` 处理方式后，再决定是否
   进入 H10；**当前不得自行启动 H10**。
3. H10（长任务计划、上下文与分页工具）的前置：H04–H09 全部 ACCEPTED 且哈希修复
   获独立复验；H10 尚未注册 `config/verification.json`，未开始实现。其验收要求为
   多次强制压缩后仍能读取被移出窗口的源文件/日志，权限、验收项、失败记录与工作树
   hash 不丢失，小任务不因记忆流程强制增加模型调用，摘要文字不能变成执行许可。
4. H12 之前需要单独批准 live 门与付费预算；H03 是否补核、`TASKS.md` 状态同步可
   作为低优先级文档任务处理。


## 9. 2026-11-11 本轮续记：哈希复验与验收记录对齐

本节晚于前八节的交接快照；原 HEAD/ahead、卡片计数、FAIL/PASS 与限制描述原样保留。
本轮起点实查为 `68dc81a55bbdfffe23b0ee18a96436b809bf448a`、工作树干净；不把本地
`origin/main` 跟踪点等同于当前 GitHub main。本轮范围仅为用户指定的哈希复验和记录对齐，
H10–H18 未启动，未新增 live 许可或发布许可。

- 哈希边界：独立文件探针在起点源码上复现 `.coveragerc` 被排除、复制遗漏与哈希遗漏，
  原 `98d3042` 规则在该范围记 FAIL。原始证据为
  `evidence/harness-v4/hash-recheck-20261111T084848Z/initial/source-fingerprint.json`
  与同目录 `baseline/`；缓存排除口径一致不等于配置边界正确。
- 候选修复 `096300eed8946086f99e331ae4c8c9c733108ff9`：只排除精确 `.coverage` 与完整匹配
  `.coverage.<host>.<数字 pid>.<nonce>` 的数据文件，不全局排除 `coverage.xml` / `htmlcov`。
  配置与报告进入树哈希；旧规则建立的任务工作区需要重建并重新验证，不沿用旧测试或
  审批。**最终独立复验 PASS**；H04–H09 两轮各130项、H00–H09 两轮各213项，
  独立探针45项、两种变异检测、原 H06 稳定性10/10与H06/H09注册门均通过。
  完整命令、源码指纹和各门范围由复验方单独归档。
- H03 路径更正：第 2.2 节的 `evidence/HV4-H03/h03-independent-review/` 写法不正确；
  实际为 `evidence/harness-v4/HV4-H03/h03-independent-review/`。原始失败与 fixed 结果
  都存在。三次 verify 覆盖 `a803784` / `0bbe935`，25 tests、同一源码指纹 `e0f2ced7…`。
  但探针 JSON 缺少覆盖 commit/指纹/reviewer 字段，历史交接的“接收”与 accept 的
  PENDING 不能仅靠文字消解，保留 H03 `PENDING_EXTERNAL_REVIEW` 并追加审计事实。
- H02 已有 PASS_EXTERNAL_REVIEW；H04–H09 的历史独立 ACCEPTED 与旧失败全部保留。
  H07 后补修复存在第二轮证据，四个 GAP 继续适用；H08 browser counts/checks 均15。
  H09 的旧计数是124/207，209属于后来增加两项H06测试的口径；HTTP 探针会过滤零UUID，
  不足以证明真实 Run ID 唯一，任务隔离仅按文件/task_id/marker证据说明。

统一当前状态与详细勘误见 [记录对齐摘要](RECORD_RECONCILIATION_20261111.json)。
最终独立结果由 [哈希复验记录](accept/HV4-H09/workspace-hash-recheck-20261111.json) 归档；
本轮最终源码指纹、独立证据路径及两轮聚合结果已写入该摘要的
`hash_boundary_recheck.final_*` / `aggregate_rounds` 字段；H03 接收身份缺口继续保留。
