# H12 R08 公开探针的子进程诊断

R08第二轮零模型集成补充：原dev11-v2资产的负对照被严格单帧解析器拒绝（BLOCKED），
因为原始stderr行出现在帧外。该失败记录保留，没有进入任何付费实验。当前修复改为
可选`PublicProbeFrame.diagnostics`：同一JSON行内最多两个stdout/stderr流，各64KiB
原始字节以base64保存，严格验证长度、编码、唯一流和截断计数。800字符摘要不变。
不放松原单帧、额外stderr、重复键/帧、源hash与权限校验；旧帧不序列化新增字段。
旧dev11-v2资产是被否决的准备提案，必须由新的资产哈希和计划替换后再冻结。

开发题 11 原结果仍为 FAIL。原公开回归 751 项通过，但新增 selfcheck 的独立行为
失败：模型补丁传 `retry: 0`，固定 Got 版本要求对象，真实 stderr 明确报
`Option 'retry': Expected value which is plain object, received value of type number`。
随后模型在源码工作区搜索不存在的 `node_modules/tsx/dist` 并耗尽纠错额度。

公开探针本来已使用固定 `node --import=tsx/esm` 正确加载入口；它把子进程输出保存在
CompletedProcess，却用没有消息的 `assert success.returncode == 0 ...` 检查结果。
外层只返回 `str(AssertionError)`，最终只留下空 `detail`。模型看到的公开错误缺少
实际 retry 参数错误，不是应该增加安装权限或把依赖目录挂进源码读取工具的证据。

新增通用资产准备器 `scripts/h12_public_probe_diagnostics.py` 与
`quality_eval/public_probe_diagnostics.py`：要求原公开资产SHA256，输出到新文件；
只调整已识别 reporter 的异常详情。空断言消息从本次公共 check 的 traceback 中
读取真实 CompletedProcess 的退出码及有界 stderr/stdout，总详情最多800字符。
已有消息保持原样；未知模板拒绝。原 check 函数的AST、所有断言与固定命令完全不变，
不改变通过条件、权限、模型预算或命令允许清单。

已生成可审查的新公开资产 `.local/h12/r08-next-public-probes/dev11-v2/probe.py`，新SHA256
`1d566d8e22824c2fbd20e5ffa66a29857ac9700707b6d6e640c61e2032fa2846`。
初版仅含摘要的资产及其attempt-002记录保留；最新版见attempt-003/asset-generation.json。
同目录 semantic-preservation.json 核对整个 check AST 相同。它尚未接入任何旧计划：
使用时必须绑定新计划与新候选冻结。原33e3b916…资产、04ad15c候选和真实评分未改。

4项定向检查全部通过：真实本地子进程失败能输出实际stderr且断言仍失败；超长输出
受800字符限制；原PASS与显式失败消息不变；未知reporter被拒绝。没有供应商请求，
没有重跑已通过的模型题或完整工程套件。证据为上述 attempt-001 的JUnit/日志。
make lint、make typecheck（286个源码文件）通过；只修复lint后重跑lint，没有重复4项
已通过测试。另补2项原日志检查：最多各保留64KiB的子进程stdout/stderr到父命令日志，
由原CommandSession/CAS归档；明确标注total_bytes、retained_bytes、truncated。3KB日志
完整保留，90KB日志明确标记截断。摘要仍最多800字符，失败状态不变。仅执行这2个新增
检查，未重复前4项。全部四组后续修复共24项相关检查分批通过，不称同一提交完整工程验收。

此修复补齐公开诊断，不把原错误补丁人工改为合格候选，不改其历史FAIL，也不构成
H12/MVP通过。新候选仍须真实模型验证该反馈能否帮助模型完成任务。
