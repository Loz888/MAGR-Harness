# GA10 Personal / Cross-domain Memory

`Engine.personal_memory` 使用实例侧租户、已登录账户 ID 和项目范围。个人范围只允许用户明确保存的偏好。项目事实和执行经历保留原项目，正常检索只在指定项目运行；跨项目检索须明确给出来源项目，且账户同时拥有两个项目的当前访问权限、该项已明确允许跨项目使用。[记忆存储](../../src/mgar_v3/general_agent/personal_memory.py) · [迁移](../../migrations/0062_personal_memory.sql)

使用浏览器账号登录后，`/api/v3/general/memory` 的 POST 和 GET 可保存和检索个人记忆。按 ID 用 GET、PATCH、POST revoke 或 DELETE 查看、修正、撤销和删除。修改、撤销和删除使用当前版本；按旧版本查看可追溯冲突。撤销和过期项退出正常召回。删除会清理当前记录和版本历史，项目权限被撤回后，账户仍能删除自己的记录。[API](../../src/mgar_v3/general_agent/memory_api.py)

手动内容标记为用户确认。执行经历可明确引用本人当前完成的任务，内容与 Outcome 摘要必须一致，来源固定任务修订和 Outcome 哈希。外部检索内容没有自动写入通道。写入复用旧记忆的凭据规则并拦截常见敏感标识。新记录提供与旧类型相容的 procedural、semantic、episodic 映射，原 Working 和仓库记忆使用原服务。

## 验收

```bash
make verify TASK=GA10
```

质量夹具涵盖相关项召回、无关账户和未授权项目的泄漏拒绝、撤权后检索、可信来源、并发修正、过期过滤及完整删除。使用真实 PostgreSQL 和已有登录及 CSRF 接口。[工程验收记录](accept/GA06-GA10/accept.json)
