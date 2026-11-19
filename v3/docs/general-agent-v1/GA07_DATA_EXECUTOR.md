# GA07 Data Analysis Executor

GA07 读取 CAS 固定的 CSV、XLSX 和 JSON 数据，生成数值证据、CSV 表格、XLSX 工作簿、SVG 图表和 Markdown 报告。清单记录原始数据哈希、标准化数据哈希、实现哈希、参数、种子、样本计数与各产物哈希。固定输入及参数下，重复执行得到相同产物字节。[数据执行器](../../src/mgar_v3/general_agent/data_analysis.py)

## 数据与执行边界

- 数据来源使用任务声明中的 `SourceRef`，`kind` 为 `dataset` 或 `artifact`，`version` 为原始数据的 SHA256。分析输入为一个 CAS 内容对象。任务图认领绑定任务修订、节点、能力和交付物 ID。
- 分析参数包括指标、sum、mean 或 count、分组列、ISO 日期列和等值过滤条件。报告展示输入行数、通过过滤的行数、有效样本量、缺失量、最小值、最大值和 IQR 异常数。均值计算使用 50 位有效十进制数字。种子进入清单，当前计算不采样。
- Linux 子进程接收已授权的内容字节和固定的分析参数，最多处理 1 MB、1 万行、32 列、10 万单元格的数据。CPU、地址空间和执行时长设有上限。固定 Python 流程执行数值计算，参数绑定的内存 SQL 只做分组排序。工作进程拒绝文件、网络和外部数据库访问。
- CSV 和 JSON 使用单表标量记录，列名使用通用任务标识符。XLSX 处理首个工作表的连续表头及数据，拒绝宏、公式、外部关系、越界归档成员与超限解压内容。趋势日期使用 `YYYY-MM-DD` 文本。CSV 对可被表格程序解释为公式的字符串做引号前缀处理，XLSX 使用文本单元格。[格式实现](../../src/mgar_v3/general_agent/data_formats.py) · [固定分析执行](../../src/mgar_v3/general_agent/data_worker.py)

## 任务图与证据

可信执行服务调用 `DataAnalysisExecutor.handler(request, outputs=...)`，由 `engine.general_executors.run` 派发 `data.analyze`。声明的每个节点输出 ID 对应一种产物，可用格式为 `numeric`、`csv`、`xlsx`、`chart`、`report` 和 `manifest`。

GA05 的 Numeric 契约使用 `numeric` JSON 作为当前表格或图表结果的机器核验产物。图表、工作簿、报告和数据清单可通过附加节点输出交付。独立验证端可从原始输入复算 `verify_manifest`，核对整个产物清单，再用 `numeric_evidence` 提交归一化样本和 Numeric 检查。原始输入继续保留，便于核对过滤条件和标准化过程。[GA05 契约](GA05_VERIFICATION_CONTRACT.md)

## 验收与复验

```bash
make lint
make typecheck
make verify TASK=GA07
```

专项覆盖描述性统计、分组和趋势、CSV/XLSX/JSON 解析、超限和无权限输入、宏及外部关系拒绝、工作簿与 SVG 结构、重复执行、真实 PostgreSQL 任务图派发和 GA05 结论发布。测试使用授权本地夹具，外部服务及真实模型为 `NOT_RUN`。
