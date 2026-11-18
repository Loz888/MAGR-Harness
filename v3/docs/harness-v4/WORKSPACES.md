# Harness V4 工作区与变更保护（HV4-H03）

- 契约版本：`harness-workspace-v1`（工作区 manifest/registration/apply receipt）
- 代码：`src/mgar_v3/harness/workspace.py`
- 测试：`tests/test_harness_v4_h03.py`（25 项，真实文件与真实 Git 进程）
- 注册验收：`config/verification.json` 的 `HV4-H03` 条目
- 依赖：H01 `harness-contract-v1.1`；未修改 H01 已接收接口

## 1. 项目注册与 workspace_id

本机项目必须由操作者显式注册绝对路径；相对路径、symlink 根目录和非目录直接拒绝。
注册产生内容寻址的 `workspace_id`（kind/root/repository/base commit），把
`registration.json` 写入 registry 根，文件包含 registration hash。浏览器传入的绝对
路径不会自动获得权限；该路径必须先经过注册调用。

固定 Commit 的 URL 工作区由 `register_github(url, commit, actor)` 注册；commit 必须
是 40 位 SHA，repository 通过现有 `parse_repository_url` 规范化。

## 2. 隔离与 dirty 处理

每个任务获得独立副本：`<registry>/<workspace_id>/tasks/<task-key>`；两个任务目录
互不覆盖，任务修改不会写回源目录。GitHub 工作区复用
`repository_analysis.workspace.RepositoryWorkspace`：只从 Git 对象库 materialize
固定提交，不执行 `git checkout`，hooks/filters/LFS smudge 不运行，symlink 与
submodule（mode 120000/160000）进入排除清单。

本机项目是工作树副本（不含 `.git`）；注册时读取 HEAD 树与 index，再逐文件比较
工作树 blob，覆盖 staged-only、unstaged、mixed、added、deleted、renamed
（相同 blob 配对）、mode/type change 与 untracked，形成 `DirtyInventory`
（entry 数量、tracked/untracked 计数、inventory hash）。`git rm --cached` 后保留
工作文件时同时记录 `deleted` 与 `untracked` 两条事实，不把脏工作区报告为干净；
普通工作树删除记为 `deleted` 且无 sha256。index/untracked 的所有工作树读取都走
逐级 `_safe_target`；父目录被替换为 symlink 时记为 `typechange`，绝不读取项目外
内容。该路径不调用 `git status`/checkout，dirty 内容随副本一起复制，源目录在
apply-back 之前保持不变。

## 3. 冲突检测与回写

`apply_back(target_root, expected_preimages)` 只在以下全部成立时写入：

1. `target_root` 的路径 hash 等于注册时保存的 hash（不接受任意目录）。
2. 每个变更路径都出现在 `expected_preimages`，且期望值等于任务基线 hash
   （新建文件为 `None`）。
3. 源目录当前文件的 SHA-256 与期望 pre-image 完全一致，且路径不经过 symlink。

任一条件失败返回 `ApplyReceipt(status="conflict")`，列出 `stale`、`missing`、
`stale_workspace` 或 `symlink`，**不写入任何文件**。同仓库两个任务先后回写时，
第二个任务看到 pre-image 已变化，得到 conflict；候选修改仍保留在任务工作区，
不静默 reset、覆盖或丢弃用户改动。

并发回写在源目录 inode 的跨进程 `flock` 临界区内完成：文件以 `O_WRONLY` 打开
但不截断，取得锁后重新校验 inode 与 pre-image，全部匹配才 `ftruncate`/写入。
两个独立 `WorkspaceRegistry` 或两个进程同时回写同一文件时，一个 applied、
另一个 conflict，不会发生后者覆盖前者的丢失更新。

manifest 保存 `baseline_hash`。打开工作区与每次 `apply_back` 前都会重新读取
`.mgar/baseline.json` 并重算 digest；不一致返回 `WORKSPACE_MANIFEST_MISMATCH`，
不会用被篡改的基线授权覆盖用户较新的修改。`.mgar` 与 `.git` 一样禁止作为
工作区路径读写。该自 hash 提供篡改检测（tamper-evident），不是签名存储；
任务目录仍由操作者控制。

