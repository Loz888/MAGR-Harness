# GA00 独立复验入口

在保留本地原始证据的检出中，从 `v3/` 核对冻结 Git Tree、证据 SHA-256 和分片计数。

```bash
python3 docs/general-agent-v1/recheck_ga00.py
```

复跑注册工程基线时，从本仓库另建隔离工作树，检出 `6402afeb0257867be88edc1108e37e8e5cf16cc5`，进入 `v3/`，用 Python 3.12 和 `uv sync --frozen` 安装依赖。配置隔离的 pgvector PostgreSQL 16、Redis 7.4 和随机本地鉴权令牌，使用短临时路径以满足 Unix socket 长度限制。测试内的会话级数据库由 pytest 单独创建和清理。

准备已记录 SHA-256 的 R03、R5 和 H12 工程用镜像锁，及 H12 公共开发镜像映射。公开开发源码按固定提交恢复到 `.local/h12/sources/`。测试使用的绝对路径、公开快照里缺少的历史材料和共享 Docker 资源依赖依本轮报告逐项复核。独立持出的私有材料由原评测方保管。全部锁定输入及现次记录见机器验收索引。[机器验收记录](accept/GA00/accept.json)

使用仅包含隔离测试 DSN、Redis URL 和本地令牌的环境运行。

```bash
export MGAR_V3_ALLOW_PAID=0 MGAR_V3_ALLOW_EXTERNAL_WRITE=0
export TMPDIR="$(mktemp -d /tmp/g0.XXXXXX)"
mkdir -p evidence/GA00-independent
uv run --frozen pytest --collect-only -q > evidence/GA00-independent/collection.log
uv run --frozen pytest -q -o junit_family=legacy \
  --junitxml=evidence/GA00-independent/junit.xml \
  > evidence/GA00-independent/command.log 2>&1
```

若按本轮方式并行，机器记录引用了原始 `shards.json`，其五组文件可以直接传给 pytest。每组使用独立的测试数据库和不同 Redis 库，保留各组退出码。复验按原收集集核对全部节点并分别统计通过、失败、错误和跳过。Ruff 和 mypy 可分别执行 `make lint` 和 `make typecheck`。单独保存本轮平台、工具、镜像和快照标识。
