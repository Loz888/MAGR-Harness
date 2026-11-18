# HV4-H06：命令、依赖、测试与应用进程

本卡在 H04 Broker 之上提供命令句柄与语言适配。所有容器仍由
`IsolatedExecutionBroker` 创建，H06 没有新增任何绕过 lease/epoch/预算/cwd/
命令 allowlist 的执行入口。

## 1. 命令句柄

模块：`src/mgar_v3/harness/commands.py`

- `CommandSession.run(request, grant, cancellation, purpose)` 先调用
  `broker.launch`，通过 H04 的 `_authorize` 后才 `docker create/start`。
  返回 `CommandHandle`：`handle_id`、`operation_id`、`purpose`、`argv`、`cwd`、
  状态、退出码、开始时间、deadline、工作树摘要 `tree_hash`、环境摘要
  `environment_hash`、日志字节数与 sha256。
- `poll(handle_id)` 用 `docker inspect` 刷新状态；`wait` 轮询到结束或调用方
  超时；`cancel` 标记取消并 `docker rm -f`。
- 句柄归创建它的 session 所有。别的 session 或伪造 id 得到
  `HARNESS_COMMAND_NOT_FOUND`；`close` 后所有句柄操作得到
  `HARNESS_SESSION_CLOSED`。
- `close()` 停止并删除本 session 启动过的全部容器。

`tree_hash` 在启动时对可见工作树（排除 `.git`/`.mgar`）逐文件 sha256 后汇总，
因此句柄、测试回执与编辑看到的是同一版本。

## 2. 日志、分页与背压

- `docker logs` 的输出受 lease 的 `max_output_bytes`（最大 1 MiB）截断。
- 内存缓冲默认等于 lease 上限，也可由 `CommandSession(max_buffer_bytes=...)`
  收紧；超限时丢弃最旧字节并累计 `dropped_bytes`，不会无限增长。
- `read_log(offset, limit)` 返回 `LogPage`，单页不超过 `page_bytes`（默认
  16 KiB），包含 `next_offset`、`eof`、`total_bytes`、`dropped_bytes`、
  `truncated`。调用方按页拉取即可对慢消费者形成背压；已丢弃的前缀从
  `dropped_bytes` 继续，不阻塞命令执行。
- 截断必须显式上报：`command_logs` 流式读完 `docker logs` 并返回
  `(保留字节, 实际总字节)`。`LogPage.total_bytes` 与 `CommandHandle.log_bytes`
  使用 broker 返回的真实总字节，不由 `dropped_bytes + 保留长度` 推算；被 lease
  削掉尾部时 `total_bytes > len(保留)` 且 `truncated=True`、`log_truncated=True`，
  未削尾时两者相等。`dropped_bytes=0` 只表示内存缓冲没有溢出，不再被当作
  "日志完整"的证据。`eof` 表示已交付全部**保留**字节；削尾时
  `total_bytes > next_offset` 仍可为 `eof=True`，差额即不可恢复的尾部。
- 当前实现每次 poll 重新读取 `docker logs` 并只追加新字节；日志驱动本身不阻塞
  容器，因此慢读不会反压到被测进程。

## 3. 超时、取消与清理

- deadline 取 `min(request.timeout_seconds, lease.timeout_seconds, grant.max_seconds)`。
- 每个句柄有独立 monitor 线程；到期后 `docker rm -f` 整个容器（`--init` 下
  PID 1 及其子进程随容器销毁），状态 `timed_out`、退出码 124。
- `cancel` 走同一清理路径，状态 `cancelled`、退出码 137。
- 运行中的命令每轮 monitor/poll 都用 `authority.peek()`（只校验
  lease id/hash/撤销/过期，不消耗预算）检查租约归属与 epoch。租约被撤销、
  到期或被新 epoch 替换时，状态 `revoked`、退出码 137，并 `docker rm -f`
  整个容器；不会让已被撤销的命令继续跑完。
- 正常退出只读取最终日志，随后 `docker rm -f`；不保留运行中的容器。

## 4. 受控后台服务与就绪

