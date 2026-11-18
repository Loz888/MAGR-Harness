# H12 真实质量门独立复验（2026-11-14）

结论：FAIL，编码MVP未放行。同一冻结候选`1e68aef`、实验
`hv4-h12-public-1e68aef`，源码指纹`494ad6d3c0114ecea8e2d8c47900bd34803f364de9cbb37b6f7d40c7c59f94d0`保持一致。
CLI正在执行真实续测时接到复验请求，因此复验者没有重复模型采样；本轮独立
核对真实供应商回执、原PG事实与封树，并在两份独立只读副本上重新执行Judge。

## 真实成绩及独立核对

| 开发题 | 质量结论 | 独立核实的原因 |
| --- | --- | --- |
| Click help | FAIL（追加核账视图） | 原首笔响应丢失，旧UNKNOWN原件保留；CLI核账后没有模型回复可用于完成任务。只读PG确认原Run仍paused/epoch2、仅一次调用、未重发。 |
| Requests URL helper | FAIL | 7次真实调用、11工具；候选调用未导入的urlsplit，公开probe检出NameError，连续无进展停止。独立重跑Judge仍FAIL。 |
| Requests quoted content type | FAIL | 20次真实调用、28工具，耗尽原上限。最终补丁引用未定义的_is_fully_quoted，公开probe出错，独立重跑Judge仍FAIL（2失败/4通过）。 |

开发追加视图0PASS/3FAIL/9NOT_RUN；原始档案仍为0PASS/2FAIL/1UNKNOWN/9NOT_RUN。
两种视图分别保留，不覆盖原件。剩余九题即使全过，最多9/12，低于开发10/12门槛；
因此本冻结提前停止。新独立持出模型12NOT_RUN，门槛9/12不变。

两份已封树样本的实际profile、源码/测试哈希、PG用量与原供应商回执逐项匹配：
共27次调用，均有deepseek-flash身份及已知用量，每个operation仅一次物理尝试。
每份独立Judge执行前后最终树未变化，结果均与原评分一致；未向模型反馈私有输出。
本轮没有修改候选、Harness、账本或核账记录。

## 发现的评分记录问题

`src/mgar_v3/quality_eval/h12_runner.py:1248` 将new_regression_failures与候选是否
completed绑定。引号分号题最终树的原公开suite真实记录为617项中4项失败，
但score.json因candidate=budget_exhausted把该指标写成0。整体FAIL判定仍正确，
该指标却漏报，不能据此声称“无新增回归”。证据、原score和建议修正见机器记录
中的P2项。应从最终树匹配的真实回归回执计算，并区分缺证据与零失败；保留原档案、
追加更正视图。此复验仅记录问题，没有改动冻结实现或评分原件。

## 费用及范围

只读共享预算快照：333次调用、保守已结算US$1.483486 / US$12，预占0、未知0。
CLI本次续测新增27次、保守US$0.166505；复验者新增模型调用0。账单对账由CLI完成，
复验者没有访问供应商账单或再次结算。快照时本实验没有running Run。

工程与准备独立PASS见此前[软件复验](H12_INDEPENDENT_RECHECK_20261114.md)，
不替代本质量FAIL。旧失败、UNKNOWN原件与追加核账视图保留；未启动H13、未推送。
[机器结论](accept/H12-INDEPENDENT-LIVE-20261114/accept.json)；独立原始证据在
`evidence/harness-v4/H12-INDEPENDENT-LIVE-20261114/`。
