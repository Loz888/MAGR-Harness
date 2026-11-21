# 第一次正式运行的连接核定

冻结 fef32397d832ef7cf6f8508ec511d59236675c214903003caeccb916b08501f7。唯一 attempt 为 r1-e2-01-8192-1-truncate / 1，request_id a338f64a-73bd-47b3-b5b6-58ee23bc5e99，发送体摘要 43ad0d59720bd3d7016413e5f1be30b3746f255579da7a3cfca2f1a355c0cd28。从 2026-11-20 13:55:48.913525 到 13:56:08.124453 UTC，原始状态 UNKNOWN，原预占 US$0.010893，无 HTTP 状态、无 usage。当前证据可把服务器前的连接失败精确定位到这笔预占。第一次运行结论保持 BLOCKED。

冻结调用选用 HTTPX 0.28.1 的同步 HTTPTransport(retries=0)，显式传入了 transport。该版本 _client.py 的 allow_env_proxies 要求 transport is None，因此当次使用直连，无 HTTP 代理或已复用的连接。HTTPX 将 httpcore.ConnectError 精确映射到当次记录的异常类。httpcore 1.0.9 的同步普通 TCP 后端 _backends/sync.py 的 connect_tcp/start_tls 将 OSError 映射为 ConnectError，读写分别映射为 ReadError/WriteError。该运行未配置 Unix sockets。_sync/connection.py handle_request 先完成 _connect，再执行 HTTP11Connection.handle_request 写入 HTTP 请求。仅一次请求，无入口重试、重定向或代理隧道，不存在先前发送之后再连接的路径。当次的 ConnectError 确认其 HTTP 应用数据尚未发出；究竟 TCP 建连还是 TLS 握手失败无法进一步细分。此结论只针对此序号。

复核使用无凭据探测。直连 DNS 可以解析，TLS 握手超时；环境代理 HTTP HEAD / 返回 401，未调用模型。核定将保留原始 UNKNOWN 回执，追加 NOT_SENT 和零费用，第一次连接占 5760 次总上限中的一次。修正候选使用 HTTPX 环境代理及系统 TLS 校验，所有模型、数据、随机化、工具和门均保持冻结配置。全修正候选采用新的运行 ID；开支与原候选共用 calls 表，独占锁和总预算未改变。后续未确认响应继续停发。
