# R08 入口日志与进程快照竞态修复

结论：本地工程定向9项通过，真实模型尚未在本修复源码复测；H12质量门和编码MVP仍未放行。
源码提交 `a3499b79cf5674085d7ad2cc6a7dc961e822c430`，源码指纹
`c3e9eef3303d9f61bd4f9b59febfc95d65c8c571c82085c1685056d75bd2c20c`。

开发08原冻结源码ab449b0的模型补丁通过独立Judge和公开回归，但入口检查两次返回
FAIL、exit=0、空说明。CAS原日志 `b360df8df8f8ecc9080ce040580c6fdd86d031ee7a6980566d3e4dd2c4594cd3`
实际含成功的loaded观察和退出0。其后模型把入口receipt_id当成artifact_hash读取，被原
CONTEXT_ARTIFACT_SCOPE_DENIED守卫拒绝。该原FAIL保留，不能把回执落库称为读取成功。

入口观察器先读取日志前缀，再读取进程快照。子进程可以在这两次读取之间退出；此时
快照已经是Z，日志前缀却只有launch，还没有loaded/exit帧。原实现立即按缺少loaded
给出FAIL，而失败说明只使用stdout；无stdout时说明为空。

修复在原入口期限内继续读取，直到出现与同一根PID匹配的退出帧。仍要求唯一退出帧、
退出码与进程快照一致、完整日志、唯一成功加载观察，以及原不监听、租约、固定资产、
树和独立清理条件。没有放松任何路径或产物绑定，也不扩大超时或自动改判历史结果。
无stdout的失败现在说明观察帧计数和退出状态。

复现使用真实隔离子进程，控制一次实际日志前缀早于退出快照，未伪造成功帧。原实现
3项FAIL的JUnit与日志保留；修复后正式`make verify TASK=HV4-H12-R08-ENTRY-REPAIR`
为9 PASS、0 FAIL/ERROR/SKIP，含竞态正例、真实加载失败、缺失加载观察、Python包导入、
缺失模块、Python/Node导入监听拒绝、Node ESM和原权限/租约/资产守卫。lint通过，
mypy 286个源码文件通过。首次注册缺少node selector开关的NOT_RUN记录也保留。

该修复在隔离分支hv4-h12-r08-entry-repair-20261117完成，未换入正在运行的
ab449b0冻结批次，新增模型调用0。未执行工程全量回归、独立持出评分、推送、合并、
部署、发布或H13。未来真实评分仍须独立冻结并保留本轮原始结果。

[机器记录](accept/HV4-H12-R08-ENTRY-REPAIR/accept.json)保存源指纹、每例、实际命令、
原失败和修复后JUnit/日志哈希；[源码](../../src/mgar_v3/harness/lifecycle.py)和
[竞态复现](../../tests/test_h12_r08_entry_observer_race.py)。

原机器记录cases.node_id使用JUnit模块/函数标签；[可直接执行的pytest节点索引](accept/HV4-H12-R08-ENTRY-REPAIR/cases-exact-nodeids.json)
另行补齐文件路径与扩展名。原记录、JUnit、测试计数和源码均未修改。
