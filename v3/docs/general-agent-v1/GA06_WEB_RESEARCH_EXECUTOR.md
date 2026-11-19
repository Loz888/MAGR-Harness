# GA06 Web Research Executor

`ResearchExecutor` 把问题拆分、网页快照、引用核验和报告交付接入通用任务图。支持 technical、industry、comparison 场景。报告逐条标注直接引述、推断、观点和未确认信息，保留有分歧的读数和每个来源的访问时间、可信级别、原始快照哈希。发布日期取自可识别的原页元数据，缺失时标为未知，超过计划时效时标为较早发布。[执行器实现](../../src/mgar_v3/general_agent/research.py)

## 入口与授权

受信服务构造 `ResearchExecutor(engine.artifacts, reader, catalog)`。其中 `reader` 是带部署域名许可的 `PublicWebReader`，`catalog` 是运营方登记的 `CatalogSource` 元组，按标题、主题和地址查询。来源可来自登记目录或任务显式声明的获准 HTTPS 地址。跟随链接时检查原页确实包含该链接，计划内的自动发现限定在目录中。网页读取只发送 GET，校验 DNS 返回地址、固定 TLS 连接地址并限制重定向、响应体和时间。 [读取实现](../../src/mgar_v3/general_agent/research_web.py)

```python
engine.general_executors.run(
    task_id,
    task_revision,
    graph_revision,
    node_id,
    capability_access,
    execute=research.node_handler,
    accepted_capabilities=("research.sources", "research.report"),
)
```

`research.sources` 把快照集合写入 CAS，中间节点交付集合哈希。报告节点可使用图中绑定的集合，交付 Markdown 或计划指定的 PDF。受信调用方也可直接使用 `collect(plan, source_refs)` 和 `synthesize(plan, collection)` 生成 `ResearchResult`，内含快照、引用、两种报告哈希。可选 CAS 研究计划通过节点 `plan` 输入传入，任务须显式声明对应的 `artifact` 来源。

GA05 验证使用 `CitationBinding.verification(step_id, output_id, collector)` 生成引用步骤与观察。可发布任务的 `SourceRef` 需登记实际 URI，版本取原始网页字节的 `sha256:<hex>`。来源可先发现并形成候选快照，待版本登记后进入引用验收。PDF 的引用核对从报告的有界文本记录重新生成整份产物，逐字节匹配后再比较引用，篡改的文件无法通过。[PDF 实现](../../src/mgar_v3/general_agent/research_pdf.py)

## 验收与复验

```bash
make verify TASK=GA06
```

本地专项包含三种报告类别、内容去重、时间与冲突标注、虚假引用拒绝、越界重定向、私网解析拒绝、Markdown/PDF 和真实 PostgreSQL 上的任务图与 GA05 完成路径。公开 Web 试探单独记录为 `BLOCKED`。隔离环境对 RFC Editor 的解析结果没有全部满足全局可路由条件，读取器在 TLS/HTTP 发起前按地址策略终止。 [探测地址](https://www.rfc-editor.org/rfc/rfc20.txt)
