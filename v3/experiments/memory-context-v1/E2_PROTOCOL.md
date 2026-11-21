# E2 Context Compaction 协议

实验从公开 MAGR-Harness 提交 `4e2fe05487262fee38b9ae0fe67301758c7c6faf` 派生。输入、评分标签、实验实现源码、任务书、依赖锁和 tokenizer 的 SHA-256 写入冻结清单。运行目录使用新路径。正式发布质量门单独记录。

## 开发包与对照

开发包包含 30 条合成长任务。每条分别校准到约 8,192、16,384 和 32,768 个完整请求参考 Token，形成 90 条输入和 360 个四组对照。每档分别包含 24、40、64 条观察。观察来自固定提交的公开源码片段、合成测试回执、失败日志及重复读取。关键回执分布在早、中、近三个阶段。任务状态保留提交、用户约束、计划、Evidence、待批准动作、待答问题、工具集合和预算。

| 条件 | 上下文策略 |
| --- | --- |
| `full_history` | 完整保留原始历史，保留相同的系统指令和任务外层 |
| `truncate` | 按同一目标预算保留尾部连续观察 |
| `summary` | 最近四条保留正文，较早观察提取首行最多 160 字，按同一目标预算收缩 |
| `magr_compaction` | 使用生产 `ContextBuilder`、结构化不变量和原 CAS 分页，将参考 tokenizer 传入原预算接口 |

三组处理臂共享目标上下文预算，为当前档位的 55%。完整历史组允许使用完整输入。工具 Schema、系统 Prompt、模型、最高思考档、输出上限、模型和工具调用限额四组一致。普通摘要采用确定性抽取，运行成本为零。只读工具接收当前上下文实际可见的 Evidence ID，并核对当前样本的工件绑定。所有臂都能重读明确保留的关键回执。

## 计量和判定

- 参考计数使用 [DeepSeek 官方 Flash-1017 tokenizer 和模板](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017/tree/1bae0e1088993b9b82de08f59de5be5a34e42170) 与 `tokenizers==0.23.1`，将真实请求中的消息和工具声明按官方模板渲染。历史 Token 为原请求和清空历史观察、压缩摘要及其元数据后的参考计数差值。系统、记忆和工具结果的单独参考计数用于诊断。
- 真实采样保留供应商 `usage`、调用 ID、回执 ID、渲染计数差值和请求哈希。首请求总输入 Token 降幅与全部调用累计输入降幅分别计算。返回的 usage 覆盖计费请求整体。
- 机械核验记录权限、工具与剩余额度是否变化，并分别检验约束、失败、审批、悬而未决的问题及 Evidence 保留。原 Artifact 通过原分页器多页重组，与输入原字节和哈希核对。恢复率分母包括所有原始引用。
- 任务评分以冻结的十个字段逐项核对最终 JSON。运行器接收输入及授权，独立评分命令读取预期答案。所有失败、未知和未运行均计入样本总数。开发包和规则由本分支编制，正式独立持出由独立方另行封存。
- 参考 Token 用任务级配对，以三档为同一任务簇，固定随机种子重采样 5,000 次。真实试采样仅一条任务。正式阶段在盲测前冻结非劣效界值、样本量与独立复验方案。

## 真实试采样范围

试采样固定 `e2-01` 的 8K 输入。四组使用 `deepseek-flash`，thinking enabled / max，模型返回上限 4,096 Token。每组可进行一次本地只读分页，随后最多有一次模型续行。累计最多 8 次调用、US$0.25，按官方峰值无缓存价预占，调用前落盘。用量缺失保留原预占并停止剩余调用。记录的费用为峰值上界，同时保存供应商缓存计数供核账。真实请求含冻结公开源码、合成历史、工具定义和该试验的模型续行上下文。授权文件将冻结 SHA-256、期限、上限和唯一运行目录绑定。 [DeepSeek 价格](https://api-docs.deepseek.com/quick_start/pricing/) · [思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)

## 复现

在 `v3` 目录创建独立实验工作树并使用锁定环境。下载本节所列官方提交中的 `tokenizer.json` 和 `encoding/encoding.py`，将两文件放在同一个目录。脚本会核对两文件的哈希。以下操作使用新目录。

```bash
make verify TASK=MEMORY-CONTEXT-E2
export E2_DIR="$PWD/.local/memory-context-v1"
uv run --frozen --with tokenizers==0.23.1 python scripts/memory_context_e2.py freeze \
  --tokenizer "$E2_DIR/tokenizer" --dataset "$E2_DIR/dataset"
freeze_sha=$(sha256sum "$E2_DIR/dataset/freeze.json" | cut -d ' ' -f 1)
uv run --frozen --with tokenizers==0.23.1 python scripts/memory_context_e2.py offline \
  --tokenizer "$E2_DIR/tokenizer" --dataset "$E2_DIR/dataset" \
  --freeze-sha256 "$freeze_sha" --out "$E2_DIR/offline"
```

真实模式为 `pilot`，使用相同 tokenizer、输入和冻结哈希，另传 `--authorization` 和该文件授权的 `--out`。密钥通过进程环境 `DEEPSEEK_API_KEY` 提供。用 `grade --dataset … --freeze-sha256 … --run … --out …` 对真实输出离线判定并保留原记录。日志目录含 `raw.jsonl`、`usage.jsonl` 与 `summary.json`。发生中断时依原始账本核对请求，保留部分结果。
