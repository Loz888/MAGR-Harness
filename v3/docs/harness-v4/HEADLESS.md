# H13 Headless CLI 与 Python SDK

CLI 和 SDK 使用本机 Harness 服务。会话、任务、变更、用量和回执都从 Web 已有的接口读取。服务端沿用项目授权、预算和隔离执行策略。

## 启动与提交

在 `v3/` 完成首次配置并启动服务。需要已配置的 PostgreSQL、Redis 和项目运行镜像。[启动说明](../../README.md)

先注册 Harness 服务能访问的本机目录，然后用返回的 `project_id` 查看准备状态。在 Web 设置中确认该项目的模型、工作区和费用授权后，提交自然语言需求。

```bash
uv run --frozen mgar harness register --input - <<'JSON'
{"path":"/absolute/path/to/project"}
JSON

uv run --frozen mgar harness readiness PROJECT_ID

uv run --frozen mgar harness submit --input - <<'JSON'
{"project_id":"PROJECT_ID","text":"修复加法函数并验证原有测试","client_id":"fix-add-001"}
JSON
```

默认使用 `http://127.0.0.1:18080`。自定义地址用 `mgar --api URL harness ...`，URL 必须是回环 HTTP(S) origin。CLI 从 `MGAR_V3_LOCAL_TOKEN` 或当前安装的服务 `.env` 读取操作者令牌。浏览器通过登录会话及 CSRF 校验访问相同 API。自动化使用稳定的 `client_id`，请求回执丢失时用相同标识和相同内容确认提交。

使用提交结果中的 `session_id` 和 `submission.task_id` 进行后续查询。响应顶层展示会话当前任务，`submission` 固定对应本次 `client_id`，保留回合标识和所关联任务的状态。用相同 ID 重查较早的提交时，取 `submission.task_id` 获取原任务。准备中的回合会返回 `submission.status=preparing`，退出码为 2，任务标识将在接收成功后出现。

```bash
uv run --frozen mgar harness task TASK_ID
uv run --frozen mgar harness events TASK_ID --follow
uv run --frozen mgar harness artifacts TASK_ID
uv run --frozen mgar harness patch TASK_ID --output result.patch
uv run --frozen mgar harness export SESSION_ID > session.json
```

| CLI 命令 | 用途 |
| --- | --- |
| `projects`、`register`、`readiness` | 本机项目列表、注册与环境准备状态 |
| `sessions`、`submit`、`session`、`send` | 创建、查询和继续会话。 `send SESSION_ID` 接收 `text` 和可选的 `client_id` |
| `task`、`pause`、`resume`、`cancel` | 读取任务、等待安全点暂停、恢复和取消 |
| `messages`、`answer` | 查看任务消息和回答澄清。 `answer TASK_ID` 接收 `text`、`reply_to` 和 `message_id` |
| `events`、`event-page` | 流式订阅或分页读取事件，均可传 `--after` |
| `artifacts`、`diff`、`patch`、`export` | 读取验证记录、Diff、补丁和会话快照 |

普通命令向标准输出写一个 JSON 值，错误详情写入标准错误。事件流为 NDJSON，每行带 `event`、`cursor`、`data`。默认读取到当前尾部，`--follow` 持续到终态。断线后用最后处理成功的 `cursor` 传给 `--after`。Worker 在安全点确认暂停和恢复。取消的响应返回服务端当前状态，尚未确认的模型用量由原账本保留并核对。

| 退出码 | 含义 |
| --- | --- |
| 0 | 命令成功。任务状态类命令表示任务已完成 |
| 1 | 任务失败、取消或请求错误 |
| 2 | 任务未完成、等待输入、用量待核对，或事件订阅中断。CLI 参数语法错误也沿用 argparse 的 2 |
| 3 | 运行镜像或项目依赖缺失，环境需要准备 |
| 130 | 操作者中断本地 CLI |

本地 CLI 中断只结束客户端读流。任务中止通过 `cancel TASK_ID` 向服务提交。

## SDK 与兼容策略

```python
import os

from mgar_v3.harness.client import HarnessClient

with HarnessClient(os.environ["MGAR_V3_LOCAL_TOKEN"]) as client:
    session = client.session("SESSION_ID")
    task = client.task(session["task_id"])
    export = client.export_session(session["session_id"])
    print(task.get("tree_after"), task.get("settled_cost_micros"))
```

SDK 提供与 CLI 对应的方法，事件用 `events(task_id, after=cursor, follow=True)` 消费。HTTP 错误包含 `HarnessAPIError.code`、`status_code` 和可用的 `request_id`。网络中断按原请求结果是否已知处理。调用方通过状态查询和消息标识确认写入回执。

Web 路由以 `/api/v3` 版本提供。会话导出是 `GET /api/v3/agent/sessions/{session_id}/export`，携带 `schema_version=mgar.harness.session.v1`、会话元数据、按顺序的回合和当前任务快照。历史回合的任务仍可按 `task_id` 单独查询。v1 可增加可选字段，改变既有语义时升版本。SDK 遇到未知的导出版本会显式报错。

H06 的执行面提供命令句柄、取消和分段日志。H13 的 Headless 客户端通过任务事件和产物查看这些结果。[H06 命令契约](COMMAND_EXECUTION.md)

运行 `make verify TASK=HV4-H13` 复验 CLI、SDK、Browser API 和持久化任务的共享契约。使用隔离测试 PostgreSQL 实例和固定 Docker 镜像。[机器验收](accept/HV4-H13/accept.json)
