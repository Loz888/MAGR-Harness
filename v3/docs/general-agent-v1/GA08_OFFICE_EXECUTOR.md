# GA08 Office Automation Executor

Office 适配器固定到一个受信账户。读取、生成草稿、发送、创建和更新日历、分享和删除各自拥有独立的资源授权。草稿保存在本地 CAS。写入请求固定操作者、项目、目标、载荷、账户和租约，使用 GA04 ActionIntent、审批和 Outbox 派发。修改现有记录要求前置版本；删除要求升级审批。[Office 契约与适配器](../../src/mgar_v3/general_agent/office.py)

Google Workspace OAuth 适配器固定官方 HTTPS 端点，在运行内存保留 Bearer 凭据。支持读取 Gmail、日历、Docs 和 Sheets。发送使用固定邮件 Message-ID 对账。日历创建与对账共用由持久化动作 ID 派生的事件 ID，旧事件在原动作标记匹配后可对账。更新使用版本前置条件。日期、IANA 时区、DST 偏移、参与者和有限重复规则在派发前验证。通用分享和删除可经支持相应条件和对账协议的连接器派发，Google 适配器当前明确拒绝这两项。[Google 适配器](../../src/mgar_v3/general_agent/office_google.py)

服务器通过 `OfficeExecutor.read_handler` 和 `draft_handler` 把已获授权的只读或草稿操作交给 `Engine.general_executors.run`。读取所声明的来源固定资源类型、账号、父对象和外部 ID，返回前复查权限。写入使用 `OfficeExecutor.queue`，审批及派发通过原 GA04 入口。办公室资源授权、OAuth 凭据及运行适配器由部署侧注入，持久化证据记录外部 ID 和意图绑定。

接口细节对应 Google 的公开文档。[Gmail 发送](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send) · [Gmail 消息查询](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list) · [Calendar 条件更新](https://developers.google.com/workspace/calendar/api/guides/version-resources) · [Docs 内容](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/get) · [Sheets 范围](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/get)

## 验收

```bash
make verify TASK=GA08
```

夹具验收使用隔离 PostgreSQL 和受控 OAuth HTTP 传输。专用生产测试账户端到端为 `NOT_RUN`。本次没有真实发送、分享、删除、发布或模型调用。[工程验收记录](accept/GA06-GA10/accept.json)

定向复测中，同一账号和日历上的两个项目使用相同幂等键，生成了两个不同的事件 ID。丢失响应的动作和旧版事件通过只读查询对账，旧事件的原动作标记不匹配时保留待对账。[定向复测记录](accept/GA06-GA10/ga06-ga08-repairs-20261119.json)