## 4. 路径与执行边界

- 绝对路径、`..`、反斜杠与 NUL/控制字符由 `normalize_relative_path` 拒绝。
- 任何路径段为 `.git` 直接拒绝；读写都不触碰 Git 元数据。
- 读取、写入、回写路径逐段检查 symlink；写入使用 `O_NOFOLLOW`。
- `search` 的根路径同样逐段检查 symlink，并跳过嵌套 symlink 目录；指向任务外
  目录的链接不会返回外部内容。
- 本机所有 Git 调用集中经过一个命令构造器：`core.hooksPath=<empty>`、
  `core.fsmonitor=false`、`core.untrackedCache=false`、
  `submodule.recurse=false`、`fetch.recurseSubmodules=false`、`diff.external=`
  与环境隔离；读 HEAD/index 使用不触发转换的 plumbing 命令。GitHub 工作区复用
  `repository_analysis.workspace` 的既有 hooks/filter/submodule 防护。
- 本机复制跳过 `.git`、submodule（含嵌套 `.git` 的目录）、symlink 与
  `__pycache__`/`.venv`/`node_modules` 等非源码目录，并记录排除项。
- GitHub 复制使用 `git -c core.hooksPath=<empty> -c core.fsmonitor=false
  -c submodule.recurse=false`，测试用真实 post-checkout hook 与 smudge filter
  证明未被触发。

## 5. 工具

`WorkspaceToolRegistry` 提供：`read_file`、`list_dir`、`search_files`、
`git_status`、`git_diff`、`apply_edits`。状态与差异是"当前任务工作树 vs 注册时
基线"，不是源目录的实时 Git index；`apply_edits` 需要 `expected_tree` 与每个文件
的 pre-image SHA-256。`git_diff` 的当前文件、基线文件、`.mgar` 与
`baseline-files` 路径都逐级检查 symlink；叶子或父目录 symlink 一律拒绝，不读取
项目外内容。

## 6. 重启恢复

manifest（含 tree_hash、baseline_hash、dirty inventory、excluded、created_at）与
baseline 文件保存在任务目录 `.mgar/`。所有元数据 JSON 写入使用同目录随机
`mkstemp`（O_EXCL）临时文件后原子替换，固定 `.tmp` 名称或预置 symlink 不能把写入
重定向到项目外；读取使用 NOFOLLOW 安全路径并拒绝 `.mgar` 目录 symlink。`apply`
成功后更新 tree_hash/manifest_hash 并原子写入 manifest，合法编辑不会因重开被判为
冲突。`open_task_workspace` 重新校验 manifest hash、baseline hash 并重新扫描文件
树；任一不一致返回 `WORKSPACE_MANIFEST_MISMATCH`。这里验证的是逻辑重启/重新打开；
真实进程 SIGKILL 与 Worker fencing 属于 H11。

## 7. 边界与未声称能力

- 本机项目采用副本隔离，不创建 Git 分支；不运行项目代码或 Git hooks/filters。
- 并发串行化只覆盖同一主机内以源目录 inode 为键的 `flock`；不声称跨主机、NFS
  或恶意绕过锁的防护。锁内会复核 inode 与 pre-image。
- GitHub 工作区验收使用本地 `file://` 固定提交仓库，未执行真实 GitHub 网络抓取；
  `live_publication_gate` 仍为 `NOT_RUN`。
- `apply_back` 覆盖文件新增/修改/删除；不提供目录 rename 或 Git commit 语义。
- manifest/baseline 的自 hash 只提供篡改检测，不是签名或只读存储；操作者控制的
  任务目录内仍可被有写权限者替换。检测失败一律拒绝打开或回写。
- H03 不需要模型调用，`live_model_gate` 为 `NOT_RUN`；本卡费用为 0。
- 未修改 Engine、迁移、预算、Codex 活动树或 `0030/0031` 所有权。
