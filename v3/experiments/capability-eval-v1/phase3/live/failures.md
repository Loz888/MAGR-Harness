# 失败与受阻索引

表中逐试次状态来自冻结矩阵的输出判定。初期实验运行器问题保留全部已付请求，详见协议。 UNKNOWN 供应商回执由独立账本约束。

| 实验 | 试次 | 策略 | 状态 | 终态原因 |
| --- | --- | --- | --- | --- |
| h12 | p3-patch-01 / 2103 | verified_resume | FAIL | model_output_continuation_limit |
| h12 | p3-patch-02 / 2103 | verified_resume | FAIL | model_output_continuation_limit |
| h12 | p3-patch-03 / 2103 | chunked | BLOCKED | PHASE3_BUDGET |
| h12 | p3-patch-04 / 2103 | verified_resume | FAIL | PHASE3_BUDGET |
| h12 | p3-patch-04 / 2103 | chunked | BLOCKED | PHASE3_BUDGET |
| h12 | p3-patch-05 / 2103 | chunked | FAIL | PHASE3_BUDGET |
| h12 | p3-patch-05 / 2103 | verified_resume | BLOCKED | PHASE3_BUDGET |
| h12 | p3-patch-06 / 2103 | verified_resume | FAIL | PHASE3_BUDGET |
| h12 | p3-patch-06 / 2103 | full_file | BLOCKED | RECOVERY_BLOCKED |
| h12 | p3-patch-07 / 2103 | verified_resume | FAIL | model_output_continuation_limit |
| h12 | p3-patch-07 / 2103 | structured_plan | BLOCKED | PHASE3_BUDGET |
| h12 | p3-patch-08 / 2103 | verified_resume | FAIL | model_output_continuation_limit |
| h12 | p3-patch-08 / 2103 | chunked | BLOCKED | PHASE3_BUDGET |
| h17 | p3-h17-11 / 419 | delegate | FAIL | non_recoverable_policy_denial |
| h17 | p3-h17-17 / 17 | single | FAIL | PHASE3_BUDGET |
| h17 | p3-h17-17 / 17 | delegate | FAIL | non_recoverable_policy_denial |
| h17 | p3-h17-13 / 9841 | delegate | FAIL | PHASE3_BUDGET |
| h17 | p3-h17-15 / 419 | delegate | FAIL | PHASE3_BUDGET |
| h17 | p3-h17-19 / 9841 | delegate | FAIL | PHASE3_BUDGET |
| h17 | p3-h17-12 / 419 | delegate | FAIL | non_recoverable_policy_denial |
| h17 | p3-h17-19 / 419 | delegate | FAIL | model_output_continuation_limit |
| h17 | p3-h17-17 / 419 | single | FAIL | PHASE3_BUDGET |
| h17 | p3-h17-11 / 17 | delegate | FAIL | PHASE3_BUDGET |
| h17 | p3-h17-19 / 17 | single | FAIL | PHASE3_BUDGET |
| h17 | p3-h17-19 / 17 | delegate | FAIL | PHASE3_BUDGET |
| h17 | p3-h17-14 / 17 | delegate | FAIL | non_recoverable_policy_denial |