- `start_service` 启动容器后轮询 `wait_ready`。探测在容器网络命名空间内执行：
  `tcp`/`http` 使用固定的 `python3` 探针脚本；`command` 使用可信适配器提供的
  argv，固定经 `docker exec` 执行。
- 就绪失败会取消并删除容器，抛出 `HARNESS_SERVICE_NOT_READY`。
- 容器固定 `--network=none` 且不发布端口，所以返回地址
  `127.0.0.1:<port>` 只在容器内可达；`host_reachable` 恒为 `False`。宿主侧
  没有可访问地址，这是隔离选择而不是遗漏。

## 5. 语言适配与测试判定

模块：`src/mgar_v3/harness/project_commands.py`

- 检测顺序：`package.json` → Node；`go.mod` → Go；
  `pyproject.toml`/`requirements.txt`/`setup.py`/`pytest.ini`/`tests/` → Python。
- Python：存在 pytest 配置时 `python3 -m pytest -q`，否则标准库
  `python3 -m unittest discover -v`；构建 `compileall`。
- Node：`npm test`、`npm run build`、`npm start`；依赖 `npm ci`/`npm install`。
- Go：`go test -v -buildvcs=false ./...`、`go build -buildvcs=false ./...`；
  `-buildvcs=false` 避免空 `.git` 掩码触发 VCS 状态错误。构建/测试需要可执行
  临时空间，因此租约提供 `/scratch` tmpfs。
- 判定规则：只有 `exit_code == 0`、`tests_run > 0`、`tests_failed == 0` 且
  不是全部跳过才是 `passed`。无测试、全部跳过是 `no_tests`；退出 0 但解析不到
  测试数是 `unverified`；测试运行时或测试包不可用是 `waiting_approval`（见 §6）；
  超时/取消/租约丢失/容器消失分别是 `timeout`/`cancelled`/`revoked`/`error`。
  退出 0 本身从不冒充通过。
- 每个 `TestReceipt`/`BuildReceipt` 绑定句柄、argv、退出码、工作树摘要、日志
  sha256 与耗时，receipt id 是这些字段的 canonical digest。

## 6. 依赖准备

- 只调用 H04 的 `broker.prepare_dependencies`。该端口当前对所有包源返回
  `HARNESS_DEPENDENCY_SOURCE_DENIED`，因此需要安装的依赖返回
  `waiting_approval`，带 `missing`、原始 argv 与可操作说明。
- 运行时也先探测：R03 镜像没有 `pytest`/`node`，这两类项目分别返回
  pytest 缺失、Node 运行时缺失的等待状态；Go 与标准库 unittest 可真实运行。
- `run_tests` 在 launch 之前调用 `_test_readiness`：pytest 项目探针
  `python3 -c "import pytest"`，Node 项目探针 `node --version`。探针失败时直接
  返回 `waiting_approval` 的 `TestReceipt`（`missing=("pytest",)` 或
  `("node",)`、`tests_run=0`、带可操作 `action`），不会先把不可能运行的命令
  启动起来再落成 `unverified`，也不伪造测试数。
- H06 不在容器内执行 `pip install`/`npm install`，也不开启网络。

## 7. 环境与资源

- 固定环境：`PATH`（含 `/usr/local/go/bin`）、`HOME=/tmp`、`LANG=C.UTF-8`、
  `TMPDIR=/scratch`、`GOCACHE=/scratch/go-cache`、`GOTMPDIR=/scratch`。
- `/tmp`：16 MiB tmpfs，`noexec,nosuid,nodev`。
- `/scratch`：租约 `scratch_mb`（默认 64，最大 2048）tmpfs，
  `exec,nosuid,nodev`，供编译器和测试二进制使用；容器销毁即消失。
- 其余隔离保持 H04：`--read-only`、`--cap-drop=ALL`、
  `no-new-privileges`、`--pids-limit`、内存/CPU/ulimit、`--ipc=none`。

## 8. 本卡同时修复的 H04 挂载缺陷（含独立复验 F1/F2）

独立复验在 `fd5b57d` 实测到两个阻断项，均已按根因修复：

