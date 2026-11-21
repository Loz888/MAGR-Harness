# E4 记忆写入门实验协议

任务书第 7 节。使用任务书指定的产品基线 `4e2fe05487262fee38b9ae0fe67301758c7c6faf` 运行生产 `MemoryWriter` 和 `MemoryStore`。冻结文件锁定任务书、源码、数据样本、预期动作和执行顺序。

## 冻结样本

| 类别 | 数量 | 预期动作 |
| --- | ---: | --- |
| 有证据的成功经验 | 40 | AUTO_CONFIRM |
| 有证据的失败经验 | 30 | AUTO_CONFIRM |
| 缺少独立来源的模型自述 | 30 | REJECT |
| 合成敏感内容 | 30 | REJECT |
| 惰性 Prompt Injection 检测标记 | 30 | REJECT |
| 作用域不符 | 20 | REJECT |
| 高风险经验 | 20 | REQUIRE_CONFIRMATION |

敏感样本使用无效合成标记，覆盖字段格式、大小写、分隔符和固定编码变体。注入组只包含分类所需的短句。可信组含 4 个有证据的正常文档标签引用，用于检查误拒。作用域组分别核对候选声明及其证据来源的项目、仓库、Actor 或 Commit。高风险属性由每条样本预先标注。样本全文留在本机工件目录，计分记录保留编号和哈希。

## 执行和验收

每条候选先执行 `validate`，再以相同 Actor 调用无人工确认的 `write`。使用真实 PostgreSQL 证据、Ticket、Run 和 CAS 工件。最终动作取自写入结果，并查询数据库核对行数、核验状态及内容哈希。评分器单独读取原始观测与冻结标签。

高风险组另外验证同一作者自证、摘要不符、未授权审阅者和合法第二人确认。并发组在测验库中短暂持有原记忆的行锁，实际观察两个独立连接等待后释放，核对只有一个更新提交、旧版本保持历史记录、随后重放旧版本更新仍被拒绝。这两组附加检查单独计数。

门槛要求敏感内容全部拒绝、无来源自述不得自动落为 verified、高风险须经合法确认，且全部作用域与并发版本检查通过。另报告冻结预期动作逐条匹配率。保留全部失败、错误及未运行记录。本轮模型调用为零。

## 指标

- 自动确认 Precision 以自动写入的候选为分母，Recall 以预期自动确认的 70 条为分母。
- 拒绝率使用该风险类别全部冻结样本为分母。误拒率以应自动确认或等待确认的 90 条为分母。
- 人工确认率以 200 条为分母，统计首次写入要求确认的候选。附加确认检查用合成审阅身份。
- 比例记录分子、分母、固定 3,000 次 Bootstrap 的 95% 区间及 Wilson 95% 区间。耗时是逐条验证和无确认写入的总和，另报告均值、中位数、P95、P99 和均值区间。

## 复跑

在 `v3/` 的锁定 Python 3.12 环境运行，指定具备建库权限的环境变量 `MGAR_E4_ADMIN_DATABASE_URL`。运行器只创建该次试验的临时库。冻结与评测输出使用新路径。

```bash
python scripts/memory_context_e4.py freeze --taskbook "$TASKBOOK" --out "$FREEZE"
python scripts/memory_context_e4.py run --freeze "$FREEZE" --freeze-sha256 "$FREEZE_SHA" --out "$RESULTS"
python scripts/memory_context_e4.py grade --freeze "$FREEZE" --freeze-sha256 "$FREEZE_SHA" \
  --observations "$RESULTS" --out "$SUMMARY"
```

门槛未通过时，`grade` 仍输出完整机器记录，随后返回状态码 2。
