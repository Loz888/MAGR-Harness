# GA02 局部重规划定向修复

重规划保存后调用现有任务阶段推进逻辑，并传入刚刚保存的新计划版本。删除最后一个可选等待节点时，已成功节点的交付物、回执、认领和累计资源完整保留，任务进入 `verifying`。

使用独立报告里的同一失败场景复测，修复候选与最终集成源码分别为 1/1。只执行新回归用例，已通过的 GA02 用例沿用原验收记录。原独立失败报告保留，修复已通过独立定向复验。[原独立复验](GA02_GA03_INDEPENDENT_20261119.md) · [修复回执](accept/GA02/replan-repair-20261119.json) · [最终集成回执](accept/GA05/accept.json)

```bash
make verify TASK=GA02-REPLAN-REPAIR
```

[独立复验](GA02_GA04_GA05_INDEPENDENT_20261119.md)