- **F1 重复挂载**：旧实现把只读根展开成逐子项 bind，又对 `.env` 追加
  `/dev/null` 掩码，同一 target 出现两次，`docker create` 直接报
  `Duplicate mount point: /workspace/.env`；缺失路径的掩码还会迫使 runc 在
  只读父挂载内创建挂载点。现在声明的 scope 只 bind 一次：读根是
  `/workspace` 只读 bind，写子集是另一次可写 bind（真实 Docker 已确认只读父
  下已存在的可写子目录可以正常挂载）。保护路径单独掩码，target 不再与任何
  scope 重复。
- **掩码规则**（优先于 bind，容器内保持 EACCES/mode 000）：
  - 已存在普通文件 → broker 自建、`mode 000` 的空普通文件只读 bind。
    不再使用 `/dev/null`：只读 bind 的 `/dev/null` 仍会接受写入并丢弃。
  - 已存在目录 → 只读 tmpfs，`size=1m,mode=000`。
  - 只读 scope 下缺失 → 不掩码。只读父挂载本身使该路径不可创建、不可读，
    宿主也不会出现任何占位。
  - 可写 scope 下缺失 → tmpfs 掩码的 target 必须存在，否则 `docker start`
    会在宿主 bind 里创建 root:root 0755 目录（F2）。现在由 broker 以工作区
    用户身份预建 `0700` 空目录作为挂载目标，Docker 不再写宿主；正常退出、
    超时、取消与 `close` 删除容器时一并 `rmdir`。
- 挂载集合仍严格等于 `read ∪ write`；保护路径在容器内不可读不可写，宿主文件
  的属主、mode、内容都不变。
- 结构上覆盖该缺陷的用例（工作区预置 `.env`/`.git`/`.mgar`）：
  `test_read_root_with_narrow_write_starts_with_existing_protected_paths`
  （存在形态）、
  `test_read_root_with_narrow_write_leaves_no_placeholder_for_missing_paths`
  （只读根缺失形态）、
  `test_writable_root_keeps_protected_paths_denied_and_host_pristine`
  （可写根缺失形态）。三者都比较挂载前后的宿主清单（类型、symlink、uid/gid、
  mode、内容 sha256），并断言容器可起、保护路径被拒。

## 9. 限制与残余风险

承接 H05 复验记录与本卡新增：

- 输出预算只约束容器输出与 `new_text`/`content`，不约束模型回复整体体积；
  `live_model_gate` 仍为 NOT_RUN。
- `revert_patch` 不检测磁盘外部改动；一次性落盘是逐文件写加失败回滚，不是
  文件系统事务；重命名不计入 `max_deleted_files`；软链防护存在同用户并发替换的
  TOCTOU 窗口；混合换行仅精确匹配。以上在本卡不涉及编辑路径，未改变。
- `/scratch` 可执行：这是 Go/测试二进制的必要条件，空间受租约限制、随容器
  销毁；命令入口仍受 allowlist 约束。
- 命令 allowlist 只约束容器入口命令，不限制容器内子进程树；H04 已标注。
- 宿主无法访问服务地址；需要宿主访问时必须显式增加受控端口发布与网络授权，
  本卡未做。
- 进程崩溃不会自动删除已启动容器（`docker create` 不支持 `--rm`）；正常退出、
  超时、取消和 `close` 都会清理。孤儿容器可通过
  `docker ps -a --filter name=mgar-h06-` 发现。
- 可写 scope 下缺失的保护路径，在容器运行期间宿主会出现 broker 预建的空
  `0700` 目录（容器内被 tmpfs 掩码覆盖，不可读不可写），随容器删除而
  `rmdir`；进程被强杀时可能残留这类空目录。其属主是工作区用户，不是 root，
  且不含任何内容，可用 `rmdir` 清理。
- 文件掩码使用宿主临时目录（`tempfile.mkdtemp`、进程内复用、mode 000）中的
  空普通文件，不在工作区内；容器只读 bind 该文件，读写均 EACCES。
- Node/TS 与 pytest 的真实运行在 R03 镜像上为 NOT_RUN（缺运行时/包），只验证
  了检测、命令生成与可操作等待状态；未伪造任何测试数。
