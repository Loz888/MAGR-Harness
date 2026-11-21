# Phase 3 Coding 与 Multi-Agent · 2026-11-21

后续真实模型复测已归档。以下是此前本地工程记录。 [真实模型复测报告](live/report.md)

**本地工程矩阵通过。正式真实模型评分为 NOT_RUN。** 本轮使用实际 PostgreSQL、固定离线沙箱和有界夹具模型。付费调用为 0，供应商费用为 US$0。Phase 2 的 E2/E3 授权只覆盖当时的实验。

- **H12-R09**。完整文件大 Patch 通过 4/8。分块、结构化方案、分块校验并恢复各通过 8/8。8 次恢复留在原 Run。20 个受控截断回复均未派发。预检在模型前检出 9/9 个故障，3 个正常环境均放行，按提示恢复后 9/9 通过原检查。已保留 H12 R08 的 22/24 历史记录。[报告](h12/report.md)
- **H17-R1**。30 题 × 3 种顺序，单 Agent 和委派臂各 90/90。交付逐对一致。委派把夹具调用从 360 次增加至 540 次，费用增加 50%，超过预注册的 25% 上限。单 Agent 保持默认。[报告](h17/report.md)

历史原件齐备的隔离检出上，H12-R02 注册回归 347/347，H17 注册回归 18/18。Ruff、mypy 和实验脚本 Ruff 检查通过。独立操作者复核登记为 NOT_RUN。[冻结协议](protocol.md) · [机器验收](acceptance.json) · [原始日志索引](evidence-index.json)

复现需提供已锁定的离线镜像和对应的 H06 沙箱锁。工程入口为 `.venv/bin/python experiments/capability-eval-v1/phase3/run.py`，参数为 `h12 ATTEMPT` 或 `h17 ATTEMPT`。可用 `--slice INDEX 6` 重现 H17 的六分片安排。汇总程序核对原始执行和评分回执。[运行器](run.py) · [汇总器](summarize.py)
