# GA12 通用 Agent 评测套件

GA12 在 Coding、Research、Data、Office、Autonomy 和 Cross-domain 六类任务上分别评分。公开开发集每类 10 题，共 60 题。独立编制方按相同的[题目协议](GA12_CASE_CONTRACT.md)封存了另 60 题，每类 10 题。每题固定初始状态、授权、工具环境和独立预期，执行器只接收输入及授权字段。

## 冻结与复现

- 公开开发包 [dev.jsonl](../../experiments/general-agent/ga12/dev.jsonl) 的 SHA-256 为 `34054a7d8c322cda8914d4a3f1949583e383022bd8d22573921625ab43c14b63`。[开发冻结清单](../../experiments/general-agent/ga12/dev.freeze.json)
- 独立持出包的 SHA-256 为 `7cf0a5d3e30252a0de870f578548648c7facbe78478681b755894efd220ac730`，先于评分冻结。题目、标签、单题结果和 CAS 只由独立方保管。库中保留六类数量、整包哈希及不透明题目承诺。[独立持出承诺](../../experiments/general-agent/ga12/holdout.commitment.json)
- 运行器重算整包 SHA-256、题目协议 SHA-256 和六类计数。持出原件和详细回执必须位于仓库之外。公开开发原包即使复制到仓库之外也会被拒绝用于持出。独立作者身份和编制过程由独立回执及操作者复核确认。清单中的角色字段只描述申报角色。

在 `v3` 目录使用锁定的 Python 3.12 环境。输出目录需使用新路径，已有回执会保留。

```bash
make verify TASK=GA12
.venv/bin/python scripts/ga12_benchmark.py run \
  --freeze experiments/general-agent/ga12/dev.freeze.json \
  --bundle experiments/general-agent/ga12/dev.jsonl \
  --out evidence/GA12/dev-reproduction
```

独立编制方使用本地私有路径运行以下命令。首次运行前执行 `freeze`，复验继续使用已冻结的清单。

```bash
.venv/bin/python scripts/ga12_benchmark.py freeze \
  --split holdout --bundle "$PRIVATE_CASES" --out "$PRIVATE_FREEZE"
.venv/bin/python scripts/ga12_benchmark.py run \
  --freeze "$PRIVATE_FREEZE" --bundle "$PRIVATE_CASES" --out "$PRIVATE_OUTPUT"
```

`raw.jsonl` 按题记录六类计分、工具轨迹、终态、耗时、供应商、模型、调用数、Token、费用、权限制约与外部效果。证据 CAS 和 Coding 产物放在各题运行目录。评分器重新读取 CAS 和文件内容，检查证据与操作、产物、动作收据的绑定。汇总报告记录源码模块哈希、运行时与每组分布，保留各消融组的预期失败。耗时来自本机单次执行，按纳秒记录。

## 公开开发集结果

每格为该领域通过题数，满分 10。主评测组为 `multi_domain`，四个消融保留同一基准和判定器。

| 运行组 | Coding | Research | Data | Office | Autonomy | Cross-domain |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 多领域 | 10 | 10 | 10 | 10 | 10 | 10 |
| 无 Planner | 0 | 0 | 0 | 0 | 0 | 0 |
| 无 Memory | 7 | 10 | 10 | 10 | 10 | 10 |
| 无 Reranker | 10 | 6 | 10 | 10 | 10 | 8 |
| 单领域 | 10 | 10 | 10 | 10 | 10 | 2 |

多领域组各计分项均为 60/60，无权限违规、外部效果错误。Office 模拟供应商接受 6 次动作，其中 2 次因无法对账而保留 `unknown`。无 Planner 组没有生成计划，任务终态和值仍有 52/60 正确。无 Memory 组失去 3 个 Coding 夹具的项目提示。无 Reranker 组仍筛掉不可信来源，按输入顺序选取首个可信结果，导致 6 题的结果及证据偏离冻结值。单领域组限制为首个执行器，导致 8 道跨域题无法完成所需的后续处理。

公开复验使用 `evidence/GA12/fixture-dev-final/`。其 `summary.json` SHA-256 为 `c448f003021207e0b6c0e57b843d3b28314d66dd2c0c19dda81aea850acbe9d6`，`raw.jsonl` SHA-256 为 `81c7e0c7b3ef6f48cce3eaebf857819bfe2e99069135ffa49d8492985e0a906c`。五组共 300 条实测记录。

## 独立持出

首轮参考组为 59/60。Coding 为 9/10，其余五类均为 10/10。权限违规与外部效果错误均为 0。原始失败回执已留存。独立侧报告了字面替换必须保留行尾字节的合同要求。GA12 夹具兼容了工作区文本接口的换行归一化，公开回归验证混合行尾和 Unicode 字节原样保存。独立复验沿用同一持出冻结包和判定规则。[首轮独立回执](../../experiments/general-agent/ga12/holdout.first.receipt.json)

独立侧在隔离检出的 `a995416` 上复跑同一冻结包的五组 300 条记录。主评测组 60/60 通过，各组权限违规与外部效果错误均为 0。此结果记录对原持出包的修复复验。[独立复验回执](../../experiments/general-agent/ga12/holdout.recheck.receipt.json)

| 运行组 | Coding | Research | Data | Office | Autonomy | Cross-domain |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 多领域 | 10 | 10 | 10 | 10 | 10 | 10 |
| 无 Planner | 0 | 0 | 0 | 0 | 0 | 0 |
| 无 Memory | 6 | 10 | 10 | 10 | 10 | 10 |
| 无 Reranker | 10 | 7 | 10 | 10 | 10 | 9 |
| 单领域 | 10 | 10 | 10 | 10 | 10 | 3 |

## 执行环境

| 模式 | 本轮结果 | 测量范围 |
| --- | --- | --- |
| `mock` | 有实测，见分领域结果 | 本地确定性夹具，复用 Harness 任务图、能力路由、工作区编辑、动作合同及 CAS |
| `real-provider` | `NOT_RUN` | 入口单独写出状态回执，当前无模型运行器 |
| `real-external` | `NOT_RUN` | 入口单独写出状态回执，当前无外部账户运行器 |

规划由固定步骤构建，记忆提示、排名、外部供应商及恢复由夹具模拟。没有通过完整生产运行时发起模型规划、完整知识检索、数据库恢复、外部发送或账户操作。真实模型、Token、费用和真实外部写入均为 0。这里的分数可用于验证评测协议、作用域和被模拟的场景，生产端到端仍需独立验收。
