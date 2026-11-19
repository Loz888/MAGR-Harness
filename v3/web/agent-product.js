/* Product client: conversations project Engine-owned tasks; it grants no authority. */
"use strict";

(() => {
  if (window.RepoSageUIReady !== true) return;
  const base = "/api/v3/agent";
  const terminal = new Set(["completed", "blocked", "denied", "failed", "cancelled", "incomplete", "budget_exhausted", "unknown"]);
  const statusLabels = {
    queued: "等待执行", running: "正在执行", completed: "已完成", blocked: "需要补充条件",
    denied: "权限不足", failed: "执行失败", cancelled: "已停止", incomplete: "尚未完成",
    budget_exhausted: "预算已用尽", unknown: "等待核实", paused: "已暂停", pausing: "正在暂停",
    waiting_input: "等待你的回答", waiting_approval: "等待授权", cancelling: "正在停止",
  };
  const toolLabels = {read_file: "阅读文件", list_dir: "查看目录", search_files: "搜索项目", apply_edits: "修改文件", run_tests: "运行测试", run_command: "执行命令", read_artifact: "阅读完整输出", ask_user: "请求补充信息"};
  const data = {
    profile: null, projects: [], sessions: [], session: null, projectId: "", taskId: "", status: "",
    stream: null, cursor: 0, generation: 0, sending: false, polling: false, entering: null,
    results: new Map(), logs: new Map(), question: "", setupAction: null, pendingRequest: null,
    answeredQuestions: new Set(),
    historyStatus: "",
  };
  const errorText = (error) => ({
    AGENT_MODEL_REQUIRED: "请先在模型设置中配置模型并保存使用许可。",
    AGENT_AUTHORIZATION_REQUIRED: "请先设置此项目的权限与总预算。",
    AGENT_AUTHORIZATION_EXPIRED: "此项目授权已过期，请重新设置项目权限。",
    AGENT_BUDGET_EXHAUSTED: "项目授权的总预算已用尽，已保留进展。可在项目权限中另行授权。",
    AGENT_SESSION_BUSY: "当前会话仍在执行，可直接补充要求或等待完成。",
    HARNESS_TASK_FINISHED: "该轮已结束，请在下方输入框继续对话。",
    HARNESS_TEST_COMMAND_REQUIRED: "尚未确定此项目的验证命令，请在对话中补充运行方式。",
  }[error.code] || explain(error));
  const fail = (error) => { $("product-error").textContent = typeof error === "string" ? error : errorText(error); };
  const node = (tag, cls, text) => element(tag, cls, text);
  const newId = () => "web-" + (globalThis.crypto?.randomUUID?.() || Date.now().toString(36) + Math.random().toString(36).slice(2));
  const project = () => data.projects.find((item) => item.project_id === data.projectId);
  const currentTurn = () => (data.session?.turns || []).find((turn) => turn.task_id === data.taskId);
  const active = () => Boolean(data.taskId && !terminal.has(data.status));

  function dialog(title) {
    const host = node("dialog", "product-github-dialog");
    host.setAttribute("aria-label", title);
    const heading = node("h2", "", title);
    const body = node("div", "product-github-body");
    const error = node("p", "error-text"); error.setAttribute("role", "alert");
    const close = node("button", "ghost-button", "关闭"); close.type = "button";
    close.addEventListener("click", () => host.close());
    host.append(heading, body, error, close);
    host.addEventListener("close", () => host.remove());
    document.body.append(host); host.showModal();
    return {host, body, error};
  }
  function inputField(host, label, value = "", multiline = false) {
    const row = node("label", "product-github-field", label);
    const input = node(multiline ? "textarea" : "input"); input.value = value;
    if (multiline) input.rows = 3;
    row.append(input); host.append(row); return input;
  }
  function checkboxField(host, label, checked = false) {
    const row = node("label", "checkbox-label");
    const input = node("input"); input.type = "checkbox"; input.checked = checked;
    row.append(input, document.createTextNode(label)); host.append(row); return input;
  }
  async function importGitHubSnapshot(run) {
    const view = dialog("修复这份 GitHub 代码");
    view.body.append(node("p", "", run.repository + " · " + run.commit),
      node("p", "field-help", "准备固定版本的隔离副本后，在项目助手中调试和运行测试。模型、编辑和 GitHub 发布分别需要授权；创建 PR 与合并会再展示完整审查结果。"));
    const objective = inputField(view.body, "要完成的修复", run.task, true);
    const prepare = node("button", "primary-button", "准备隔离项目"); prepare.type = "button";
    prepare.addEventListener("click", async () => {
      prepare.disabled = true; view.error.textContent = "";
      try {
        const registered = await call("POST", base + "/github/projects", {
          repository_url: "https://github.com/" + run.repository, commit: run.commit,
        });
        if (!registered.project_id) throw new Error("未收到隔离项目登记回执。");
        newChat(); data.projectId = registered.project_id;
        save("mgar-agent-project", registered.project_id);
        await refresh();
        setCodingMode("coding");
        if (data.entering) await data.entering;
        $("product-input").value = objective.value;
        title(); $("product-input").focus(); view.host.close();
        if (registered.source_complete === false) {
          const exclusions = (registered.exclusions || []).map((item) => item.path || item.reason || "排除项").slice(0, 8).join("、");
          fail("此隔离副本有排除项" + (exclusions ? "：" + exclusions : "") + "。当前来源并不完整；可继续调查，PR 审查会要求完整且干净的基线。");
        }
      } catch (error) { view.error.textContent = errorText(error); prepare.disabled = false; }
    });
    view.body.append(prepare);
  }
  function githubLink(url, label) {
    try {
      const target = new URL(url);
      if (target.protocol !== "https:" || target.hostname !== "github.com" || target.username || target.password) return null;
      const link = node("a", "ghost-button", label); link.href = target.href;
      link.target = "_blank"; link.rel = "noopener noreferrer"; return link;
    } catch { return null; }
  }
  async function reviewGitHub(result, taskId) {
    const sessionId = data.session?.session_id, projectId = data.projectId;
    if (!sessionId || active() || taskId !== data.taskId) return;
    const view = dialog("GitHub 变更交付");
    const permissions = node("section", "product-github-permissions");
    const review = node("section", "product-github-review");
    view.body.append(permissions, review);
    const deliveryKey = "mgar-github-delivery:" + sessionId + ":" + taskId;
    let authority, candidate;
    const showError = (error) => { view.error.textContent = errorText(error); };
    async function refreshDelivery(id) {
      const record = await call("GET", base + "/github/deliveries/" + encodeURIComponent(id));
      if (view.host.isConnected) renderDelivery(record);
    }
    async function showMerge(record) {
      const approval = await call("GET", base + "/github/deliveries/" + encodeURIComponent(record.publication_id) + "/merge-review");
      const panel = node("section", "product-github-merge");
      panel.append(node("h3", "", "审查合并"), node("p", "", "PR #" + approval.pull_number),
        node("p", "github-exact-version", "Head: " + (approval.head_sha || "") + "\nBase: " + (approval.base_sha || "")));
      if (approval.checks) panel.append(node("p", "field-help", "检查结果：" + JSON.stringify(approval.checks)));
      if (typeof approval.ci_passed === "boolean") panel.append(node("p", "field-help",
        "CI " + (approval.ci_passed ? "通过" : "未通过") + " · 分支状态 " + (approval.mergeable_state || "待确认")
        + " · 写入权限 " + (approval.permission ? "已确认" : "未确认") + " · PR " + (approval.draft ? "Draft" : approval.state || "待确认")));
      if (approval.review_hash) panel.append(node("p", "github-exact-version", "审查摘要 " + approval.review_hash));
      if (!approval.can_merge) {
        panel.append(node("p", "field-help", "尚不能合并：" + (approval.blockers || []).map((item) => typeof item === "string" ? item : item.code || item.message).join("；")));
      } else {
        const agree = checkboxField(panel, "我已审查这个 PR 的变更与检查结果，确认合并上面显示的准确 Head 和 Base。");
        const merge = node("button", "primary-button", "确认合并此版本"); merge.type = "button"; merge.disabled = true;
        agree.addEventListener("change", () => { merge.disabled = !agree.checked; });
        merge.addEventListener("click", async () => {
          merge.disabled = true; agree.disabled = true; view.error.textContent = "";
          try {
            const merged = await call("POST", base + "/github/deliveries/" + encodeURIComponent(record.publication_id) + "/merge", {
              pull_number: approval.pull_number, head_sha: approval.head_sha, base_sha: approval.base_sha,
              review_hash: approval.review_hash, confirm: true,
            });
            renderDelivery(merged);
          } catch (error) { showError(error); panel.append(node("p", "field-help", "请刷新交付状态核对回执；页面不会自动重发合并请求。")); }
        });
        panel.append(merge);
      }
      review.append(panel);
    }
    function renderDelivery(record) {
      candidate = record;
      const statusLabels = {review: "等待审查", ready: "已批准，等待派发", dispatching: "正在提交", succeeded: "操作已确认完成", outcome_unknown: "结果待核对", failed: "操作失败"};
      const state = node("p", "github-delivery-state", statusLabels[record.status] || record.status || "等待回执");
      state.dataset.status = record.status || "";
      review.replaceChildren(node("h3", "", record.action_type === "merge_pr" ? "合并状态" : "交付状态"), state);
      if (record.reason_code) review.append(node("p", "field-help", record.reason_code));
      const steps = node("ol", "product-github-steps");
      for (const step of record.steps || []) steps.append(node("li", "", (step.kind || "操作") + " · " + (step.status || "等待")));
      if (steps.childElementCount) review.append(steps);
      for (const action of record.merge_actions || []) {
        const row = node("p", "field-help", "合并操作 · " + (statusLabels[action.status] || action.status));
        const inspect = node("button", "ghost-button", "查看合并回执"); inspect.type = "button";
        inspect.addEventListener("click", () => refreshDelivery(action.publication_id).catch(showError));
        row.append(inspect); review.append(row);
      }
      const bundle = record.bundle;
      if (record.action_type === "merge_pr" && bundle) {
        review.append(node("p", "github-exact-version", "PR #" + (bundle.pull_number || "") + "\nHead: " + (bundle.head_sha || "") + "\nBase: " + (bundle.base_sha || "")));
      }
      if (bundle && (bundle.title || bundle.diff || bundle.files?.length)) {
        const details = node("details", "product-details product-diff");
        details.append(node("summary", "", "审查提交内容与完整 Diff"), node("p", "", bundle.title || ""),
          node("pre", "github-proposal-body", bundle.body || ""),
          node("p", "github-exact-version", "仓库 " + (bundle.repository || "") + "\n基础版本 " + (bundle.base_sha || "") + "\n分支 " + (bundle.branch || "")),
          node("pre", "coding-diff", record.diff || bundle.diff || ""));
        review.append(details);
      }
      if (record.tests) review.append(node("p", "product-verification-note", "验证回执：" + JSON.stringify(record.tests)));
      if (record.payload_hash) review.append(node("p", "github-exact-version", "提案摘要 " + record.payload_hash));
      if (record.status === "review" && record.payload_hash) {
        const consent = checkboxField(review, "我已审查完整 Diff、验证回执和提案摘要，确认创建此 Draft PR。");
        const publish = node("button", "primary-button", "批准并创建 Draft PR"); publish.type = "button"; publish.disabled = true;
        consent.addEventListener("change", () => { publish.disabled = !consent.checked || !authority?.valid || !authority?.allow_publish || !record.writer_available; });
        publish.addEventListener("click", async () => {
          publish.disabled = true; consent.disabled = true; view.error.textContent = "";
          try {
            const queued = await call("POST", base + "/github/deliveries/" + encodeURIComponent(record.publication_id) + "/approve", {payload_hash: record.payload_hash});
            renderDelivery({...record, ...queued});
          } catch (error) { showError(error); review.append(node("p", "field-help", "请刷新交付状态核对回执；页面不会自动重复发布。")); }
        });
        review.append(publish);
        if (!authority?.valid || !authority?.allow_publish || !record.writer_available) review.append(node("p", "field-help", "等待发布权限或服务器写入凭据；补丁和验证记录仍可保留。"));
      }
      const refresh = node("button", "ghost-button", "刷新交付状态"); refresh.type = "button";
      refresh.addEventListener("click", () => refreshDelivery(record.publication_id).catch(showError)); review.append(refresh);
      if (record.status === "ready" && record.payload_hash) {
        const dispatch = node("button", "ghost-button", "继续已批准派发"); dispatch.type = "button";
        dispatch.disabled = !authority?.valid || !record.writer_available
          || !(record.action_type === "merge_pr" ? authority?.allow_merge : authority?.allow_publish);
        dispatch.addEventListener("click", async () => {
          dispatch.disabled = true; view.error.textContent = "";
          try { renderDelivery(await call("POST", base + "/github/deliveries/" + encodeURIComponent(record.publication_id) + "/dispatch", {payload_hash: record.payload_hash})); }
          catch (error) { showError(error); review.append(node("p", "field-help", "请刷新状态核对这个既有动作；页面不会重复审批或重发未知步骤。")); }
        });
        review.append(dispatch);
      }
      if (["unknown", "outcome_unknown"].includes(record.status) || record.steps?.some((step) => ["unknown", "outcome_unknown"].includes(step.status))) {
        const reconcile = node("button", "ghost-button", "只读核对未知回执"); reconcile.type = "button";
        reconcile.addEventListener("click", async () => {
          reconcile.disabled = true;
          try { renderDelivery(await call("POST", base + "/github/deliveries/" + encodeURIComponent(record.publication_id) + "/reconcile")); }
          catch (error) { showError(error); reconcile.disabled = false; }
        });
        review.append(reconcile);
        if (record.action_type === "draft_pr" && record.payload_hash) {
          const consent = checkboxField(review, "核对已发出的步骤后，允许沿已批准提案继续尚未发送的步骤。");
          const resume = node("button", "ghost-button", "核对并继续未发送步骤"); resume.type = "button"; resume.disabled = true;
          consent.addEventListener("change", () => { resume.disabled = !consent.checked || !authority?.valid || !authority?.allow_publish || !record.writer_available; });
          resume.addEventListener("click", async () => {
            resume.disabled = true; consent.disabled = true; view.error.textContent = "";
            try { renderDelivery(await call("POST", base + "/github/deliveries/" + encodeURIComponent(record.publication_id) + "/resume", {payload_hash: record.payload_hash})); }
            catch (error) { showError(error); review.append(node("p", "field-help", "未知步骤未经确认时不会重发。可继续只读核对回执。")); }
          });
          review.append(resume);
        }
      }
      if (record.pull_request) {
        const link = githubLink(record.pull_request.url, "查看 PR #" + record.pull_request.number);
        if (link) review.append(link);
        const hasMerge = record.merge_actions?.some((action) => ["ready", "dispatching", "succeeded", "outcome_unknown"].includes(action.status));
        if (!["unknown", "outcome_unknown"].includes(record.status) && !record.pull_request.merged && !hasMerge) {
          const merge = node("button", "ghost-button", "审查是否可合并"); merge.type = "button";
          merge.addEventListener("click", async () => { merge.disabled = true; try { await showMerge(record); } catch (error) { showError(error); merge.disabled = false; } });
          review.append(merge);
        }
      }
    }
    try {
      authority = await call("GET", base + "/github/projects/" + encodeURIComponent(projectId) + "/authorization");
      if (!authority.eligible) { permissions.append(node("p", "", "此项目尚未关联固定版本的 GitHub 仓库。请从仓库分析页准备 GitHub 隔离项目。")); return; }
      permissions.append(node("p", "", authority.repository + " · " + authority.commit),
        node("p", "field-help", authority.writer_available ? "服务器已配置 GitHub 写入凭据。发布与合并仍需下方独立授权和准确提案审批。" : "服务器尚未配置 GitHub 写入凭据。可继续调试和下载补丁；发布将等待权限。"));
      if (!authority.writer_available) {
        const profile = await call("GET", base + "/github/profile");
        if (profile.credential_setting && profile.enable_setting) permissions.append(node("p", "field-help",
          "管理员需配置独立写入凭据 " + profile.credential_setting + "，并启用 " + profile.enable_setting
          + "。模型设置中的 GitHub 只读 Token 仅用于读取；发布和合并还需要此处的项目授权与提案确认。"));
      }
      const branch = inputField(permissions, "目标分支", authority.base_branch || "main");
      const publish = checkboxField(permissions, "允许向此仓库创建 Draft PR（有效期一天）", Boolean(authority.allow_publish));
      const merge = checkboxField(permissions, "允许审查后合并 PR（每次仍需确认准确版本）", Boolean(authority.allow_merge));
      const grant = node("button", "ghost-button", "保存 GitHub 交付权限"); grant.type = "button";
      const receipt = node("p", "field-help");
      grant.addEventListener("click", async () => {
        grant.disabled = true; view.error.textContent = "";
        try {
          authority = await call("POST", base + "/github/projects/" + encodeURIComponent(projectId) + "/authorization", {
            allow_publish: publish.checked, allow_merge: merge.checked, base_branch: branch.value.trim(), days: 1,
            expected_revision: authority.revision || 0,
          });
          receipt.textContent = "交付权限已保存。每次外部操作仍需审查并确认。";
          if (candidate) renderDelivery(candidate);
        } catch (error) { showError(error); }
        finally { grant.disabled = false; }
      });
      permissions.append(grant, receipt);
      const titleInput = inputField(review, "PR 标题", result.summary?.split("\n")[0]?.slice(0, 200) || "修复并验证项目问题");
      const bodyInput = inputField(review, "PR 说明", "说明修改原因、验证方法与已知限制。\n\n" + (result.summary || ""), true);
      const prepare = node("button", "primary-button", "生成完整 PR 审查提案"); prepare.type = "button";
      prepare.addEventListener("click", async () => {
        prepare.disabled = true; view.error.textContent = "";
        try {
          const proposal = await call("POST", base + "/github/sessions/" + encodeURIComponent(sessionId) + "/review", {
            task_id: taskId, expected_tree: result.tree_after, base_branch: branch.value.trim(), title: titleInput.value.trim(), body: bodyInput.value,
          });
          if (!proposal.publication_id) throw new Error("未收到 PR 审查提案标识。");
          save(deliveryKey, proposal.publication_id); renderDelivery(proposal);
        } catch (error) { showError(error); prepare.disabled = false; }
      });
      review.append(prepare);
      const records = await call("GET", base + "/github/sessions/" + encodeURIComponent(sessionId) + "/deliveries");
      const previous = saved(deliveryKey) || (Array.isArray(records) ? records : records.deliveries || [])
        .find((item) => item.task_id === taskId)?.publication_id;
      if (previous) await refreshDelivery(previous);
    } catch (error) { showError(error); }
  }

  function closeStream() {
    data.stream?.close();
    data.stream = null;
  }
  function title() {
    if (CODING.mode !== "coding") return;
    $("page-title").textContent = data.session?.title || project()?.repository || "新的项目对话";
    const model = data.readiness?.model || data.profile?.model;
    $("model-label").textContent = model?.model_id || "PROJECT AGENT";
    $("connection-dot").className = "status-dot " + (model?.available ? "ready" : "error");
    $("mode-badge").textContent = statusLabels[data.status] || "项目助手";
    $("new-chat").disabled = false;
    $("product-project").disabled = !data.projects.length;
    $("product-project-settings").disabled = !data.projectId;
    $("product-send").disabled = data.sending || (!data.projectId && !state.isOwner);
    const hasText = Boolean($("product-input").value.trim());
    const stopping = active() && data.status !== "paused" && !hasText;
    $("product-send").type = active() ? "button" : "submit";
    $("product-send").classList.toggle("is-stop", stopping);
    $("product-send").querySelector('[aria-hidden="true"]').textContent = stopping ? "■" : "↑";
    $("product-send-label").textContent = data.sending ? "正在提交…" : data.status === "paused" ? "继续执行" : active()
      ? hasText ? data.question ? "回复并继续" : "发送补充" : "中止"
      : data.session ? "继续对话" : "发送需求";
    $("product-composer-hint").textContent = active()
      ? "运行中也可以补充要求，助手会在安全点处理。"
      : data.session ? "继续调整，当前工作区和对话会保留。" : "直接描述目标，助手会自行阅读项目并安排步骤。";
    // Historical controls remain addressable for old tasks. The composer is the
    // only visible runtime control; input stays usable while the model works.
    $("product-pause").hidden = true;
    $("product-resume").hidden = true;
    $("product-cancel").hidden = true;
    $("product-question").querySelector(".coding-inline").hidden = true;
    $("product-welcome").hidden = Boolean(data.session);
  }
  function renderHistory() {
    if (CODING.mode !== "coding") return;
    const host = $("history");
    host.replaceChildren();
    for (const session of data.sessions) {
      const status = session.session_id === data.session?.session_id
        ? data.question && !terminal.has(data.status) ? "waiting_input" : data.status || session.status
        : session.status;
      const button = node("button", "history-item" + (session.session_id === data.session?.session_id ? " active" : ""));
      button.type = "button";
      const text = session.title || session.text || session.turns?.[0]?.text || "项目对话";
      button.title = text;
      const title = node("div", "history-title"); title.append(node("strong", "", text));
      if (["queued", "running", "pausing", "cancelling"].includes(status)) {
        const indicator = node("span", "history-running-indicator");
        indicator.setAttribute("role", "status"); indicator.setAttribute("aria-label", "正在运行");
        title.append(indicator);
      }
      button.append(title, node("small", "", (session.repository || session.project_id || "项目") + " · " + (statusLabels[status] || status || "已保存")));
      button.addEventListener("click", () => selectSession(session.session_id).catch(fail));
      host.append(button);
    }
    if (!data.sessions.length) host.append(node("p", "sidebar-empty", "还没有项目对话。描述你的第一个需求，进度会保存在这里。"));
  }
  function renderSetup(readiness) {
    const banner = $("product-setup");
    let heading = "", description = "", action = "";
    const model = readiness?.model || data.profile?.model || {};
    if (!state.isOwner && !data.projects.length) {
      heading = "暂无可用项目";
      description = "请管理员为你的账户分配项目。";
      data.setupAction = null;
    } else if (!state.isOwner && (!model.available || !readiness?.authorization?.valid)) {
      heading = "项目尚未获得执行配置";
      description = "请管理员核对项目凭据与预算。";
      data.setupAction = null;
    } else if (!data.projects.length) {
      heading = "从打开一个项目开始";
      description = "选择 Harness 所在电脑上的项目目录，后续可以随时切换项目。";
      action = "打开项目";
      data.setupAction = openProject;
    } else if (!model.available) {
      const configured = model.configured_model_id || model.model_id;
      const retired = ["deepseek-chat", "deepseek-reasoner"].includes(configured);
      heading = retired ? "更新模型设置" : "连接你的模型";
      description = retired ? "当前配置的 " + configured + " 已停用。请选择 DeepSeek Flash 或 V4 Pro 并保存后开始。" : "配置模型和使用许可后，助手就可以根据需求自主工作。";
      action = "模型设置";
      data.setupAction = showSettings;
    } else if (!readiness?.authorization?.valid) {
      heading = "设置一次项目权限";
      description = "为当前项目授权隔离编辑、命令执行和模型总预算，日常需求直接发送即可。";
      action = "设置权限";
      data.setupAction = openPermission;
    } else {
      const environment = readiness.environment || {};
      const issues = environment.issues || readiness.issues || [];
      const status = environment.status || readiness.status;
      if (issues.length || ["blocked", "not_ready", "missing"].includes(status)) {
        heading = "项目环境需要准备";
        description = issues.map((issue) => typeof issue === "string" ? issue : issue.message || issue.detail || issue.code).filter(Boolean).join("；") || "发送需求后将检查准备条件；尚未满足的环境不会标记为验证通过。";
        action = "查看环境";
        data.setupAction = () => { $("product-input").value = "检查项目环境、依赖与运行方式，说明并处理当前阻塞。"; $("product-input").focus(); };
      }
    }
    banner.hidden = !heading;
    $("product-setup-title").textContent = heading;
    $("product-setup-description").textContent = description;
    $("product-setup-action").textContent = action;
    $("product-setup-action").hidden = !action;
  }
  async function refreshReadiness() {
    if (!data.projectId) { renderSetup(null); return; }
    const readiness = await call("GET", base + "/projects/" + encodeURIComponent(data.projectId) + "/readiness");
    if (readiness.project_id && readiness.project_id !== data.projectId) return;
    data.readiness = readiness;
    renderSetup(readiness);
    title();
  }
  async function prepareModelSettings() {
    if (!state.connected) return;
    const profile = await call("GET", base + "/profile");
    data.profile = profile;
    const model = profile.model || {};
    const options = Array.isArray(model.catalog) && model.catalog.length ? model.catalog : [{model_id: "deepseek-flash", label: "DeepSeek Flash"}, {model_id: "deepseek-v4-pro", label: "DeepSeek V4 Pro"}];
    const list = $("product-model-options"); list.replaceChildren();
    const ids = [];
    for (const item of options) {
      const id = typeof item === "string" ? item : item.model_id || item.id;
      if (!id) continue;
      ids.push(id);
      const option = node("option", "", typeof item === "string" ? id : item.label || item.name || id);
      option.value = id; list.append(option);
    }
    const configured = model.configured_model_id || model.model_id;
    const normalized = ({"deepseek-v4-flash": "deepseek-flash", "deepseek-v4-flash-vision-exp": "deepseek-flash"})[configured] || configured;
    const supported = ids.includes(normalized);
    $("model-id").value = supported ? normalized : model.recommended_model_id || "deepseek-flash";
    const notice = $("product-model-notice");
    notice.hidden = !configured || supported;
    notice.textContent = configured && !supported ? "已保存的模型 " + configured + " 已停用或不在当前支持列表中。请选择在用模型并保存；保存前不会发起模型调用。" : "";
    title();
  }
  async function refresh() {
    if (!state.connected) return null;
    const [profile, sessions] = await Promise.all([call("GET", base + "/profile"), call("GET", base + "/sessions")]);
    data.profile = profile;
    data.projects = profile.projects || [];
    data.sessions = Array.isArray(sessions) ? sessions : sessions.sessions || [];
    const select = $("product-project");
    select.replaceChildren();
    if (!data.projects.length) {
      const option = node("option", "", "尚未打开项目"); option.value = ""; select.append(option); data.projectId = "";
    } else {
      if (!data.projects.some((item) => item.project_id === data.projectId)) data.projectId = data.projects.find((item) => item.project_id === saved("mgar-agent-project"))?.project_id || data.projects[0].project_id;
      for (const item of data.projects) {
        const location = item.execution_site?.kind === "server_sandbox" ? item.execution_site.name : item.path;
        const label = location ? item.repository + " · " + location : item.repository;
        const option = node("option", "", label); option.value = item.project_id; select.append(option);
      }
      select.value = data.projectId;
      save("mgar-agent-project", data.projectId);
    }
    title(); renderHistory();
    await refreshReadiness();
    return profile;
  }
  async function enter() {
    if (!state.connected || CODING.mode !== "coding") return;
    if (data.entering) return data.entering;
    data.entering = (async () => {
      await refresh();
      if (CODING.mode !== "coding" || !state.connected) return;
      const previous = data.session?.session_id || saved("mgar-agent-session");
      if (previous && data.sessions.some((item) => item.session_id === previous)) {
        try { await selectSession(previous); } catch (error) { save("mgar-agent-session", ""); fail(error); }
      }
      title();
    })();
    try { await data.entering; } finally { data.entering = null; }
  }
  async function selectSession(id) {
    const version = ++data.generation;
    closeStream();
    const session = await call("GET", base + "/sessions/" + encodeURIComponent(id));
    if (version !== data.generation) return;
    data.session = session;
    data.projectId = session.project_id;
    $("product-project").value = data.projectId;
    save("mgar-agent-session", session.session_id);
    save("mgar-agent-project", session.project_id);
    const turns = session.turns || [];
    data.taskId = session.task_id || turns.at(-1)?.task_id || "";
    data.status = turns.at(-1)?.status || session.status || "queued";
    data.question = ""; $("product-question").hidden = true;
    for (const turn of turns) if (turn.result) data.results.set(turn.task_id, turn.result);
    renderConversation(); title(); renderHistory();
    setSidebar(false);
    await updateTask(version);
    if (version === data.generation && active()) subscribe();
    await refreshReadiness();
  }
  function newChat() {
    ++data.generation; closeStream();
    data.session = null; data.taskId = ""; data.status = ""; data.question = ""; data.pendingRequest = null;
    data.results.clear(); data.logs.clear();
    save("mgar-agent-session", "");
    $("product-conversation").replaceChildren();
    $("product-question").hidden = true; $("product-error").textContent = "";
    $("product-input").value = "";
    title(); renderHistory(); $("product-input").focus();
  }
  function resultView(result, taskId) {
    const content = node("div", "product-result");
    content.append(markdown(result.summary || result.reason || statusLabels[result.status] || "执行进度已保存"));
    const stats = [];
    if (result.verification_scope === "build_only") {
      content.append(node("p", "product-verification-note", "仅构建/内容检查；未运行行为测试"));
    } else if (result.tests?.status === "not_run") {
      stats.push("未运行行为测试");
    } else if (result.tests) stats.push("测试 " + result.tests.run + " 项 / " + result.tests.failed + " 失败");
    if (Number.isFinite(result.model_calls)) stats.push(result.model_calls + " 次模型调用");
    if (Number.isFinite(result.cost_micros)) stats.push("$" + (result.cost_micros / 1000000).toFixed(4));
    if (stats.length) content.append(node("p", "product-result-meta", stats.join(" · ")));
    if (result.changes?.length) {
      const changes = node("div", "product-changes");
      for (const change of result.changes) changes.append(node("span", "", (change.change || change.status || "修改") + " · " + change.path));
      content.append(changes);
    }
    if (result.commands?.length) {
      const details = node("details", "product-details"); details.append(node("summary", "", "命令与验证记录（" + result.commands.length + "）"));
      const list = node("ul", "product-command-list");
      for (const command of result.commands) list.append(node("li", command.exit_code === 0 ? "ok" : "bad", (command.argv || []).join(" ") + " · exit " + command.exit_code));
      details.append(list); content.append(details);
    }
    if (result.diff) {
      const details = node("details", "product-details product-diff"); details.append(node("summary", "", "审查完整 Diff"), node("pre", "coding-diff", result.diff));
      content.append(details);
    }
    const actions = node("div", "product-result-actions");
    if (result.patch_url?.startsWith("/api/v3/harness/tasks/")) {
      const download = node("a", "ghost-button", "下载补丁"); download.href = result.patch_url; actions.append(download);
    }
    if (state.isOwner && result.changes?.length && result.tree_after && terminal.has(result.status)) {
      const apply = node("button", "ghost-button", "应用变更到项目"); apply.type = "button";
      apply.disabled = active() || taskId !== data.taskId || result.status !== "completed";
      const receipt = node("span", "field-help", "");
      apply.addEventListener("click", async () => {
        apply.disabled = true;
        try {
          const applied = await call("POST", base + "/sessions/" + encodeURIComponent(data.session.session_id) + "/apply", {task_id: taskId, expected_tree: result.tree_after});
          const conflicts = applied.conflicts || applied.receipt?.conflicts || [];
          const applyStatus = applied.status || applied.receipt?.status;
          receipt.textContent = conflicts.length ? "项目文件已变化，未覆盖。冲突：" + conflicts.map((item) => item.path).join("、") : applyStatus === "applied" ? "已应用到项目。" : "尚未应用：" + (applied.message || applyStatus || "请查看回执");
          if (applyStatus !== "applied") apply.disabled = false;
        } catch (error) { receipt.textContent = errorText(error); apply.disabled = false; }
      });
      actions.append(apply, receipt);
      if (result.status === "completed" && taskId === data.taskId) {
        const github = node("button", "ghost-button", "审查 GitHub PR 交付"); github.type = "button"; github.disabled = active();
        github.addEventListener("click", () => reviewGitHub(result, taskId));
        actions.append(github);
      }
    }
    if (state.isOwner && result.status === "completed" && taskId === data.taskId) {
      const start = node("button", "ghost-button", "启动应用预览"); start.type = "button"; start.disabled = active();
      const previewHost = node("div", "product-preview-host");
      const previewStatus = node("span", "field-help", "");
      start.addEventListener("click", async () => {
        start.disabled = true;
        try {
          const launch = await call("POST", base + "/sessions/" + encodeURIComponent(data.session.session_id) + "/preview", {task_id: taskId});
          const url = launch.preview_url;
          if (!url?.startsWith("/api/v3/agent/") || url.includes("..")) throw new Error("预览服务未返回有效的访问地址。");
          const frame = node("iframe", "product-preview"); frame.title = "应用预览"; frame.setAttribute("sandbox", "allow-scripts allow-forms"); frame.src = url;
          const stop = node("button", "product-text-button", "停止预览"); stop.type = "button";
          stop.addEventListener("click", async () => {
            stop.disabled = true;
            try { await call("DELETE", base + "/sessions/" + encodeURIComponent(data.session.session_id) + "/preview"); previewHost.replaceChildren(); previewStatus.textContent = "预览已停止。"; start.disabled = false; }
            catch (error) { previewStatus.textContent = errorText(error); stop.disabled = false; }
          });
          previewHost.replaceChildren(frame, stop); previewStatus.textContent = "应用预览已就绪。";
        } catch (error) { previewStatus.textContent = errorText(error); start.disabled = false; }
      });
      actions.append(start, previewStatus); content.append(previewHost);
    }
    if (actions.childElementCount) content.append(actions);
    const previewUrl = result.preview_url || result.preview?.url;
    if (state.isOwner && previewUrl?.startsWith("/api/v3/agent/") && !previewUrl.includes("..")) {
      const preview = node("details", "product-details"); preview.append(node("summary", "", "应用预览"));
      const frame = node("iframe", "product-preview"); frame.title = "应用预览"; frame.setAttribute("sandbox", "allow-scripts allow-forms"); frame.src = previewUrl;
      preview.append(frame); content.append(preview);
    }
    return content;
  }
  function renderConversation() {
    const host = $("product-conversation");
    const opened = new Set([...host.querySelectorAll("details[open]")].map((item) => item.closest("[data-task-id]")?.dataset.taskId + ":" + item.querySelector("summary")?.textContent));
    const focused = host.contains(document.activeElement);
    const nearBottom = $("product-stage").scrollHeight - $("product-stage").scrollTop - $("product-stage").clientHeight < 120;
    host.replaceChildren();
    const groups = [];
    for (const turn of data.session?.turns || []) {
      const last = groups.at(-1);
      if (turn.task_id && last?.task_id === turn.task_id) {
        last.messages.push(turn.text || turn.objective || "");
        last.status = turn.status;
      } else groups.push({...turn, messages: [turn.text || turn.objective || ""]});
    }
    for (const turn of groups) {
      const article = node("article", "product-turn"); article.dataset.taskId = turn.task_id;
      for (const text of turn.messages) article.append(node("div", "product-user-message", text));
      const header = node("div", "product-assistant-heading");
      const result = data.results.get(turn.task_id);
      const status = turn.task_id === data.taskId ? data.status : result?.status || turn.status;
      header.append(node("span", "assistant-icon", "M"), node("strong", "", "MAGR-Harness"), node("span", "run-status " + (status === "completed" ? "succeeded" : status), statusLabels[status] || status || "已保存"));
      article.append(header);
      const logs = data.logs.get(turn.task_id) || [];
      if (logs.length) {
        const activity = node("details", "product-details product-activity"); activity.open = turn.task_id === data.taskId && active();
        activity.append(node("summary", "", active() && turn.task_id === data.taskId ? logs.at(-1).text : "查看执行过程"));
        const list = node("ol", "product-log");
        for (const log of logs.slice(-100)) list.append(node("li", log.kind || "", log.text));
        activity.append(list); article.append(activity);
      }
      if (result) article.append(resultView(result, turn.task_id));
      else {
        article.append(node("p", "product-progress-text", turn.task_id === data.taskId && active() ? "正在处理你的需求。你可以随时在下方补充要求。" : turn.summary || turn.error_code || "进度已保存。"));
        if (turn.task_id && terminal.has(status)) {
          const load = node("button", "product-text-button", "查看变更与验证记录"); load.type = "button";
          load.addEventListener("click", async () => {
            const version = data.generation; load.disabled = true;
            try {
              const detail = await call("GET", "/api/v3/harness/tasks/" + encodeURIComponent(turn.task_id));
              if (version !== data.generation) return;
              data.results.set(turn.task_id, detail); renderConversation();
            } catch (error) { fail(error); load.disabled = false; }
          });
          article.append(load);
        }
      }
      host.append(article);
    }
    for (const item of host.querySelectorAll("details")) if (opened.has(item.closest("[data-task-id]")?.dataset.taskId + ":" + item.querySelector("summary")?.textContent)) item.open = true;
    if (nearBottom && !focused) $("product-stage").scrollTop = $("product-stage").scrollHeight;
    title();
    const historyStatus = [data.session?.session_id, data.taskId, data.status, Boolean(data.question)].join(":");
    if (data.historyStatus !== historyStatus) { data.historyStatus = historyStatus; renderHistory(); }
  }
  function log(text, kind = "") {
    if (!text || !data.taskId) return;
    const logs = data.logs.get(data.taskId) || [];
    logs.push({text, kind});
    if (logs.length > 200) logs.splice(0, logs.length - 200);
    data.logs.set(data.taskId, logs);
  }
  function onEvent(record) {
    // Buffered or replayed events cannot reopen a known terminal run.
    if (terminal.has(data.results.get(data.taskId)?.status)) return;
    const payload = record.payload || {};
    switch (record.kind) {
      case "run_started": data.status = "running"; log("开始处理需求"); break;
      case "plan_bound": log("已建立执行与验收计划"); break;
      case "model_requested": log("正在分析下一步"); break;
      case "model_completed": if (payload.text) log(payload.text); break;
      case "assistant_message": if (payload.text) log(payload.text); break;
      case "tool_started": log(toolLabels[payload.name] || "使用工具 · " + payload.name); break;
      case "tool_completed": log((toolLabels[payload.name] || payload.name) + (payload.status === "ok" ? "完成" : "：" + (payload.code || "需要调整")), payload.status === "ok" ? "" : "warn"); break;
      case "command_completed": log((payload.argv || []).join(" ") + " · exit " + payload.exit_code, payload.exit_code === 0 ? "ok" : "warn"); break;
      case "test_completed": log("测试 " + (payload.tests_run || 0) + " 项 / " + (payload.tests_failed || 0) + " 失败", payload.tests_failed ? "warn" : "ok"); break;
      case "patch_applied": log("已更新隔离工作区中的文件"); break;
      case "replanned": log("已根据新要求调整计划"); break;
      case "instruction_applied": log("已处理补充要求：" + (payload.text || "")); break;
      case "ask_user":
        if (data.answeredQuestions.has(data.taskId + ":" + payload.question_id)) break;
        data.question = payload.question_id; data.status = "waiting_input"; $("product-question").hidden = false; $("product-question-text").textContent = payload.question; break;
      case "ask_user_answered": data.question = ""; $("product-question").hidden = true; data.status = "running"; break;
      case "paused": data.status = "paused"; log("已暂停，工作进度已保存"); break;
      case "resumed": data.status = "running"; log("继续执行"); break;
      case "cancel_observed": data.status = "cancelled"; log("执行器已确认停止"); break;
      case "run_failed": log(payload.message || payload.code || "执行失败", "warn"); break;
      default: break;
    }
    renderConversation();
  }
  function subscribe() {
    closeStream();
    const id = data.taskId, generation = data.generation;
    if (!id || !active() || CODING.mode !== "coding") return;
    data.cursor = 0;
    data.logs.set(id, []);
    const source = new EventSource("/api/v3/harness/tasks/" + encodeURIComponent(id) + "/events?after=0&follow=true");
    data.stream = source;
    source.addEventListener("update", (event) => {
      if (generation !== data.generation || id !== data.taskId) return;
      let record; try { record = JSON.parse(event.data); } catch { return; }
      if (Number(record.cursor) <= data.cursor) return;
      data.cursor = Number(record.cursor); onEvent(record);
    });
    source.addEventListener("done", () => {
      if (generation !== data.generation) return;
      closeStream(); updateTask(generation).then(() => refresh()).catch(fail);
    });
    source.addEventListener("auth_required", () => { closeStream(); showLogin(); });
    source.onerror = () => { updateTask(generation).catch(fail); };
  }
  async function updateTask(version = data.generation) {
    const id = data.taskId; if (!id) return;
    const result = await call("GET", "/api/v3/harness/tasks/" + encodeURIComponent(id));
    if (version !== data.generation || id !== data.taskId) return;
    data.results.set(id, result); data.status = result.status || data.status;
    const turn = currentTurn(); if (turn) turn.status = data.status;
    if (terminal.has(data.status)) { closeStream(); data.question = ""; $("product-question").hidden = true; }
    renderConversation();
  }
  async function send(event) {
    event.preventDefault(); if (data.sending) return;
    const text = $("product-input").value.trim();
    if (!text) return;
    if (data.question) { await answer(text, $("product-input")); return; }
    fail("");
    if (!data.projectId) { openProject(); return; }
    if (!data.profile?.model?.available) { await showSettings(); return; }
    if (!data.readiness?.authorization?.valid) { openPermission(); return; }
    data.sending = true; title();
    const viewGeneration = data.generation, previousTaskId = data.taskId, wasActive = active();
    // Retain identity across an ambiguous HTTP failure; a second click cannot mint another run.
    const fingerprint = JSON.stringify([data.session?.session_id || "", data.projectId, text]);
    if (data.pendingRequest?.fingerprint !== fingerprint) data.pendingRequest = {fingerprint, client_id: newId()};
    try {
      const id = data.session?.session_id;
      const reply = await call("POST", id ? base + "/sessions/" + encodeURIComponent(id) + "/messages" : base + "/sessions", {text, client_id: data.pendingRequest.client_id, ...(!id ? {project_id: data.projectId} : {})});
      data.pendingRequest = null;
      if (viewGeneration !== data.generation) { await refresh(); return; }
      if ($("product-input").value.trim() === text) $("product-input").value = "";
      if (id && wasActive && reply.task_id === previousTaskId) {
        // A safe-point instruction belongs to the same run. Preserve its SSE
        // cursor and pending question instead of replaying the entire stream.
        data.session = reply.turns ? reply : await call("GET", base + "/sessions/" + encodeURIComponent(id));
        renderConversation(); renderHistory();
        if (terminal.has(reply.status)) await updateTask();
      } else await selectSession(reply.session_id || id);
      await refresh();
    } catch (error) {
      if (error.status >= 400 && error.status < 500) data.pendingRequest = null;
      fail(error);
    }
    finally { data.sending = false; title(); }
  }
  function openProject() {
    $("product-project-error").textContent = "";
    if (!$("product-project-dialog").open) $("product-project-dialog").showModal();
    $("product-project-path").focus();
  }
  const runnerBase = "/api/v3/runner";
  const runnerDialog = $("product-runner-dialog");
  const runnerSelect = $("product-runner-workspace");
  let runnerTimer = null;
  let runnerRefreshing = false;
  let runnerSubmitting = false;
  let runnerPending = null;
  let pairExpires = 0;
  const runnerError = (error) => { $("product-runner-error").textContent = error ? errorText(error) : ""; };
  const runnerButton = (label, action) => {
    const button = node("button", "ghost-button", label);
    button.type = "button";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try { runnerError(); await action(); }
      catch (error) { runnerError(error); }
      finally { button.disabled = false; }
    });
    return button;
  };
  async function showRunnerResult(task) {
    const result = await call("GET", "/api/v3/harness/tasks/" + encodeURIComponent(task.task_id));
    const panel = $("product-runner-result");
    panel.replaceChildren(
      node("h3", "", task.workspace_name),
      node("p", "", result.summary || result.reason || statusLabels[result.status] || ""),
    );
    if (result.output) panel.append(node("pre", "", result.output));
  }
  async function refreshRunners() {
    if (runnerRefreshing || !runnerDialog.open) return;
    runnerRefreshing = true;
    try {
      const [devices, workspaces, jobs] = await Promise.all(
        ["/devices", "/workspaces", "/jobs"].map((path) => call("GET", runnerBase + path)),
      );
      if (!runnerDialog.open) return;
      if (pairExpires && Date.now() >= pairExpires) {
        $("product-runner-code").hidden = true;
        $("product-runner-code").replaceChildren();
        pairExpires = 0;
      }
      $("product-runner-devices").replaceChildren(...devices.map((device) => {
        const status = device.revoked_at ? "已撤销" : device.online ? "在线" : "离线";
        const item = node("li", "", device.name + " · " + status);
        if (!device.revoked_at) item.append(runnerButton("撤销", async () => {
          if (!window.confirm("撤销此设备并停止它的任务？")) return;
          await call("DELETE", runnerBase + "/devices/" + encodeURIComponent(device.device_id));
          await refreshRunners();
        }));
        return item;
      }));
      const selected = runnerSelect.value;
      runnerSelect.replaceChildren(...workspaces.map((workspace) => {
        const label = workspace.name + " · " + workspace.device_name + (workspace.online ? "" : "（离线）");
        const option = node("option", "", label);
        option.value = workspace.workspace_id;
        return option;
      }));
      if (workspaces.some((workspace) => workspace.workspace_id === selected)) runnerSelect.value = selected;
      $("product-runner-submit").disabled = runnerSubmitting || !workspaces.length;
      $("product-runner-jobs").replaceChildren(...jobs.map((job) => {
        const status = job.waiting_for_device && !job.device_online
          ? "等待设备上线" : statusLabels[job.status] || job.status;
        const item = node("li", "", job.workspace_name + " · " + job.execution_site.name + " · " + status);
        item.append(runnerButton("查看结果", () => showRunnerResult(job)));
        if (["queued", "running", "paused", "unknown"].includes(job.status)) {
          item.append(runnerButton("停止", async () => {
            await call("POST", "/api/v3/harness/tasks/" + encodeURIComponent(job.task_id) + "/cancel");
            await refreshRunners();
          }));
        }
        return item;
      }));
    } catch (error) { runnerError(error); }
    finally { runnerRefreshing = false; }
  }
  $("product-runner-open").addEventListener("click", () => {
    $("product-project-dialog").close();
    runnerError();
    runnerDialog.showModal();
    refreshRunners();
    runnerTimer = setInterval(refreshRunners, 2500);
  });
  $("product-runner-close").addEventListener("click", () => runnerDialog.close());
  runnerDialog.addEventListener("close", () => {
    clearInterval(runnerTimer);
    $("product-runner-code").hidden = true;
    $("product-runner-code").replaceChildren();
    $("product-runner-result").replaceChildren();
    pairExpires = 0;
  });
  $("product-runner-pair").addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    try {
      runnerError();
      const issued = await call("POST", runnerBase + "/pairings");
      if (!runnerDialog.open) return;
      pairExpires = Date.parse(issued.expires_at);
      const code = $("product-runner-code");
      code.replaceChildren(node("p", "", "在目标设备运行 mgar runner pair，输入下面的配对码。"),
        node("code", "", issued.code));
      code.hidden = false;
    } catch (error) { runnerError(error); }
    finally { $("product-runner-pair").disabled = false; }
  });
  $("product-runner-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const workspace = runnerSelect.value;
    if (!workspace || runnerSubmitting) return;
    runnerSubmitting = true;
    const submit = $("product-runner-submit");
    submit.disabled = true;
    try {
      runnerError();
      if (runnerPending?.workspace_id !== workspace) {
        runnerPending = {workspace_id: workspace, client_id: crypto.randomUUID()};
      }
      await call("POST", runnerBase + "/jobs", runnerPending);
      runnerPending = null;
      await refreshRunners();
    } catch (error) { runnerError(error); }
    finally { runnerSubmitting = false; submit.disabled = !runnerSelect.value; }
  });
  function openPermission() {
    if (!data.projectId) { openProject(); return; }
    $("product-permission-error").textContent = "";
    $("product-permission-project").textContent = project()?.path || data.projectId;
    $("product-permission-cost").value = (data.readiness?.authorization?.max_cost_micros || 250000) / 1000000;
    $("product-permission-github").checked = Boolean(data.readiness?.authorization?.allow_github_read);
    if (!$("product-permission-dialog").open) $("product-permission-dialog").showModal();
  }
  async function control(action) {
    if (!data.taskId || data.sending) return;
    data.sending = true; title();
    try {
      const result = await call("POST", "/api/v3/harness/tasks/" + encodeURIComponent(data.taskId) + "/" + action);
      data.status = action === "cancel" ? "cancelling" : result.status || (action === "pause" ? "pausing" : "running");
      if (action === "cancel") log("已收到停止请求，正在等待执行器确认。已发生的用量仍会计费。");
      renderConversation();
    } catch (error) { fail(error); }
    finally { data.sending = false; title(); }
  }
  async function answer(text = $("product-answer").value.trim(), input = $("product-answer")) {
    if (!text || !data.question || !data.taskId || data.sending) return;
    const fingerprint = JSON.stringify([data.taskId, data.question, text]);
    if (data.pendingRequest?.fingerprint !== fingerprint) data.pendingRequest = {fingerprint, client_id: newId()};
    data.sending = true; title();
    $("product-answer-send").disabled = true;
    try {
      const taskId = data.taskId, questionId = data.question;
      await call("POST", "/api/v3/harness/tasks/" + encodeURIComponent(taskId) + "/messages", {kind: "answer", text, message_id: data.pendingRequest.client_id, reply_to: questionId});
      data.pendingRequest = null;
      data.answeredQuestions.add(taskId + ":" + questionId);
      if (input.value.trim() === text) input.value = "";
      $("product-question").hidden = true; data.question = ""; data.status = "running";
      log("已收到你的回复，沿当前进度继续执行"); renderConversation();
    } catch (error) { fail(error); }
    finally { data.sending = false; $("product-answer-send").disabled = false; title(); }
  }
  async function poll() {
    if (data.polling || !state.connected) return;
    data.polling = true;
    try { if (active()) await updateTask(); if (!data.stream && active()) subscribe(); await refresh(); }
    catch (error) { fail(error); }
    finally { data.polling = false; }
  }
  function leave() { ++data.generation; closeStream(); }
  function exit() { leave(); data.session = null; data.projects = []; data.sessions = []; data.profile = null; data.taskId = ""; data.status = ""; data.results.clear(); data.logs.clear(); }
  window.ProductAgent = {enter, leave, exit, refresh, poll, newChat, renderHistory, prepareModelSettings, importGitHubSnapshot};

  $("product-composer").addEventListener("submit", send);
  $("product-input").addEventListener("input", title);
  $("product-send").addEventListener("click", (event) => {
    if (!active()) return;
    event.preventDefault();
    if (data.status === "paused") control("resume");
    else if ($("product-input").value.trim()) send(event);
    else control("cancel");
  });
  $("product-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault(); if (!event.repeat && !data.sending) $("product-send").click();
    }
  });
  $("product-project").addEventListener("change", async (event) => { newChat(); data.projectId = event.target.value; save("mgar-agent-project", data.projectId); await refreshReadiness().catch(fail); title(); });
  $("product-add-project").addEventListener("click", openProject);
  $("product-project-settings").addEventListener("click", openPermission);
  $("product-setup-action").addEventListener("click", () => data.setupAction?.());
  $("product-project-close").addEventListener("click", () => $("product-project-dialog").close());
  $("product-permission-close").addEventListener("click", () => $("product-permission-dialog").close());
  $("product-project-form").addEventListener("submit", async (event) => {
    event.preventDefault(); $("product-project-save").disabled = true; $("product-project-error").textContent = "";
    try {
      const registered = await call("POST", "/api/v3/harness/projects", {path: $("product-project-path").value.trim()});
      newChat(); data.projectId = registered.project_id; save("mgar-agent-project", data.projectId);
      await refresh(); $("product-project-dialog").close(); $("product-project-path").value = "";
    } catch (error) { $("product-project-error").textContent = errorText(error); }
    finally { $("product-project-save").disabled = false; }
  });
  $("product-permission-form").addEventListener("submit", async (event) => {
    event.preventDefault(); $("product-permission-save").disabled = true; $("product-permission-error").textContent = "";
    try {
      await call("POST", base + "/projects/" + encodeURIComponent(data.projectId) + "/authorization", {allow_write: true, allow_paid: true, allow_github_read: $("product-permission-github").checked, max_cost_micros: Math.round(Number($("product-permission-cost").value) * 1000000), max_model_calls: 64, expected_revision: data.readiness?.authorization?.revision || 0});
      await refreshReadiness(); $("product-permission-dialog").close(); $("product-input").focus();
    } catch (error) { $("product-permission-error").textContent = errorText(error); }
    finally { $("product-permission-save").disabled = false; }
  });
  $("product-runtime-form").addEventListener("submit", async (event) => {
    event.preventDefault(); $("product-runtime-save").disabled = true; $("product-runtime-error").textContent = "";
    try {
      await call("POST", base + "/projects/" + encodeURIComponent(data.projectId) + "/environment", {image_lock: $("product-runtime-lock").value.trim(), wheelhouse: $("product-runtime-wheelhouse").value.trim()});
      await refreshReadiness(); $("product-runtime-error").textContent = "项目运行环境已保存。";
    } catch (error) { $("product-runtime-error").textContent = errorText(error); }
    finally { $("product-runtime-save").disabled = false; }
  });
  $("product-pause").addEventListener("click", () => control("pause"));
  $("product-resume").addEventListener("click", () => control("resume"));
  $("product-cancel").addEventListener("click", () => control("cancel"));
  $("product-answer-send").addEventListener("click", () => answer());
  for (const button of document.querySelectorAll("[data-agent-prompt]")) button.addEventListener("click", () => { $("product-input").value = button.dataset.agentPrompt; title(); $("product-input").focus(); });
  window.addEventListener("pagehide", closeStream);
  if (state.connected) enter().catch(fail);
  if (CODING.mode === "coding") setCodingMode("coding");
})();
