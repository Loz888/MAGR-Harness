# GA06–GA10 独立工程复验

复验源码为 `45971ec`，原验收提交为 `85b0786`。源码指纹为
`6b7fad22e523ee24a66222939c000338a803bd74247c1bcba0830a6906a1f1f1`。
使用独立 Git 检出和临时 PostgreSQL、Redis，从原始 JUnit 比对用例身份和结果。
原验收的 67 份证据索引全部通过 SHA-256 校验。[原验收](accept/GA06-GA10/accept.json) · [本轮机器记录](accept/GA06-GA10/independent-20261119.json)

| 卡片 | 注册专项 | 独立工程门 | 验证范围 |
| --- | ---: | --- | --- |
| GA06 | 12/12 | INDEPENDENT_FAIL | 多来源、引用核验和 Markdown 夹具通过。独立 PDF 样例显示正文缺字，公开 Web 为 BLOCKED。 |
| GA07 | 18/18 | INDEPENDENT_PASS | CSV、XLSX、JSON 读取，统计、趋势、图表和 GA05 数值复算通过。桌面 Excel 为 NOT_RUN。 |
| GA08 | 16/16 | INDEPENDENT_FAIL | 夹具授权、审批和对账通过，双项目日历补充项失败。生产账户 E2E 为 NOT_RUN，Google 分享与删除仍未启用。 |
| GA09 | 11/11 | INDEPENDENT_PASS | 现有 CodingLoop 同一 Run 恢复、失败传播、GA05 验证和 H12 工程回归通过。真实模型与发布为 NOT_RUN。 |
| GA10 | 5/5 | INDEPENDENT_PASS | 用户和项目隔离、跨项目显式召回、并发版本修正、撤销、删除及 0062 迁移通过本地回归。 |

## 独立发现

- **GA06 PDF 正文缺字。** 报告标题包含 `𠮷野` 和 `📈` 时，PDF 页面文字为 `�野` 和 `�`，PDF 证据核验仍从文件的文本副本返回原文。渲染器会在写入页面前替换 U+FFFF 以上的字符。修复后需核对 PDF 实际页面的文本及其可见字形，并从 Markdown 与 PDF 用例验证同一引用。[渲染实现](../../src/mgar_v3/general_agent/research_pdf.py#L91) · [补充回执](../../evidence/GA06-GA10/independent-20261119T114142Z-948e6d37/functional-probes.json)
- **GA08 两个已批准的项目动作共享了 Google 事件 ID。** 两项目在同一账户和日历上各创建一个事件，项目内使用同一个幂等键。GA04 的项目级唯一性约束允许两项分别获批，Google 适配器为两项生成同一个事件 ID。第二次 POST 返回 409，对账找不到第二项，Outbox 状态停在 `unknown`。修复要让外部身份绑定持久化的动作身份，派发和对账共用，并能识别旧身份下已提交的动作。[适配器](../../src/mgar_v3/general_agent/office_google.py#L251) · [GA04 约束](../../migrations/0060_general_actions.sql#L36) · [双项目回执](../../evidence/GA06-GA10/independent-office-20261119T114816Z-ec06bb1c/GA08-cross-project-calendar/command.log)

## 回归和外部边界

- 五卡及研究→数据→报告专项共 65/65，混合图 3/3 含伪造上游数值拒绝。Ruff 和 mypy 均通过。
- 372 个常规用例和 77 个 H12 历史工程用例有 6 个重合，合计 443/443 个唯一用例通过。H12 复验读入归档提交 `a91ae4c` 的 379 个已核对文件，复验前后比对 721 个源码和工程文件。H12 真实质量复测为 NOT_RUN。
- 使用原地址策略探测 RFC Editor，DNS 结果未满足全局可路由要求，连接在 TLS/HTTP 前被拒绝。真实模型调用和外部写入均为 0。测试检出、归档重放目录和本轮临时容器已清理。
