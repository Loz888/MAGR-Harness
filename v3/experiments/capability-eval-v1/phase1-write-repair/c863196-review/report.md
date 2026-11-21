# E4-R2 写入门独立盲测

冻结候选 `c86319673c8bbfb83428cffb571b0e3e6c616bc3`。结论 **FAIL**。共预注册 360 条，实际尝试 4，有效评分 4，未运行 356，不可判定 0。首次运行失配 1。
立即停止项：['INCORRECT_VERIFIED']。环境或评分阻塞：无。

|指标|分子 / 实测分母|比率|
|---|---:|---:|
| auto_precision | 1 / 2 | 50.00% |
| auto_recall | 1 / 1 | 100.00% |
| confirmed_after_review | 0 / 0 | 不可估 |
| false_reject | 0 / 1 | 0.00% |
| foreign_scope_auto | 0 / 2 | 0.00% |
| high_risk_procedure_confirmation | 0 / 0 | 不可估 |
| human_confirmation_rate | 0 / 4 | 0.00% |
| injection_block | 0 / 0 | 不可估 |
| legitimate_auto_persisted | 1 / 1 | 100.00% |
| persisted_as_expected | 3 / 4 | 75.00% |
| provenance_violation_auto | 0 / 2 | 0.00% |
| self_as_evidence | 0 / 0 | 不可估 |
| sensitive_block | 0 / 1 | 0.00% |
| tri_state_exact | 3 / 4 | 75.00% |
| validation_write_agreement | 4 / 4 | 100.00% |
| verified_mislabel_self_report | 0 / 0 | 不可估 |

每项分母仅含已评分用例。敏感与注入有交集，来源违规与自引用也有交集。置信区间在 metrics.json 中，仅描述参数化样本。授权审核分母为预期需确认样例。未运行项未计为成功或失败。
PostgreSQL 16.12 (Debian 16.12-1.pgdg12+1)。模型调用 0，费用 0，远程写入 0。
源码归档 SHA-256 `1a83aa7da527cdc0b68c9a33fedb918bd5c9323e37734afcfd56412eac24a485`。
预注册 SHA-256 `8e9dfabf7fc384e4d2d7eb3ced7047b6c68d98e94cbf2a042361e8664acb5bd5`。
原始记录 SHA-256 `25a5dd5123e97c5e6af4fc8485a684d79867c0add76cdf92e4bfc743bcc45a2c`。
