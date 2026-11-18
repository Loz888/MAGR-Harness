# H12 新公开候选：软件前置已补齐，首笔用量未知待对账

结论：软件准备门PASS；新真实质量UNKNOWN，历史H12质量FAIL、编码MVP仍未放行。
最终实现提交1e68aef，源指纹
494ad6d3c0114ecea8e2d8c47900bd34803f364de9cbb37b6f7d40c7c59f94d0。

## 已完成

正式三Python与一Go公开计划、完整12题准备、dev-only v2冻结与评分器接线均已完成。
实际构建与测试分门，scratch256MiB与原独立准备对齐；保护目录只暴露空只读遮罩，
真实内容不可见；pytest缓存覆盖只读及窄写任务并保留镜像原配置。旧模型/工具/
Token/费用/期限上限及开发10/12、持出9/12门槛不变。

最终源码subset-v4四题PUBLIC_SUBSET_READY、full-dev-v3十二题FULL_DEV_READY，
前后同源指纹；两报告已按真实回执、原树、镜像、租约、scope、命令、完整日志及
24个实际profile重新核对，封存FROZEN，SHA256
3190e0e7ba222d9ea74f12ae0867004cf45c43f4c8ec575871bdfbe3ab29cfca。
原永久H12_FULL_DEV_READINESS_REQUIRED软件阻断已替换为证据验证门。

新增75个不同工程节点分阶段通过，首次失败与定向复验保留；全局Ruff/mypy245
源码文件通过。未重跑已PASS128或全工程聚合，不伪称最后75整套在同源一次重跑。
源1e68aef的完整12物理预检是真实新执行，不复用旧8/12或前一源码的12/12冒充。

## 真实评分与当前停止点

新实验hv4-h12-public-1e68aef复用原共享PG授权。第一题Click的首笔调用尝试进入
UNKNOWN，供应商未返回id/model/usage，未取得任何可执行模型回复。仅执行了两条
原树基线命令，没有模型补丁。新dev为0PASS/0FAIL/1UNKNOWN/11NOT_RUN；独立持出
模型NOT_RUN。原0PASS/3FAIL/9NOT_RUN及全部旧历史保留。

原先305次派发、保守已结算US$1.316981；现账本306次（含这一UNKNOWN），
保留预占US$0.069831，已结算仍US$1.316981，上界含预占US$1.386812 / US$12。
这笔实际费用未知，不能将未知Token或费用填零，也没有退款或重发。

本地call_id 80889fc4-1460-430d-989c-b48aba6658ab；operation
h08-f6b0045e9392:model:1；Run e16e53f9-ca4d-4366-b414-ed530c9f78e2。
任务已fence到epoch2/paused，后续付费停止。

## 连接诊断与责任

本次操作启动脚本只继承基本环境，漏传原HTTP_PROXY/HTTPS_PROXY/NO_PROXY等，
是本轮执行配置错误，已在ignored操作脚本修正并通过受控环境传递测试。
无鉴权、无模型生成的GET诊断：不继承代理ConnectTimeout（8.05秒），继承代理
得到HTTP401（0.186秒），证明后者成功到达服务器。[HTTPX官方环境说明](https://www.python-httpx.org/environment_variables/)。

原模型请求只保留通用中断结果，没有记录原HTTP异常类或供应商标识；上述诊断
支持网络路径解释，不能证明原POST绝未到达供应商。因此没有凭推测清掉预占，
也没有为“验证修复”再发一笔模型调用。

## 继续所需证据

需核对供应商北京时间2026-11-13 21:21:20—21:21:40（UTC13:21:20—40）的该key
请求/Token/计费记录；无记录也需明确的操作员核对结论。随后只由原Engine依据
真实证据对账，保留旧UNKNOWN与恢复轨迹，不重发原请求。依据[H12执行卡](CODEX_CLI_H12_TASK.md)：
“未知调用保留预占且停止进一步付费派发，不重发、不切供应商绕过。”

本次未推送、未修改在线主线/SA13/0046或主业务库，未停止外部容器，未读取持出
或私有Judge实现作开发反馈，未启动H13。[机器验收记录](accept/H12-FULL-DEV-REPAIR-20261113/accept.json)。
