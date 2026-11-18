# 公开快照与验证

H12 R08 正式质量门通过独立复核。开发集 11/12，门槛 10/12。独立持出 11/12，门槛 9/12。24 题合计通过 22 题，23 题实际调用模型；持出第 12 题在公开基线阶段因缺少项目内依赖而阻塞，正式计 FAIL。197 次模型请求的保守费用合计 US$1.669227，共享账本累计 US$7.906475 / US$12。产品发布状态为 `NOT_RELEASED`。[正式成绩](v3/docs/harness-v4/H12_R08_FORMAL_REAL_MODEL_20261118.md) · [独立验收报告](v3/docs/harness-v4/H12_R08_INDEPENDENT_MATRIX_REVIEW_20261118.md) · [匿名回执](v3/docs/harness-v4/accept/HV4-H12-R08-FORMAL-20261118/independent-review.json)

## 来源与工程范围

公开树合并了 H12 正式分支、产品主分支截至 SA22 的接口交接、入口观察器分支的复核记录以及 H10 独立复验记录。共核对 3,645 条项目路径，公开 1,476 条，另有本说明与机器索引两份新文件。原始实现哈希、各目录历史 Git 树标识、责任范围及公开摘录的原始与发布版 SHA256 可在索引中查验。历史报告中的提交号和得分按原实验冻结来源理解。[机器索引](PUBLICATION.json) · [Harness 路线](v3/docs/harness-v4/README.md) · [产品接口交接](v3/docs/harness-v4/SUPER_AGENT_HANDOFF.md)

真实模型成绩对应原实验的冻结 Worker `33e39a4c18043613965d4490fd4001b3b5da5524`。公开整合树的工程验收对应初始公开提交 `2fab3577a802a75b8ecfe2b466b43d45eb2cf0b0`。本次成绩与独立复核文档归档于原项目提交 `66dd504048d3d0c53f9cc81103f1ee8f8b7680a0`。复核核对了 24 个首样本、501 个 CAS 对象和 651 个引用文件，复核新增模型调用数为 0。[机器验收记录](v3/docs/harness-v4/accept/HV4-H12-R08-FORMAL-20261118/accept.json)

## 本地验证

运行环境为 Python 3.12。进入 `v3/` 后，可运行以下独立检查。两处帮助入口均会显示可用参数。

```bash
uv sync --frozen --group dev
export PYTHONPATH="$PWD:$PWD/src"
uv run --frozen python -m pytest -q experiments/harness-v4/r08-timing-admission-v1/test_timing_admission.py
uv run --frozen python scripts/h12_r08.py --help
uv run --frozen python experiments/harness-v4/r08-timing-admission-v1/timing_admission.py --help
```

产品端按应用说明执行 `make setup`、`make deps-up`、`make migrate`。需要执行沙箱测试时先准备项目指定的基础镜像，然后运行 `make sandbox-build-r03`。公开开发环境的 Docker 测试另需按环境准备说明生成本机镜像映射。发布时在隔离检出中用已核对的公开开发镜像、R03 锁和临时 pgvector 实例收集到 2,909 个测试。实际运行的 76 项全部通过，其中 23 项验证耗时规则。其余测试在本次发布中仅收集。实际模型成绩另见上方两份实验记录。[应用设置](v3/README.md) · [H12 环境准备](v3/docs/harness-v4/H12_ENV_REPAIR_20261112.md)

<a id="archive"></a>

## 原始记录与公开摘录

独立持出题、答案和样本专用判定程序由独立评测方保管。公开开发目录收录目标、基线与公共验收条件，通用判定器实现和公开合成测试均可查看。完整历史计分需要其原始冻结清单、授权、镜像和隔离材料。H12 R00 历史归因工具支持通过 `--private-cause-map` 读取受控的旧持出归因表；完整原版身份可用来源哈希核对。

本机配置、账户凭据、服务状态、逐次供应商回执、原始账本和 CAS 单据保留在原归档。公开工程报告按责任阶段保留状态、判定边界、开发集结果和已披露费用汇总。H11 原始证据树共有 1,665 个跟踪条目，五份复核工具已转成可移植路径。复核工具使用 `H11_REVIEW_WORKSPACE` 指定原历史工作区。机器索引提供证据树哈希、文件类型计数及独立探针记录。历史状态字段按记录统计，同一项检查可以在多份记录中出现。[公开索引](PUBLICATION.json)
