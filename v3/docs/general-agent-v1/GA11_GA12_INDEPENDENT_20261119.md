# GA11 与 GA12 独立验收

复验源码 `a995416`，原验收提交 `9b95892`。在独立 Git 检出、锁定依赖、真实 PostgreSQL、pgvector 和 Redis 上复跑，GA11 注册专项 5/5、GA12 注册专项 13/13、原兼容回归 69/69。同原 JUnit 的 87 个唯一节点全部通过，无失败、错误或跳过。Ruff、mypy 和锁文件检查通过。[原验收](accept/GA11-GA12/accept.json) · [本轮机器记录](accept/GA11-GA12/independent-20261119.json)

## GA11 · INDEPENDENT_PASS

本地技术文档 RAG 与项目记忆的独立检索、检索前鉴权、撤权后拒绝读取、版本与内容绑定、精确引用以及单来源降级通过。远程知识服务和生产 Embedding 为 `NOT_RUN`。[接入和专项范围](GA11_KNOWLEDGE_GATEWAY.md)

## GA12 · INDEPENDENT_FAIL

公开开发包主评测 60/60。相同冻结的独立持出包盲跑 60/60。两包各跑五组共 300 条记录，逐条得分与各自的原复验一致，主评测组均为零权限违规和零外部效果错误。持出包的整包哈希、60 个题目承诺、协议摘要和首轮、复验原始回执均完成核对。首轮 59/60 的 Coding 题以 `PATCH_STALE` 失败，同一题在本轮通过。持出题目、标签和单题回执保存在仓库之外。[持出首轮回执](../../experiments/general-agent/ga12/holdout.first.receipt.json) · [持出原复验](../../experiments/general-agent/ga12/holdout.recheck.receipt.json)

**持出准入未通过。** 独立负例只改变公开开发包的 JSON 序列化，每条题目对象保持完全相同。两个文件字节不同，现有冻结入口只比较文件字节，因此接受了全部 60 道开发题，生成 `split=holdout`、`author_role=independent` 的清单并评分为 60/60。已封存持出包与公开开发包逐题比较，去掉题目 ID 后没有完全相同的题目。应以规范化题目内容识别跨集合重复，并在登记或评分时核对独立冻结承诺。[冻结实现](../../src/mgar_v3/quality_eval/ga12_benchmark.py#L78) · [独立负例回执](accept/GA11-GA12/independent-20261119.json)

真实 Provider、真实外部系统均为 `NOT_RUN`。真实模型调用、费用和外部写入均为 0。
