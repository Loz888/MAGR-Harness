# H12 真实任务评测

H12 的开发集、持出集、旧版对照及所有候选共用同一 PostgreSQL 授权预算，累计上限
12 USD。新实验 ID、进程重启和重新生成报告均不重置预算。工程测试使用受控模型，
真实成绩单独记录。冻结题集、判定规范和阈值仍以 H00 原件为准。

最新环境修订为dev-v4，完整命令如下（证据目录每次必须新建）：

```bash
uv run --frozen python scripts/h12_preflight.py --split dev --execute-readiness \
  --environment-revision h12-dev-environment-v4 \
  --image-map .local/h12/image-map-20261112-v4-final.json \
  --evidence evidence/harness-v4/HV4-H12-ENV/<new-directory>
```

本轮开发11/12、原持出基础设施10/12就绪；原缺陷基线、资产关联与公开时间断言问题保留。
退出2表示仍未全量就绪，不能派发真实模型。原持出只由隔离控制器核验，不能把dev修订传入持出入口。
见[环境交付与当前限制](H12_ENV_REPAIR_20261112.md)。以下v3/v2及首轮复现约束保留。

2026-11-12 修复使用独立开发环境修订 `h12-dev-environment-v3`，见
[修复说明](H12_REPAIR_20261112.md)。先执行全量零模型实际预检：

```bash
uv run --frozen python scripts/h12_preflight.py --split dev --execute-readiness \
  --image-map .local/h12/image-map-20261112-v3.json \
  --evidence evidence/harness-v4/HV4-H12-REPAIR/<new-readiness-directory>
```

只有该修订下的 12 道开发题全部 READY，新的候选才允许付费；单题入口也不能绕过。
`SOURCE_READY`、镜像准备成功及预期 bugfix 基线失败与模型任务成功分别报告。
新评测必须显式传入 `--environment-revision h12-dev-environment-v3`，绑定新候选源码与
环境锁，共用原预算；已有持出结果被隔离，不能作为本轮开发反馈再评测。
当前仍有真实环境阻塞，未新增模型调用。以下 v2 配置与首轮成绩的复现说明保留。

## 运行与恢复

在 `v3/` 中使用锁定的 Python 3.12 环境。数据库初始化会先以私有文件绑定唯一身份，
随后创建专用数据库并执行迁移；初始化被中断时继续同一身份。配置、凭据与持出判定器
保存在 `.local/h12/`，不提交到 Git。

```bash
uv run --frozen python scripts/h12_eval.py --initialize-db \
  --out evidence/harness-v4/HV4-H12/live-20261112-v1
```

环境准备先核验冻结提交或已有冻结快照。依赖只在准备阶段下载，项目执行和判定均使用
固定镜像、非 root 用户与隔离网络。Python 项目在隔离镜像内安装；该镜像复制自 uv
管理的 Python，因此准备命令显式记录 `--break-system-packages`，宿主环境不参与安装。
环境锁映射以 `repository@base_commit` 为键、项目 `sandbox-lock.json` 绝对路径为值。
缺失源码、依赖、运行时版本或独立判定实现的题目记为 NOT_RUN，分母保持 12。

首轮环境映射 v2 覆盖 14 个源码 pin 中的 13 个。Click 镜像补齐经 Debian 签名索引
和包哈希核验的 less；pflag 使用 `GOMAXPROCS=1 / GOFLAGS=-p=1` 控制编译并发。
Node 缺少原生 package-lock 时，单独冻结环境解析锁，再离线使用 `/node_modules`。
沙箱原有文件描述符、进程、网络和内存上限不提高。源码自身要求更高 Go 版本、
原生符号链接无法保真装载或公开工具受固定资源限制时，保留具体环境阻塞原因。

评测控制器使用 `--image-map` 固定环境、`--experiment-id` 选择不可变候选。
`--initialize-experiment` 将候选源码指纹、模型、判定器与环境锁绑定到 Engine。
`--execute-live --split dev --arm candidate` 执行开发集。单题运行使用 `--task-id`。
每例首次成绩和每次汇总快照均保留；恢复复用选定 Run 及已结算结果，不覆盖失败样本。
源码或环境变化需要另一个候选，仍共用原累计预算。

开发候选冻结后才运行持出集。持出判定器由隔离控制器读取，通过
`--registry` 与 `--registry-sha256` 绑定；只把单例必要目标、公开源码和公开命令送给模型。
持出失败不能作为开发反馈再择优重跑。旧版或其他产品缺少同条件可用适配器时，
对照明确记录 NOT_RUN，不从模型差异推断 Harness 收益。

## 判定与证据

任务经过 H11 Engine、PG、CAS、工作区租约及 Broker/CommandSession。模型给出的完成
只是候选状态；封树后由独立判定器检查行为、公开回归、测试完整性及进程清理。
只有独立 PASS 绑定同一最终树，Engine 才能记录 completed。

每题在付费前先核验独立判定实现，并在基线的只读副本执行已有公开回归。
依赖、收集或资源错误记 NOT_RUN；实际断言失败单独保留，不冒充绿色基线，也不
笼统归为依赖缺失。付费后模型改坏代码或未满足行为要求仍为 FAIL。
HTTP 服务通过实际就绪和清理回执验证；一次性程序沿同一命令生命周期验证退出、
完整日志及所属容器清理，再经过独立行为判定。最终公开回归使用不含私有判定器的
另一份封树副本，避免全仓 lint 扫描判定器造成错误结论。

`provider/` 保存供应商身份、原始用量与规范化费用，不保存密钥或内部推理。
缺 usage 或未知结果保留预占并停止后续付费派发，先对账，不自动重发。
运行时异常也按精确的 Engine Run 读取调用、Token、结算与预占；已经执行的样本记为
FAIL，未知调用记为 UNKNOWN，不因结果保存失败而改成零费用 NOT_RUN。已知失败通过
Engine fencing 收口并保存小型 CAS 异常回执。历史计分错误只追加逐例更正与汇总，
分别记录原执行源码和更正评分器的指纹，保留原成绩且不再次派发模型。
模型和费用沿用获准的 DeepSeek Flash 配置；价格按峰值缓存未命中上界核算，
依据 [DeepSeek 官方价格](https://api-docs.deepseek.com/quick_start/pricing/)，不称为精确账单。

```bash
make verify TASK=HV4-H12
python3 scripts/h12_checks.py aggregate
python3 scripts/h12_checks.py compatibility
python3 scripts/h12_checks.py lint
python3 scripts/h12_checks.py typecheck
```

固定检查脚本保存命令、原始日志、JUnit、源码前后指纹和退出码。
每次检查有唯一临时目录。涉及 Docker 的注册验收和聚合回归顺序执行，因为旧版
清理断言会检查全局 Harness 容器；与同时运行的合法任务并行会造成错误失败。
最终接收摘要位于 `accept/HV4-H12/accept.json`；工程、安全、真实模型、真实 Agent 与
外部发布五门分别报告。开发集至少 10/12、持出集至少 9/12，且误报完成、越权及未知
盲重试均为零，才允许声明本轮编码 MVP 通过。H09/H10 既有 OPEN 项及历史 FAIL 保留。
