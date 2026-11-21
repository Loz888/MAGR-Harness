# E3-R1 预检、会话失败与缺测

- evidence/CAPABILITY-PHASE2/e3/preflight-1.log，本地会话凭据长度校验失败，SHA-256 `31e8630901ea228175bcec95d89d3f312bda91cf8e23a8ce1bb3642eea49c684`。
- evidence/CAPABILITY-PHASE2/e3/preflight-2.log，回执或证据文件解析遇到不完整 JSON，SHA-256 `613f6d46579f49d53ba0fa12c35fca3b945685025e427eb36794c30e5e1c5692`。
- evidence/CAPABILITY-PHASE2/e3/preflight-3.log，PostgreSQL 连接中断，SHA-256 `e27269b10b4e3022b0e329dabb642d58874e8d27c4c589f987ac02cb2463d2fe`。

## fixture

缺少 0 个计划会话，全部缺失键保存在 metrics.json。

## formal

缺少 0 个计划会话，全部缺失键保存在 metrics.json。
- e3-h35 / bm25 / S1，最终成功 True，会话原因 None，动作错误 INVALID_MODEL_REPLY，旧版本尝试 0。
- e3-r1-d11 / hybrid_reranked / S3，最终成功 True，会话原因 None，动作错误 INVALID_MODEL_REPLY，旧版本尝试 0。

账本未决或拒绝原因按已观察状态汇总。

{}

未决请求 0，已完成会话账本不符 0，缺完整会话的请求 0。
