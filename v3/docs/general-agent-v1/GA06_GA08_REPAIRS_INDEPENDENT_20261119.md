# GA06、GA08 修复独立复验

源码 `c586e82` 的两项定向复验均为 **INDEPENDENT_PASS**。新 Git 检出使用锁定依赖、独立 PostgreSQL 和 Redis，`make verify TASK=GA06-GA08-REPAIRS` 只运行原失败对应的两项，2/2 通过，零失败、错误或跳过。Ruff、mypy 和锁文件检查均通过。[本地修复记录](accept/GA06-GA10/ga06-ga08-repairs-20261119.json) · [机器记录](accept/GA06-GA10/ga06-ga08-repairs-independent-20261119.json)

- **GA06。** Poppler 26.07.0 独立渲染了组合页面和两个单字形页面，`𠮷` 和 `📈` 都可见。GA05 对 Markdown 和 PDF 接受同一真实引用，并拒绝错误引用及改动过的 PDF。用原实现生成的旧版 PDF 和纯 ASCII 旧报告均被新版校验拒绝，重新生成后通过。[渲染与兼容记录](accept/GA06-GA10/ga06-ga08-repairs-independent-20261119.json)
- **GA08。** 在真实 PostgreSQL 审批和 Outbox 链路上，两个项目使用同一幂等键提交到同一个模拟 Google 日历，结果是不同的事件 ID。丢失响应后通过 GET 找到对应事件。旧 ID 的动作标记不匹配时保留 `unknown`，匹配后对账成功。整个对账阶段没有额外 POST。[专项回执](accept/GA06-GA10/ga06-ga08-repairs-independent-20261119.json)

本轮复用了此前已通过用例的证据。原独立失败和修复团队的前后对照均保留。公开 Web 上轮为 `BLOCKED`，本轮未复跑。Office 生产账户为 `NOT_RUN`，外部写入和真实模型调用均为 0。[原独立复验](GA06_GA10_INDEPENDENT_20261119.md)
