# HV4-H12-R01：Acceptance Contract完整性与一致传递

状态：DONE / ENGINEERING_PASS。用户于2026-11-15明确授权“启动R01”。依赖R00本地验收PASS；
起点7789be3，标准源码指纹6bff0896ec4c15e8c3322d7c8688afd88b8a6676d8fb8fe09f227600abaa4636。
本轮只实施R01，不启动R02–R08、付费矩阵、真实发布或H13，原H12/MVP FAIL保留。

## 原任务卡完整要求

定义acceptance_id、acceptance_revision、acceptance_hash、required_checks、
required_artifacts、scope、baseline_commit和task_type。LLM仅可建议补充，不能删除/
降低既有required。Planner、Worker Prompt Builder、Context Compactor、Verifier、
Scorer必须记录并引用同一revision/hash。投影/压缩不得丢required行为；用户追加目标
创建新revision，记录原因及受影响检查，不覆盖旧契约。每项required有稳定ID，可追踪
到计划、实际测试回执及最终判定。缺字段、删除项、篡改revision/hash执行前FAIL/BLOCKED。

必测：Prompt Projection丢字段、Compaction丢字段、Planner/Verifier版本不一致、
模型删验收项、多次压缩、用户追加指令时旧回复不得覆盖新目标，以及公开自检任务中
“打印实际观察到的HTTP状态”完整进入Worker上下文。

## 实施边界

复用现有TaskPlan/Checklist、WorkingState、PG Run事件和CAS/恢复检查点，不创建第二
业务真相；不改Engine/API接线、来源事件DTO、SQL迁移、权限或预算算法。历史运行配置、
原score及原源码不回写；旧档案可读取，缺契约的执行不能被当作新契约验收通过。

契约区分完整性绑定与行为验证。公开自然语言required均有稳定ID及现有必需检查的
证据关联，不能把完整性回执当作每条自然语言行为已经被测试。具体入口/Ready探针、
独立全回归与Judge语义覆盖仍按R04/R05/R06处理，不在本卡偷改或注入隐藏判定。

通过真实注册make verify TASK=HV4-H12-R01及lint/typecheck；必要恢复/并发检查用
独立临时PostgreSQL。原H12共享账本只读核对，起点813次/保守US$4.211490，未知/预占0。
证据写入evidence/harness-v4/HV4-H12-R01/<attempt>/，失败尝试保留。

来源为用户HV4_H12_R_Systematic_Repair_Taskbook.md的R01、全局规则与验收要求；
SHA256 d7c4bc793b25b894fd98d1169e042a1bddedd3eff08e69a0cc52339ab7bc4d35。

## 本地验收结果

源码cc626d81ba3ef07061b5e720cc2f6c273cdab3de，指纹
e0f38c877dea8be31435de9866401d76621bc7403210f4c89d0fd6b112251729。
注册206项全部通过、零失败/错误/跳过，lint和mypy通过。原128份score哈希不变；
原共享预算仍813次/US$4.211490、未知/预占0。本轮新增真实模型调用0。

[完整报告](H12_R01_ACCEPTANCE_CONTRACT_20261115.md) ·
[机器接收记录](accept/HV4-H12-R01/accept.json) · [协议说明](ACCEPTANCE_CONTRACT.md)。
本卡完成后停止；R02–R08待单独授权，原H12/MVP FAIL不改变。
