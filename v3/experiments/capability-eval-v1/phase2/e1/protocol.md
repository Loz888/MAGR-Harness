# E1-R1 冻结与复算

从公开 Git 对象重建 1,080 条历史记忆；字节哈希与历史公开清单一致。新开发和独立方分别添加 252 和 270 条记忆，共同形成 1,602 条快照。新样本包括公开工程源码检索和合成版本、交接、权限任务，外推到自然产生的企业工作流仍需独立数据。开发集包含 21 个主题、315 条查询，独立集包含 210 条查询。独立方先封存持出，另一复核者独立核过开发标签，随后冻结候选与判定门。旧私有查询、旧私有标签未用于本轮。

开发基线提交为 `d4fee2f555c5e031720302e98ba164cf1bd8db74`，冻结候选另绑定未提交源码、语料、分拆、模型、评分器和依赖指纹。清单的 SHA-256 为 `733b51b422c90da9166380a711b8922a9d90863f6ff8e703db7401d54d0f283f`。运行时复核所有指纹。共享公开语料，模型输入仅可接触同项目、库、Actor 的已授权子集。记录全部已授权原始排序，各臂统一评 Top 10。年龄本身不会给记忆加权。C 融合两种排序；D 结合原始学习分数、BM25、融合排序及文本支持；E 修正来源状态与相对请求 Commit 的版本；F 检查首位证据的绝对文本支持及版本。

质量门要求开发与持出分别满足 Recall@5 比 A 高至少 5pp、MRR@10 配对区间下限大于零、无答案 FPR 至多 10%、可答覆盖至少 90%、ACL 泄漏为零。选择结果前同一主题内的查询保持配对，以主题为单位有放回重采样 10,000 次。区间报告实际观测群组间变动，单一主题与稀疏分层的估计只适用于冻结样本。正标签给出 1–3 级相关项，未标 ID 计 0。Recall、Precision、MRR、nDCG 在可答查询上作宏平均，可答拒答计零；过期、错版本污染以实际返回槽位为分母，并列报告。候选、各阶段原始输出和最终返回均可按 ID 复核，范围过滤日志在同一范围内按引用去重。

在 v3 根目录使用已锁定的实验环境。开发已产生一次正式输出。复算请指定新的输出目录。
 
```bash
export PYTHONPATH=src PYTHONHASHSEED=0 OPENBLAS_NUM_THREADS=1
e1=experiments/capability-eval-v1/phase2/e1
model_python=.local/memory-context-v1/e1-model-env/bin/python
seal=733b51b422c90da9166380a711b8922a9d90863f6ff8e703db7401d54d0f283f
"$model_python" "$e1/run.py" rank --freeze "$seal" --split development \
  --queries "$e1/development.queries.jsonl" --out .local/r1-reproduction
"$model_python" "$e1/run.py" score --freeze "$seal" --split development \
  --queries "$e1/development.queries.jsonl" --labels "$e1/development.labels.jsonl" \
  --out .local/r1-reproduction
```

正式数据开放前，非评测连通性检查曾因一个临时本地会话令牌长度不足而中止。修正该启动配置后，两个独立于评测题的样例通过 PostgreSQL、真实本地模型、原始结果写入和评分全流程。上述修正在候选冻结前完成。本地 PostgreSQL 默认角色无建库权限，正式两臂分拆均运行于自建的新 pgvector 容器，没有连接或修改现有库。

[候选清单](e1-r1-manifest.json) · [开发标注复核](development-label-review.json) · [独立持出承诺](holdout-commitment.json) · [开发结果](development/report.md)
