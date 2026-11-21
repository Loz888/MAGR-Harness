# MAGR-Harness 实验档案

这里收录记忆与上下文 E1–E5 首轮和能力评测 Phase 0–4 的公开结果。每轮报告保留自己的候选提交、样本分母和验收状态。项目整体发布仍为 **BLOCKED**。[原任务书](PLAN_V1.0.md) · [归档索引与 SHA-256](publication.json)

| 实验 | 结果 | 报告与机器验收 |
| --- | --- | --- |
| E1–E5 首轮 | E1 持出与 E4 写入未过门，E2/E3 完成首轮试样，E5 完成 120,000 次主矩阵请求 | [首轮总报告](../memory-context-v1/acceptance/final_report.md) |
| Phase 0 · 证据冻结 | 八个方向的历史证据绑定通过 | [报告](phase0/report.md) · [验收](phase0/acceptance.json) |
| Phase 1 · 写入与恢复 | E4 首轮和两次修复后的盲测均未过门，最后一轮 38 条已评分、410 条未运行。H18 隔离恢复通过 8/8 | [报告](phase1/report.md) · [E4 修复与验收](phase1-write-repair/report.md) · [H18 独立复验](phase1/independent/h18-report.md) |
| Phase 2 · 检索与上下文 | E1 独立质量门失败。E2 完成 1,440 个真实试次，压缩组未达到非劣门槛。E3 完成 640 个真实会话，调用数减少，成功率收益未建立 | [报告](phase2/report.md) · [验收](phase2/acceptance.json) |
| Phase 3 · 编码与委派 | H12 真实试次 19 PASS、7 FAIL、6 BLOCKED。H17 单 Agent 87/90，委派可用臂 80/90，实际专家调用为 0 | [工程报告](phase3/report.md) · [真实模型报告](phase3/live/report.md) · [真实模型验收](phase3/live/acceptance.json) |
| Phase 4 · 检索性能 | E5-R1 通过本地性能和结果一致性门。172 个组合完成 36,864 次检索，冻结持出回归与原结果一致 | [报告](phase4/report.md) · [验收](phase4/acceptance.json) · [原始性能样本](phase4/samples.tar.gz) |

## 公开复核

可公开的报告、判定、失败记录、方法和样本保留原始字节。索引按相对于 `v3/experiments/` 的路径登记每份文件的 SHA-256。原报告指向本机或独立侧的路径供保管方复核。独立持出原件、逐次供应商回执和账本留在原归档，公开版可查看[压缩核账](phase2/e2/accounting-audit.json)、[跨会话核账](phase2/e3/receipt-audit.json)、[编码与委派费用复算](phase3/live/tariff-audit.json)和[历史证据复核](phase4/archive-audit.json)。

部分复现脚本依赖冻结候选、映像和本机证据。候选提交号按实验时的源码记录。此归档提交仅包含实验资料及导航。
