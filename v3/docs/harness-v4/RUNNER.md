# H15 远程 Runner

服务端保管任务和回执。Runner 从设备主动连接服务，领取任务后，用该设备上的项目快照和已准备的 Docker 镜像运行固定测试。网页和 CLI 展示设备名及当前任务状态。固定 GitHub commit 的项目继续在服务端隔离环境运行。

## 配对并验证一个项目

在控制端的 H15 安装目录中运行 `make migrate`，应用 `0056_harness_remote_runner.sql` 后启动服务。网页在「打开项目 → 配对另一台设备」生成配对码，也可以在控制端命令行生成。

```bash
uv run --frozen mgar runner issue-code
```

在装有 Python 3.12 和本地 Docker 的 Runner 设备上，用同版本源码安装客户端。把下例中的服务地址、设备名、目录和镜像锁路径换成实际值。配对命令会提示输入五分钟内有效的配对码。Linux/WSL 使用设备自身的绝对路径。

```bash
uv sync --frozen
uv run --frozen mgar runner pair --server https://harness.example.org --name "开发电脑"
uv run --frozen mgar runner attach /home/me/project \
  --name "我的项目" --image-lock /home/me/runtime/image-lock.json
uv run --frozen mgar runner work
```

每台设备独立保存状态，默认为 `$XDG_STATE_HOME/mgar-runner`，未设置该变量时为 `~/.local/state/mgar-runner`。需要切换设备身份时，在子命令前用 `mgar runner --state DIRECTORY` 指定对应目录。服务端操作者令牌只用于控制端。

在页面选择已登记的工作区，再点「运行项目测试」。也可以从控制端提交并用相同的 H13 接口读取结果。

```bash
uv run --frozen mgar runner workspaces
uv run --frozen mgar runner submit WORKSPACE_ID
uv run --frozen mgar runner jobs
uv run --frozen mgar harness task TASK_ID
uv run --frozen mgar harness events TASK_ID
```

自动化用 `submit --client-id UUID` 重查同一次提交。Python SDK 对应方法为 `issue_runner_pairing`、`runner_devices`、`runner_workspaces`、`queue_runner_job`、`runner_jobs`、`runner_events` 和 `revoke_runner`。[H13 CLI 和 SDK](HEADLESS.md)

## 连接和执行范围

- 设备使用 HTTPS 校验证书。私有 CA 通过 `pair --ca-file /path/to/ca.pem` 设置。单机联调可用回环 HTTP 地址。跨设备部署时由 TLS 反向代理转发 `/api/v3/runner/device/`，上游 Host 使用现有回环服务地址。操作者页面与管理接口沿用现有回环入口，也可以经受信的本地端口转发访问。
- 工作区接口只传设备绑定的 ID、显示名、项目类型和计划哈希。项目路径、镜像选择和测试 argv 保存在 Runner 本机。Runner 使用 H03 的隔离副本和 H04 的 Broker。容器以非 root 用户运行，使用只读工作区和临时写入空间，网络关闭，项目进程无法访问 Docker socket、Runner 凭据或被遮蔽的配置。
- v1 运行已登记的 Python、Node 或 Go 固定测试计划。镜像锁与项目探测在 Runner 端确认。准备镜像和依赖由操作者负责。验收使用完整、最长 8 KiB 的测试记录，零条测试、无法解析的结果和截断结果均无法记为通过。远程自然语言编码和应用变更仍待接入。

## 失联、恢复与撤销

- 设备领取时取得约 12 秒的票据，通过心跳续租。每个票据绑定设备、任务、工作区、启动时的树与 Run 的 epoch。重复领取与结果重传沿用原回执。服务端重启后，确认尚未启动的任务可回到原 Run 重新领取，epoch 随之增加。
- 已确认开始的任务在失联后显示「等待核实」。本机监视进程会按原容器标识清理，仅有过期票据的结果无法覆盖服务端状态。运行结果不明的任务保留事件，后续验证需提交新任务。
- 网页可以停止任务或撤销整个设备。授予该设备的所有待执行票据随撤销失效。CLI 对应 `mgar runner revoke DEVICE_ID`，审计可通过 `mgar runner events --after CURSOR` 分页读取。设备重新加入时使用新的私有状态目录和配对码。

运行 `make verify TASK=HV4-H15` 复验。验收需要隔离 PostgreSQL 与固定 R03 镜像，包含独立 Runner 进程、真实 HTTPS、容器强制中断和已固定的服务端 GitHub 导入检查。
