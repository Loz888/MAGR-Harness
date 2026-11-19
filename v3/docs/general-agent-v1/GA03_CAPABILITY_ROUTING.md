# GA03 Capability Registry 与 Domain Router

GA03 与 GA02 共用候选源码 `1c88262`，从 GA01 独立复验提交 `d78612b` 开发。Native、MCP 和 Domain Executor 使用同一份能力描述。描述包括版本、领域、输入输出 Schema、Provider、权限、风险、副作用、幂等、资源上界、超时和证据协议。目录另行记录能力健康状态。[能力注册实现](../../src/mgar_v3/general_agent/capabilities.py)

## 路由与发现

- 路由依据当前节点的领域和备选能力，核对任务声明、项目与任务修订授权、能力描述哈希、权限、风险、副作用、健康和工具 Schema 的上下文占用。输入、预算及证据协议不匹配时检查下一候选。路由采用按序备选，记录每项未入选原因及选中的能力哈希。
- 模型投影只包含当前有效认领的一个能力 Schema，投影前复查成员权限、当前描述和授权预算。等待审批的图节点只决定流程能否继续，外部写动作仍交由 Action/Outbox 通道。
- MCP 部署端显式固定服务器地址、服务器版本、能力 ID、工具名、输入和输出 Schema。发现使用含认证的 HTTPS 初始化与工具列表接口，禁用重定向和环境代理。发现只更新配置项健康，目录外工具无法进入路由。超时、版本不符、Schema 不符和健康观察过期均有可审计原因。[MCP 发现实现](../../src/mgar_v3/general_agent/mcp_discovery.py)
- 默认目录登记 Coding、Research、Data 和 Office。Coding 使用现有 Harness 策略。Research、Data 和 Office 的执行器条目在接入各自生产执行器前报告不可用。覆盖完整跨领域图的验收使用受控执行器回执。[默认目录](../../src/mgar_v3/general_agent/builtins.py)

## 验收与复验

```bash
make lint
make typecheck
make verify TASK=GA03
uv run --frozen pytest -q tests/test_mcp.py tests/test_r4_mcp.py
```

专项 8/8、既有 MCP 回归 10/10 均通过，无失败、错误或跳过。专项用真实 PostgreSQL 核对路由事件，使用受控 MCP 传输核对认证、目标、输出 Schema、超时回退及并发发现。独立工程复验为 INDEPENDENT_PASS，远程 MCP 联调为 `NOT_RUN`。源码指纹及原始证据路径见[验收记录](accept/GA03/accept.json)。

[独立复验](GA02_GA03_INDEPENDENT_20261119.md)
