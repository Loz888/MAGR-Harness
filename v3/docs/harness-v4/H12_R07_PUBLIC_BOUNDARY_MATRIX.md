# R07 开发公开边界补充矩阵

版本：`harness-r07-public-boundary-matrix-v1`。这是可复用的公开**合成工程补充集**，
共 6 类边界、5 个项目、Python/Node/Go 三种语言，每类均有真实隔离执行的正反例。
它不改变原 24 题及其分数，不是新持出，也不证明实际 H12 新题集已经重新就绪。

| 类别 / 项目 | 公开检查 | 正例 | 反例必须暴露的问题 |
|---|---|---|---|
| unknown remaining argv / Python CLI | 已识别参数之外的 argv 保持顺序和值；空剩余列表 | parse_known_args 保留未知选项及值 | parse_args 抛 SystemExit，unittest 保留身份并报告 ERROR；不把插入的 usage 输出当作缺失结果 |
| quoted delimiter / Node parser | 单/双引号内分号不切分；保留前后空字段 | 仅在引号外分割 | 简单 split(';') 违反实际公开断言，Node TAP 记录失败 |
| module entry import / Python package | 导入 package.__main__ 不启动入口；轻量导出保持可用 | main 受模块入口条件保护 | 导入时执行入口并抛错，实际 unittest 失败 |
| lightweight import / Go package | 既有导出 LegacyName 可被调用且行为不变 | 旧导出仍存在 | 删除导出导致实际编译阻塞；测试数保持 null，不能报 0 或 PASS |
| readiness / Node HTTP | 动态端口、明确 READY、实际 HTTP body | 正确 Ready/端口/HTTP 观察一致 | 仅 BOOTING 不满足 Ready；使用原失败路径的清理回执，不重复清理已移除容器 |
| cleanup / Node HTTP | SIGTERM 后正常退出、端口释放、无遗留进程 | graceful shutdown | 忽略 SIGTERM 不能通过 graceful 要求；原强制兜底仍完成自有容器/进程清理 |

配置及每份公开资产 hash 位于
[config/h12-r07-public-boundaries.json](../../config/h12-r07-public-boundaries.json)。
公开程序在 [tests/fixtures/h12_r07](../../tests/fixtures/h12_r07/)，实跑目标在
[test_h12_r07_boundaries.py](../../tests/test_h12_r07_boundaries.py)。
单/双引号和空字段是此版本实际披露的 delimiter 向量，不宣称覆盖任意转义/RFC 语法。

命令通过原 IndependentRegressionRunner、Broker 和固定镜像执行；HTTP 使用原
LifecycleRunner/CommandSession 的容器内端口、进程和清理观察，不以字符串“Ready”
单独代替 HTTP/OS 证据。Python 3.12、Node/Go 工具链和依赖沿用已有锁，没有下载。

“反例按预期 FAIL/BLOCKED”是工程断言通过，不是模型题目的 PASS。每个具体结果、
命令、原始日志、镜像和 tree 见 R07 机器验收与 trace-index；不能从本表推算 H12
通过率。冻结前的失败/中断尝试也原样保存。

实际 H12 的后续新候选必须把这些类别落实到各项目公开接口和完整回归计划，复核
R06 基线/逐断言映射并重新冻结；本补充集不提供人工解题补丁或新的独立持出答案。
R08 的原开发回归和全新独立持出质量门继续单独执行，旧历史不回写。
