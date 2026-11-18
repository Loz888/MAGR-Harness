# HV4-H12-R00 基线冻结与证据核验（2026-11-15）

**本地R00验收：PASS。已完成本卡并停止，R01–R08未启动。**
本结论仅覆盖原始证据保全、确定性诊断与工程核验，不改变H12真实质量FAIL、
MVP未放行、H13未启动或真实发布NOT_RUN。

## 固定起点与版本

本轮在独立分支`hv4-h12-r00-20261115`执行。开始时H12修复工作树干净；主仓库
原未跟踪H10记录保留。三份关键源码均从不可变Git字节按原标准指纹算法重算：

| 版本 | Git提交 | 标准源码SHA256 |
| --- | --- | --- |
| 主轮真实模型 | d272a01c577a9f7e3dcc826c569eaad28e3adb6d | b7dfff2c71320db22c3f5e541b4ba72e878475041a22d3c54ceb034ff1f84629 |
| 三题首样本补测 | 0655b59210d247cede5a43fab3c8279ebf3dc2d6 | 14379894ecc51a06c005d3244052f11e2f1d58ceb575182bec7accf4563591e4 |
| R00冻结起点 | 3804a401c2d52fd18291990c9a886b4772db420a | 81f17c419139c832ee20199d9390b3f941445079af55978ffdc3bbdb69134cc8 |

R00工程实现为`f3390e6367b3232cf54e3c0fa572b3df591ef8fa`，其指纹为
`6bff0896ec4c15e8c3322d7c8688afd88b8a6676d8fb8fe09f227600abaa4636`。
只增加R00离线工具、公开synthetic夹具、测试与真实验收注册；原src、migrations、web
业务文件与冻结起点一致。最终40项检查前后指纹相同。没有将R00实现冒充原模型执行源码。

## 冻结与核验结果

- 四个Harness工作树的相关历史证据已读取并备份。归档99,971条来源记录、35,782个
  去重内容对象，包含score、供应商与工具回执、完整命令/测试/Judge日志、补丁、核账、
  原CAS与持久工作区资料。复制对象为只读，来源逐份重新计算SHA256，原输入变化0。
- 原PG使用REPEATABLE READ READ ONLY读取，保存17个实验、82个任务、813个模型用量
  记录与2,475个操作事实；不是新业务数据库。H12隔离PG已有31份迁移，最高0037，
  与基线SQL逐份校验一致；没有执行DDL或业务写入。
- 原全局CAS的11,228个对象均与内容哈希相符。三份恢复锁文件按辅助文件另存。
  4,774条类型化PG/CAS/工作区引用、2,374个不同哈希均找到已归档材料，缺失0。
  workspace manifest为语义标识，不能当成原始字节CAS哈希；三个旧暂停任务的对应
  manifest保存在原request中，已重算语义哈希，未恢复、修改或取消这些旧任务。
- 保留128份分版本原评分：28 PASS、51 FAIL、48 NOT_RUN、1个历史UNKNOWN。
  这是不同候选和旧状态的记录数量，不能相加作为新通过率。历史UNKNOWN原文件保留，
  当前账本用量未知仍为0。主轮开发6/12、持出6/12及三条NOT_RUN不改；补测2PASS/1FAIL
  也不拼回主轮。
- 原始索引v1保留。四份旧Click记录缺少仓库提交元数据，追加v2索引从唯一匹配的已归档
  任务定义补齐，128份所需字段完整；逐项核对状态、原因、Run、费用及证据哈希不变。
  没有使用临时本地工作区的零commit冒充任务声明的Git基线。
- 冻结前后共享账本均为813次、保守US$4.211490/US$12、未知0、预占0。
  R00新增真实模型调用、付费和外部发布均为0；未创建新题、Run或另一预算真相。

原始私有归档目录：

`/home/developer/magr-harness-h12-repair/v3/evidence/harness-v4/HV4-H12-R00/20261115T093316Z-attempt-005/`

