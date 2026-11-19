# GA00 独立复验

2026-11-19，GA00 基线冻结与架构盘点 **INDEPENDENT_PASS**，完整工程基线 **FAIL**。GitHub `main` 复查仍为 `6402afeb0257867be88edc1108e37e8e5cf16cc5`，Git Tree 为 `71eda705b72bd27fc74e4441bf053436ead00581`，产品版本 `magr-harness 0.1.0`。本地交付 `90608b5` 只改 9 个文档及验收文件。桌面任务书与入库副本逐字节一致。[GitHub main](https://github.com/Loz888/MAGR-Harness/tree/main) · [原始报告](GA00_BASELINE_20261119.md) · [本轮机器记录](accept/GA00/independent-20261119.json)

## 测试与证据

- 从同一提交新建独立检出，用 Python 3.12、冻结依赖，以及独立 PostgreSQL 和 Redis 复跑。源码指纹前后均为 `51586bf2e7a1bc56559e79ea0a63655b2eafdb4ab998ce67d1e2c70e18854900`。检出在测试前后均干净，复验后隔离工作树与服务已清理。
- 复核原始五份 JUnit 的全部测试标识与退出码及 52 项证据哈希引用。原始计数为 3,001 项，2,785 通过、214 失败、2 错误、0 跳过。独立重新收集得到同一组 3,001 项和 219 个测试模块。
- 独立复跑全部 216 个原未通过节点，结果仍为 214 失败、2 错误，逐项结果类型一致。原已通过的 2,785 项沿用冻结版本的全量证据。Ruff 和 mypy 重新运行通过，H12 耗时准入补充的 23 项通过经原 JUnit 确认。
- 首次复验脚本误把原记录的 `errors` 当作 `error`，在建立工作树或启动测试前退出。修正解析后完成复验。

## 非通过结果归因

| 直接失败原因 | 节点数 | 复验证据 |
| --- | ---: | --- |
| H00 冻结材料或相应异常断言 | 80 | 公开开发文件哈希为 `ab03431d…`，原协议要求 `43c47739…`。私有持出文件缺失；另有 4 项异常断言受目录校验影响。 |
| 历史评测档案与 v34 素材 | 51 | 旧快照、答案档案、历史报价文件或 v34 manifest 节缺失。 |
| V3.5 公开任务字段 | 19 | 4 条公开任务都没有旧加载器强读的 `expected_changed_paths`。包含 2 个 setup 错误和演示命令首次运行失败。 |
| V3.5 旧文档或演示索引断言 | 3 | 测试仍期待旧仓库地址、文档链接或旧验收清单键。 |
| 沙箱锁或环境前置 | 14 | 固定镜像准备检查失败，包含 2 项字面引用 `/home/developer/…` 的锁文件。 |
| P0 调查结果未产生数值溯源 | 3 | 三个实际调查 Run 均保存 `NUMERIC_PROVENANCE_REQUIRED`。 |
| P0 已通过业务结果护栏 | 17 | 下游缺少当前已通过的调查结果，返回 `BUSINESS_RESULT_REQUIRED`。 |
| H12 三个测试辅助调用 | 3 | 旧测试调用 `_base_items(None, bound)`，当前静态方法将 `None` 读作 `TaskBinding`，在业务断言前触发异常。 |
| 陈旧迁移断言 | 2 | 断言固定为 19 项或截止 `0019`，本次冻结版有 35 个迁移文件。 |
| H04 共享 Docker 断言 | 1 | 与基线相同的两个旧容器在此次执行前已存在，测试对全局标签要求结果为空。 |
| 其余行为或断言 | 23 | 包含仓库记忆续问停在 `waiting_input`、重复失败命令未触发预期停机等。保留逐项失败，完整因果仍待核实。 |

P0 的三个代表场景使用原问题调用冻结路由器，均只选择 `repo`。将同一句中的 `order` 改为 `orders` 仅作路由诊断，三例都增加 `data`。真实复跑中，结果提交要求同一输入修订的 `commerce_rows_observed`，三个调查 Run 均因缺少该观察失败。该证据定位了代表用例的上游问题。17 个护栏报错保留其直接原因，未逐个作相同的因果断定。[调查请求](../../src/mgar_v3/commerce/store.py#L711) · [路由](../../src/mgar_v3/routing/experts.py#L244) · [结果校验](../../src/mgar_v3/commerce/results.py#L293) · [H12 辅助接口](../../src/mgar_v3/harness/coding_loop.py#L3630)

## 源码复核

Web 与 Python SDK 均调用 `/api/v3/agent/sessions` 及其消息端点，Headless CLI 通过 SDK 调用这些会话入口。API 验证项目成员，再走 `HarnessProductService.create/send` 与 `CodingTaskRequest`，最终由现有 Harness 运行时校验代码任务回执。CodingLoop 内有 `TaskPlan`、`RunGraph` 和五种编码策略。它们给 GA01 及后续卡片提供适配基础，跨领域任务与结果协议、跨执行器的动态依赖图和通用证据验收仍是缺口。原报告的 18 项架构矩阵计数均与对应源码版本的 JUnit 一致。[架构矩阵](GA00_ARCHITECTURE.md) · [产品路由](../../src/mgar_v3/harness/product_api.py#L185) · [CLI](../../src/mgar_v3/harness/headless.py#L123) · [SDK](../../src/mgar_v3/harness/client.py#L166)

真实模型调用 0 次，真实外部账户端到端为 `NOT_RUN`。停在 GA00，GA01 等待授权。历史失败及其原始回执全部保留。[任务队列](TASKS.md)
