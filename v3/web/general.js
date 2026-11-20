(() => {
  const byId = (id) => document.getElementById(id);
  const tasks = "/api/v3/general/tasks";
  const statuses = {
    draft: "草稿", ready: "待规划", running: "执行中", waiting: "等待中",
    blocked: "受阻", verifying: "验证中", completed: "已完成", failed: "失败", cancelled: "已取消",
    pending: "未开始", succeeded: "已完成", skipped: "已跳过", unknown: "待核实",
  };
  const profiles = {
    coding: {label: "代码检查", kind: "report", capability: "coding.execute.inspect", output: "代码与运行方式说明", check: "结论可追溯到项目中的具体文件。"},
    research: {label: "资料研究", kind: "report", capability: "research.sources", output: "带来源的研究简报", check: "关键结论有可核实的来源。"},
    data: {label: "数据分析", kind: "table", capability: "data.analyze", output: "指标汇总表", check: "能根据输入数据复算主要指标。"},
    office: {label: "办公草稿", kind: "draft", capability: "office.draft", output: "供审阅的草稿", check: "草稿覆盖已提供的要求，等待审阅。"},
  };
  const editable = new Set(["draft", "ready", "waiting", "blocked"]);
  const active = new Set(["running", "verifying"]);
  const projectNames = new Map();
  const drafts = new Map();
  let session;
  let current;
  let graph;
  let recent = [];
  let renderedRecent;
  let pendingCreate;
  let busy = false;

  function element(tag, className, label) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (label !== undefined) node.textContent = label;
    return node;
  }

  async function call(path, body) {
    const response = await fetch(path, body === undefined ? undefined : {
      method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": session.csrf_token},
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (response.ok) return result;
    if (response.status === 401) {
      session = null;
      projectNames.clear();
      byId("general-project").replaceChildren(new Option("选择项目", ""));
      showTask(null);
      recent = [];
      drafts.clear();
      byId("general-goal").value = "";
      renderRecent();
      byId("general-auth").hidden = false;
      byId("general-examples").hidden = true;
      byId("general-content").hidden = true;
      byId("general-account").textContent = "";
    }
    throw Object.assign(new Error(result.message || "任务请求失败。"), {status: response.status});
  }

  async function action(button, execute, quiet = false) {
    if (busy) return;
    busy = true;
    const error = byId("general-error");
    if (!quiet) error.hidden = true;
    if (button) button.disabled = true;
    try {
      await execute();
    } catch (cause) {
      error.textContent = cause.message;
      error.hidden = false;
    } finally {
      busy = false;
      if (button) button.disabled = false;
    }
  }

  function renderRecent() {
    const key = JSON.stringify([current?.task_id, recent]);
    if (key === renderedRecent) return;
    renderedRecent = key;
    const list = byId("general-recent");
    list.replaceChildren();
    if (!recent.length) list.append(element("p", "rail-empty", "暂无任务"));
    for (const item of recent) {
      const button = element("button", "history-item");
      button.type = "button";
      button.setAttribute("aria-current", String(item.task_id === current?.task_id));
      button.append(element("span", "", item.goal || "未填写目标"));
      button.setAttribute("aria-label", `${item.goal || "未填写目标"}，${statuses[item.state] || item.state}`);
      button.addEventListener("click", () => void action(button, () => load(item.task_id)));
      if (active.has(item.state)) button.append(element("span", "pulse"));
      list.append(button);
    }
  }

  async function refreshRecent() {
    recent = await call(tasks);
    renderRecent();
  }

  function suggestedDomain(goal) {
    const matches = [
      ["coding", /代码|架构|仓库|项目结构|编译|README|\bcode\b/i],
      ["research", /资料|调研|研究|文献|资料来源/i],
      ["data", /数据|表格|指标|\bCSV\b|\bExcel\b/i],
      ["office", /邮件|草稿|日程|会议邀请/i],
    ].filter(([, pattern]) => pattern.test(goal || ""));
    return matches.length === 1 ? matches[0][0] : "";
  }

  function selectedDomain() {
    return current?.spec.domain || byId("general-domain").querySelector("input:checked")?.value;
  }

  function updateDraftFields() {
    const profile = profiles[selectedDomain()];
    byId("general-delivery").placeholder = profile?.output || "填写希望收到的交付物";
    byId("general-criteria").placeholder = profile?.check || "描述怎样算完成";
  }

  function needsClarification(task) {
    const {spec, clarifications} = task;
    return task.state === "draft" || clarifications.length > 0 ||
      !spec.allowed_capabilities.some((item) => item.startsWith(spec.domain + "."));
  }

  function renderClarification(changed) {
    const form = byId("general-clarify");
    form.hidden = !editable.has(current.state) || !needsClarification(current);
    if (form.hidden) return;
    const missing = new Set(current.clarifications);
    const questions = current.spec.open_questions;
    byId("general-missing-goal").hidden = !missing.has("goal");
    byId("general-domain").hidden = !missing.has("domain");
    byId("general-questions-wrap").hidden = !questions.length;
    byId("general-delivery-wrap").hidden = !missing.has("deliverables");
    byId("general-criteria-wrap").hidden = !missing.has("success_criteria");
    byId("general-questions").textContent = questions.join("\n");
    if (changed) {
      form.reset();
      const suggestion = suggestedDomain(current.spec.goal);
      for (const input of byId("general-domain").querySelectorAll("input")) {
        input.checked = input.value === suggestion;
      }
    }
    updateDraftFields();
  }

  function renderPlan() {
    const planned = Boolean(graph) || Boolean(current.plan?.steps.length);
    byId("general-plan").hidden = current.state !== "ready" || planned || needsClarification(current);
    byId("general-plan-panel").hidden = !planned;
    const steps = byId("general-steps");
    steps.replaceChildren();
    if (graph) {
      for (const node of graph.graph.nodes) {
        const step = element("li", "", node.goal);
        const progress = graph.progress[node.id];
        if (progress) step.append(element("span", "step-state", statuses[progress.state] || progress.state));
        steps.append(step);
      }
    } else {
      for (const step of current.plan?.steps || []) steps.append(element("li", "", step));
    }
  }

  function updateComposer() {
    const box = byId("general-goal");
    const locked = current && (!editable.has(current.state) || current.spec.context.length >= 12);
    box.disabled = Boolean(locked);
    byId("general-create").disabled = box.disabled;
    box.maxLength = current ? 2048 : 16000;
    box.placeholder = locked ? "可新建任务继续" : current ? "补充需求或上下文…" : "描述你的需求…";
    byId("general-composer-scope").textContent = current
      ? (locked ? (editable.has(current.state) ? "当前任务已达到补充上限" : "当前任务状态不支持修订") : "补充会创建新的任务修订")
      : (projectNames.get(byId("general-project").value) || "发送前选择项目");
  }

  function showTask(task, candidateGraph = null) {
    const previous = current?.task_id || "";
    const next = task?.task_id || "";
    const changed = previous !== next || current?.revision !== task?.revision;
    if (previous !== next) {
      const input = byId("general-goal");
      drafts.set(previous, input.value);
      input.value = drafts.get(next) || "";
    }
    current = task;
    graph = candidateGraph;
    byId("general-welcome").hidden = Boolean(task);
    byId("general-task").hidden = !task;
    byId("general-refresh").hidden = !task;
    if (task && projectNames.has(task.project_id)) byId("general-project").value = task.project_id;
    byId("general-project").disabled = Boolean(task) || !projectNames.size;
    const url = new URL(location.href);
    if (task) url.searchParams.set("task", task.task_id);
    else url.searchParams.delete("task");
    history.replaceState(null, "", url);
    renderRecent();
    updateComposer();
    if (!task) return;
    byId("general-id-input").value = task.task_id;
    byId("general-title").textContent = task.spec.goal || "未填写目标";
    byId("general-status").textContent = task.state === "ready" && task.plan_version ? "计划已生成" :
      task.reason === "clarification_required" ? "待补充" : (statuses[task.state] || task.state);
    byId("general-task-info").textContent = [
      projectNames.get(task.project_id) || task.project_id,
      profiles[task.spec.domain]?.label,
      `修订 ${task.revision}`,
    ].filter(Boolean).join(" · ");
    const reason = byId("general-reason");
    reason.hidden = !task.reason || task.reason === "clarification_required";
    reason.textContent = task.reason === "deadline_expired" ? "任务已超过截止时间。" : task.reason || "";
    byId("general-context").replaceChildren(...task.spec.context.map((entry) => {
      const article = element("div", "entry entry-user");
      article.append(element("div", "entry-label", "补充"), element("p", "", entry));
      return article;
    }));
    renderClarification(changed);
    renderPlan();
    byId("general-result").hidden = !task.outcome;
    byId("general-outcome").textContent = task.outcome?.summary || "";
    byId("general-deliverables").replaceChildren(...(task.outcome?.deliverables || []).map((file) =>
      element("li", "", `${file.deliverable_id} · ${file.sha256}`)));
  }

  async function load(taskId) {
    if (!/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(taskId)) {
      throw new Error("请输入有效的任务 ID。");
    }
    const path = `${tasks}/${taskId}`;
    let task;
    try {
      task = await call(path);
    } catch (error) {
      if ([403, 404].includes(error.status) && current?.task_id === taskId) {
        recent = recent.filter((item) => item.task_id !== taskId);
        showTask(null);
      }
      throw error;
    }
    let planned = null;
    if (task.plan_version) {
      try { planned = await call(`${path}/graph`); }
      catch (error) { if (error.status !== 404) throw error; }
    }
    showTask(task, planned?.graph.task_revision === task.revision ? planned : null);
  }

  function fence(task) {
    return {expected_revision: task.revision, expected_version: task.version};
  }

  async function revise(task, spec, draft = false) {
    return call(`${tasks}/${task.task_id}/revisions`, {...fence(task), spec, draft});
  }

  async function plan(task) {
    await call(`${tasks}/${task.task_id}/graph`, fence(task));
    await load(task.task_id);
  }

  function clarifiedSpec(task) {
    const domain = selectedDomain();
    const profile = profiles[domain];
    if (!profile) throw new Error("请选择最接近的任务方向。");
    const spec = {...task.spec, domain};
    spec.goal ||= byId("general-goal-input").value.trim();
    if (!spec.goal) throw new Error("请输入任务需求。");
    if (spec.open_questions.length) {
      const answer = byId("general-answer").value.trim();
      if (!answer || spec.context.length >= 12) throw new Error("请补充问题，或开启一个新任务。");
      spec.context = [...spec.context, answer];
      spec.open_questions = [];
    }
    if (!spec.deliverables.length) {
      spec.deliverables = [{id: "result", kind: profile.kind,
        description: byId("general-delivery").value.trim() || profile.output}];
    }
    const checked = new Set(spec.success_criteria.flatMap((criterion) => criterion.deliverable_ids));
    const missing = spec.deliverables.filter((item) => !checked.has(item.id)).map((item) => item.id);
    if (missing.length) {
      const used = new Set(spec.success_criteria.map((criterion) => criterion.id));
      let id = "workspace-check";
      while (used.has(id)) id += "-next";
      spec.success_criteria = [...spec.success_criteria, {
        id, description: byId("general-criteria").value.trim() || profile.check, deliverable_ids: missing,
      }];
    }
    if (domain === "coding") spec.coding_policy ||= "inspect";
    if (!spec.allowed_capabilities.some((item) => item.startsWith(domain + "."))) {
      spec.allowed_capabilities = [...spec.allowed_capabilities, profile.capability];
    }
    return spec;
  }

  byId("general-task-create").addEventListener("submit", (event) => {
    event.preventDefault();
    void action(event.submitter, async () => {
      const box = byId("general-goal");
      const text = box.value.trim();
      if (!text) return;
      if (current) {
        if (!editable.has(current.state) || current.spec.context.length >= 12) return;
        const spec = {...current.spec, context: [...current.spec.context, text]};
        showTask(await revise(current, spec, current.state === "draft"));
      } else {
        const project = byId("general-project").value;
        if (!project) throw new Error("请先选择任务所属项目。");
        if (pendingCreate?.spec.goal !== text || pendingCreate?.project_id !== project) {
          pendingCreate = {project_id: project, request_id: crypto.randomUUID(),
            spec: {goal: text, approval_policy: "deny_external_writes"}};
        }
        const created = await call(tasks, pendingCreate);
        pendingCreate = undefined;
        box.value = "";
        showTask(created);
        await refreshRecent();
        return;
      }
      box.value = "";
      await refreshRecent();
    });
  });

  byId("general-goal").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && !event.repeat) {
      event.preventDefault();
      byId("general-task-create").requestSubmit(byId("general-create"));
    }
  });
  byId("general-task-open").addEventListener("submit", (event) => {
    event.preventDefault();
    void action(event.submitter, () => load(byId("general-id-input").value.trim()));
  });
  byId("general-domain").addEventListener("change", updateDraftFields);
  byId("general-project").addEventListener("change", updateComposer);
  byId("general-new").addEventListener("click", (event) => void action(event.currentTarget, async () => {
    showTask(null);
    byId("general-goal").focus();
  }));
  byId("general-reload").addEventListener("click", (event) =>
    void action(event.currentTarget, refreshRecent));
  byId("general-refresh").addEventListener("click", (event) =>
    void action(event.currentTarget, () => load(current.task_id)));
  byId("general-plan").addEventListener("click", (event) =>
    void action(event.currentTarget, async () => { await plan(current); await refreshRecent(); }));
  byId("general-clarify").addEventListener("submit", (event) => {
    event.preventDefault();
    void action(event.submitter, async () => {
      const revised = await revise(current, clarifiedSpec(current));
      showTask(revised);
      await refreshRecent();
      if (revised.state === "ready") await plan(revised);
    });
  });
  for (const sample of byId("general-examples").querySelectorAll("[data-prompt]")) {
    sample.addEventListener("click", () => {
      byId("general-goal").value = sample.dataset.prompt;
      byId("general-goal").focus();
    });
  }
  setInterval(() => {
    if (document.hidden || !session?.authenticated) return;
    void action(null, async () => {
      await refreshRecent();
      if (current && (editable.has(current.state) || active.has(current.state))) {
        await load(current.task_id);
      }
    }, true);
  }, 8000);

  void action(null, async () => {
    const selected = new URL(location.href).searchParams.get("task");
    session = await call("/api/v3/auth/session");
    if (!session.authenticated) {
      byId("general-auth").hidden = false;
      return;
    }
    byId("general-account").textContent = session.username || "";
    const projects = await call("/api/v3/harness/projects");
    const selector = byId("general-project");
    for (const project of projects) {
      const name = project.repository || project.project_id;
      projectNames.set(project.project_id, name);
      selector.add(new Option(name, project.project_id));
    }
    if (projects.length === 1) selector.value = projects[0].project_id;
    selector.disabled = !projects.length;
    byId("general-no-project").hidden = Boolean(projects.length);
    byId("general-examples").hidden = !projects.length;
    byId("general-content").hidden = !projects.length;
    updateComposer();
    await refreshRecent();
    if (selected) await load(selected);
  });
})();
