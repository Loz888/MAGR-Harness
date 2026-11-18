# HV4-H11 独立复验任务

当前：PASS_AFTER_INDEPENDENT_RECHECK（仅 H11 工程范围）。见 [独立复验报告](accept/HV4-H11/independent-recheck-20261111T134513Z.json)。

历史交付状态（保留）：PENDING_INDEPENDENT_RECHECK。实现方工程门PASS不代表独立放行。

- 仓库 `/home/developer/magr-harness`，工程根 `v3`。
- 仅复验H11；不启动H12、不推送、不发付费调用。
- 源码提交 `d3f3457b3d9dfab68bc73a40add299e4e633fbeb`；标准源码指纹 `a0cc8fac7467f7a99866df01c8ebea9f611f9844c0001c2d4130190fd9676cf2`。
- 先读AGENTS、ADR0001、本目录实施任务卡、RECOVERY和[证据](accept/HV4-H11/accept.json)。
- 保留H10原FAIL、9b52190的F01/F02 scoped PASS、原未跟踪记录与本轮失败attempt。

核验Engine单写者/PG真值、same Run/new epoch、旧Worker拒编辑/确认消息/执行/提交成功、
模型known复用/unknown保留且不重发、部分工具顺序与补丁不重复、manifest/tree/CAS严格绑定、
容器原identity/原provenance、日志先保存再删除、跨进程取消无owned进程残留及收件箱终态竞态。
用自有独立探针复验边界，不能仅复跑注册门就判独立PASS。其他报告的OPEN发现单独关闭。

```bash
cd /home/developer/magr-harness/v3
uv run --frozen python --version
make lint
make typecheck
make verify TASK=HV4-H11
uv run --frozen pytest -q tests/test_harness_v4_h*.py tests/test_h11_loop.py --basetemp=/tmp/magr-h11-review-UNIQUE
uv run --frozen pytest -q tests/test_v33_context_compaction.py tests/test_r4_context.py tests/test_recovery.py --basetemp=/tmp/magr-h11-review-context-UNIQUE
```

将UNIQUE替换为本次唯一值。使用真实PG；专用PG的stop/start仅针对owned fixture，禁止共享实例停启。
当前用户已批准本轮相关PG工程测试与专用实例停启。若新的会话审批不继承，应按实际权限请求
必要测试权限，不通过修改连接认证或全局沙箱设置绕过。凭据只在本机私有文件保存，不输出/提交。
逐例证据由H11_EVIDENCE_DIR选定新的目录，历史attempt不覆盖。预期注册68、聚合349、上下文15、
既有Engine恢复9，均零失败/错误/跳过；先核源码指纹，测试前后均保存。
