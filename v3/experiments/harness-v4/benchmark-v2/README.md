# H12 基准 v2 草案

状态：DRAFT_NOT_FROZEN，不能用于付费派发或新质量分数。用户2026-11-12明确同意
另建修订基准并保留原历史。原H00字节、评分门槛和预算身份不改。

`dev.jsonl`保留原十一题，替换原Requests content-type无缺陷基线题。新题仍为同一
真实提交、同一源码范围和资源上限，要求正确解析引号内的分号/等号。专用判定器
在原始源码上实测4条缺陷断言失败、2条兼容断言通过，原题不是被改记为通过。

`got-public-amendments.json`是只作用于独占临时评测副本的公开测试修订，绑定原项目
提交、文件前像和新文件SHA256。它不修改项目实现或原冻结测试：

- backoffLimit测试观察实际计算出的退避值，并验证三次真实HTTP尝试；不再把OS调度
  延迟当作退避值。随机噪声和真实定时器保留。Node官方明确不保证回调精确时刻，见
  [Timers](https://nodejs.org/api/timers.html#settimeoutcallback-delay-args)。
- 错误密码测试增加同一密钥/证书的正确密码正例，再要求错误密码被拒绝；接受先前
  真实诊断已观察到的OpenSSL解码拒绝码。原完整失败与原随机性审计保留。

这两项改变了基准测试契约，必须作为v2报告，不能把v2全绿解释为原v1全部通过。
三轮完整公开命令各746普通通过、1 known failure、4 skipped，原始日志位于
`evidence/harness-v4/HV4-H12-RECHECK-20261112/got-full-v2-3/`。这些是公开项目回归，
不是三轮真实模型求解。既有计数器的total包含skipped/known failure；已修正摘要
错误地将total写成通过数的问题，保留原回执。

零模型开发草案预检：

```bash
uv run --frozen python scripts/h12_preflight.py --split dev --execute-readiness \
  --environment-revision h12-dev-environment-v4 \
  --image-map .local/h12/image-map-20261112-v4-final.json \
  --draft-proposal experiments/harness-v4/benchmark-v2/proposal.json \
  --evidence evidence/harness-v4/HV4-H12-RECHECK-20261112/<new-attempt>
```

草案即便全部就绪也只返回`DRAFT_READY`，不能满足真实模型入口要求的`READY`。
新持出集、完整判定器正反例验证、正式版本派发接线及冻结验收仍须完成；不能只改
proposal状态字符串来开放付费入口。
