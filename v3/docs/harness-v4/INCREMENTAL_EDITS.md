# HV4-H05 版本化增量编辑

## 契约

一次修复仍返回 `{"summary", "edits"}`。每个 `FileEdit` 新增两种形态：

- 完整文件（历史兼容）：`operation=update` + `content`；
- 增量替换：`operation=update` + `replacements`；每个替换为
  `{"old_sha256", "old_text", "new_text", "occurrence"?}`。

`replacements` 的约束：

- `old_sha256` 必须等于 `sha256(old_text)`，片段与文件一样带版本校验；
- `old_text` 必须非空，默认必须在当前文件中唯一；
- 同一片段出现多次时，`occurrence`（1 起）显式选中一次，缺省则
  `PATCH_AMBIGUOUS`；
- 多个替换按顺序应用，任一失败则整组不写入；
- 不允许原始 diff、`hunks`、行号或 `command`/`shell`/`argv`。

`expected_sha256` 仍是文件前像校验：文件在读取后变化即 `PATCH_STALE`，
旧版本计划不会被重放。重复提交同一计划时前像已变，同样拒绝，不会重复插入。

## 大文件

输出预算按 `content` 或 `new_text` 的实际输出字节计算，不按文件大小计算。
因此 142KB 以上的文件只改动少量片段时，模型输出随改动规模增长，无需重发全文；
仍受 `max_file_bytes`、`max_changed_lines`、`max_total_bytes`、
`max_replacements` 约束。

## 文本与定位

- UTF-8 原样处理，不做编码降级；
- 一致的 CRLF 文件按 LF 归一化匹配，再按原换行符写回，宿主字节保持 CRLF；
- 混合换行文件按精确匹配处理，无法匹配时确定性拒绝。

## 原子性、底稿与候选

- `apply_patch` 先在内存中应用全部编辑，全部成功后才一次性落盘；
  第二个文件失败时整组回滚，工作树与磁盘都不变；
- 失败后固定底稿仍在 baseline/固定提交中；
- 成功应用生成 `PatchReceipt`：前后文件 sha256、真实工作树 Diff 的 sha256；
- `candidate()` 保留最近一次候选内容，即使随后 `revert_patch()` 仍可复核；
- `get_diff()` 从真实工作树读取后像，不以内存镜像代替。

## 能力边界

- 创建受 `allow_new_files` 约束；
- 删除受 `max_deleted_files` 约束，默认 0；
- 重命名受 `allow_rename` 约束，默认关闭；源与目标都必须落在授权路径且
  不受保护规则命中，目标已存在则 `PATCH_CONFLICT`；
- 受保护配置文件与授权路径外文件仍由既有规则拒绝。

## 限制

- 增量协议只做精确片段替换，不做 AST 重写；
- 重命名在 Diff 中表现为一次删除加一次创建；
- H05 不新增执行入口，不接触 H04 Broker 的 lease/epoch/预算；
- 修复级审批对象不在本卡范围；任何后续审批都必须绑定工作树/前像哈希，
  当前实现以 `expected_sha256` 与前像树拒绝陈旧计划。
