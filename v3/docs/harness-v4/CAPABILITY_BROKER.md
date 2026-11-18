# Harness V4 能力许可与执行 Broker（HV4-H04）

- 契约版本：`harness-capability-lease-v1`
- 代码：`src/mgar_v3/harness/broker.py`
- 测试：`tests/test_harness_v4_h04.py`（27 项，含真实 Docker 容器允许/拒绝矩阵）
- 注册验收：`config/verification.json` 的 `HV4-H04` 条目
- 状态：独立验收曾 FAIL（整工作区挂载与内建工具绕过）；本版按完整卡修复，
  等待独立复验，`security_gate` 保持 `FAIL` 直到复验通过。

## 1. 能力租约

`CapabilityLease` 冻结 actor、workspace、repository、execution epoch、读写路径、
命令 allowlist、网络意图、时限、输出/内存/CPU/pids 限额、凭据名称、保护路径、
调用预算、外部写许可、批准人与 approval_ref、有效期与自 hash。三类预设：

| 预设 | 写路径 | 批准 | 外部写 | 用途 |
| --- | --- | --- | --- | --- |
| investigate | 必须为空 | 不需要 | 否 | 只调查/只读挂载 |
| modify_test | 必须显式 | 必须 | 否 | 指定工作区修改并测试 |
| publish | 可空 | 必须 | 是 | 仅保留许可；执行仍拒绝，走 Action/Approval/Outbox |

`issue_lease` 对网络意图直接拒绝：H04 不授权任何 egress。旧的只读 grant 不会
自动升级：`grant.execute=false` 在执行入口直接拒绝；可写模式必须给出具体写路径、
批准人与 approval_ref。

## 2. 唯一执行入口与即时授权

`IsolatedExecutionBroker.execute` 在创建容器前通过 `CapabilityAuthority` 读取
**当前**租约，并校验：

1. 租约自 hash、有效期、撤权状态（本地或外部 `revocation_check`）；
2. 预算（外部 `budget_check`；未接账本时使用租约 `max_calls` 有界计数器，
   不存在“无回调即无限放行”）；
3. `grant.actor`、workspace、repository 与当前租约精确一致；
4. `request.execution_epoch` 等于当前租约 epoch（旧 epoch/旧 context 拒绝）；
5. `grant.execute` 为真；
6. 读写范围取 `lease ∩ grant` 的最窄公共根；可写模式必须得到非空写范围；
7. cwd 位于有效范围且不在保护路径内；
8. argv[0] 同时位于租约与 grant 命令 allowlist（grant 为空则拒绝）；
9. 超时取 request/lease/grant 三者的最小值；
10. publish/external_write 一律拒绝，必须走 Action/Approval/Outbox；
11. 网络意图（租约或 grant）一律拒绝，容器始终 `--network=none`。

`CapabilityAuthority` 支持 `lease_source` 回调，在每次调用时重新读取受信当前
租约；本地测试可用 `replace` 替换租约，替换后旧 epoch 立即失效。

## 3. 真实容器隔离

执行使用已固定的 R03 镜像 ID（`.local/r03/sandbox-lock.json`），每个调用创建独立
容器：

- 只挂载 `lease ∩ grant` 的有效范围：读路径只读挂载，写路径读写挂载；未批准的
  兄弟目录、`.mgar`、`.git` 与保护文件不会出现在容器内；
- 挂载前逐组件检查宿主 symlink；任一 scope 组件是 symlink 或解析后逃出工作区，
  在创建容器前拒绝，避免 `allowed -> /etc` 或 `allowed -> protected` 绕过；
- 保护路径（默认 `.git`、`.mgar`、`.env`）即使落在读写根内也会被覆盖：
  已存在目录用 `mode=000` 的只读 tmpfs，已存在文件用 broker 自建 `mode 000`
  的空普通文件只读 bind（不用 `/dev/null`，只读 bind 的 `/dev/null` 仍接受写入）；
  只读 scope 下缺失的路径不掩码（只读父挂载本身阻止创建与读取）；可写 scope 下
  缺失的路径由 broker 以工作区用户身份预建 `0700` 空目录作为 tmpfs target，
  随容器删除，Docker 不再在宿主创建 root 属主占位；
- `--network=none`，独立 IPC；
- `--read-only` 根文件系统，`/tmp` 为受限 tmpfs；
- `--user=65532:65532`，`--cap-drop=ALL`，`no-new-privileges`；
- `--pids-limit`、`--memory`/`--memory-swap`、`--cpus`、`nofile`/`core` ulimit；
- 环境仅 `PATH`/`HOME`/`LANG`；Broker 持有凭据但值不进入项目进程；宿主 home
  不在容器内；
- 超时后 `docker rm -f` 清理，回执状态 `timeout`；正常/失败路径同样清理。

## 4. 内建工具的同一授权入口

`HarnessToolRegistry` 的构造必须提供 `CapabilityAuthority`，不存在缺省放行：

- `read_file` / `list_dir` / `apply_edits` / `run_tests` 每次调用都校验当前
  actor、workspace、repository、epoch、预算、撤权、租约有效期与路径范围；
- 传到 authority 的路径同时包含调用路径与 workspace 解析后的 realpath；symlink
  指向兄弟目录或 `.mgar`/`.git` 时按真实目标拒绝；
- workspace 若不能提供 `resolve`（无法证明授权路径就是实际访问路径），四个内建
  工具全部返回 `HARNESS_PATH_DENIED`，不使用可选回调降级放行；
- 保护路径在读、列、编辑和执行入口均拒绝；
- `apply_edits` 仍保留 grant 的 `can_write` 与 workspace 的版本化编辑校验；
- `run_tests` 把 task epoch 放入 `ExecutionRequest`，再进入同一个 Broker；
- 旧 `ToolContext`、过期租约、已撤权租约、预算耗尽与陈旧 epoch 均在触碰
  workspace 或进程前拒绝。

## 5. 命令 allowlist 的真实含义

命令 allowlist 只约束 **入口 argv[0]**，不约束入口进程再启动的子进程。例如
allowlist 只列 `sh` 时，`sh -c 'python3 ...'` 仍可启动 `python3`。因此：

- allowlist 是最小入口约束与审计信号，**不是**进程树隔离；
- 实际进程边界仍由容器提供：非 root、只读根、按范围挂载、无网络、cap drop、
  cgroup/pids/ulimit；子进程继承同一容器边界；
- 不声称能阻止被允许入口内的任意子进程；也不靠解析命令字符串或禁用某几个
  字符串来“修补”隔离。

## 6. 兼容与未声称能力

- `ExecutionRequest.execution_epoch` 为加法字段；H01/H02 的本地执行测试仍通过，
  但其 `HarnessToolRegistry` 构造也显式提供匹配的 `CapabilityAuthority`。
- 依赖准备是独立入口 `prepare_dependencies`，H04 无包源/网络授权，固定返回
  `HARNESS_DEPENDENCY_SOURCE_DENIED`；实际安装留 H06。
- 未修改 Engine、API、数据库迁移、预算算法或 Codex 活动树；不占用 0033。
- 未接入真实 MCP/插件；扩展卡（H14）仍需独立验收。
- 未执行真实模型、外部发布或真实凭据使用：`live_model_gate`、
  `live_agent_gate`、`live_publication_gate` 为 `NOT_RUN`。
- 容器逃逸、内核漏洞、恶意宿主 OS 与跨主机隔离不在本次证明范围；仅证明声明的
  Docker 配置在本地真实运行并通过允许/拒绝矩阵与清理检查。
- H04 无模型调用要求，费用 0；合成文件/假凭据，无真实隐私或付费外发。