## 诊断夹具与工程验收

公开目录只有八类synthetic观察：契约丢项、输出截断、无效工具JSON、路径拒绝、
Ready缺项、回归漏跑、Feature预期红基线、Refactor错误结构前置。每类有正反控制，
不包含真实隐藏答案或私有Judge实现。私有任务书全文、PG、CAS及Judge留在控制器归档，
不复制到模型工作区。模型适配器测试只使用MockTransport；默认真实供应商网络也被测试守卫阻止。

| 命令/核验 | 结果 | 证据 |
| --- | --- | --- |
| make verify TASK=HV4-H12-R00 | PASS，40测试，0失败/错误/跳过 | test-results.json、JUnit及原日志 |
| make lint | PASS | lint-final.log |
| make typecheck | PASS，256源码文件 | typecheck-final.log |
| 归档只读、哈希、原文件不变 | PASS | evidence-manifest.json、accept.json |
| 八类协议问题与反例、状态语义 | PASS，零真实模型调用 | diagnostic-fixtures/、注册测试 |
| 类型化PG/CAS/manifest引用 | PASS，4,774引用、缺失0 | pg-cas-reference-audit-v2.json |

NOT_RUN表示未执行；BLOCKED表示存在阻塞或缺少可核验证据；FAIL表示已执行检查失败。
不存在“没执行/没回执所以PASS”的路径。私有夹具拒绝被当作公开诊断输入，归档的缺失、
篡改、可写对象、外部路径、schema错误和重复来源都有反例。

归档捕获使用当时39项验证的R00版本；完成后只追加元数据补充、类型化引用审计和最终
40项检查。原归档清单及原v1索引保持只读。最终实现与捕获版本在commands.json中分别记录，
不重写原模型执行版本或混合模型成绩。

## 失败尝试及来源差异

四个早期R00尝试分别保留：测试启动路径造成收集错误、一个lint长行、只读SQL的LIKE
占位符、将恢复锁文件误作CAS。最初指纹重建草案多加依赖分隔符，已纠正并核对三份
标准指纹。草案PG引用审计把语义manifest/工作区字节都当全局CAS，保留原草案并追加
正确类型审计。以上均为R00归档/核验工具问题，未造成模型重试、费用或历史证据修改。

用户任务书引用的TASKS与ACCEPTANCE标识在本地是Git blob，与本次实际基线文档不同，
差异已记录。所引README对象本地不存在，远端比对明确为NOT_RUN，不折算PASS；本轮
冻结使用当前实际本地文件与提交。该限制不会被写成远端来源已经验证。

这是同一CLI的本地机械与工程核验，不是外部独立验收或新的模型能力评分。更早候选的
其它源码指纹按原回执保留，本轮只对上述三份关键源码实际重建；未声称已重建全部历史
源码。旧暂停任务未恢复，未做新的安全总体验收，也未运行任何后续卡。

## 交付入口与停止点

- [R00验收记录](accept/HV4-H12-R00/accept.json)
- [baseline.json](accept/HV4-H12-R00/baseline.json)
- [完整h12-case-index.json](accept/HV4-H12-R00/h12-case-index.json)
- [source-manifest.json](accept/HV4-H12-R00/source-manifest.json)
- [八类公开诊断与回放结果](accept/HV4-H12-R00/diagnostic-fixtures/replay-results.json)
- [私有完整evidence-manifest引用及哈希](accept/HV4-H12-R00/evidence-manifest.json)
- [命令记录](accept/HV4-H12-R00/commands.json)与[测试结果](accept/HV4-H12-R00/test-results.json)
- [任务书来源差异](accept/HV4-H12-R00/basis-differences.json)
- [仅R00授权的任务卡](CODEX_CLI_H12_R00_TASK.md)

**已停止。R01–R08均为NOT_RUN，H12/MVP仍FAIL，H13未启动。后续实施需新的明确批准。**
