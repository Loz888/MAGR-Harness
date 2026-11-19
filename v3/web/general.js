(() => {
  const byId = (id) => document.getElementById(id);
  const tasks = "/api/v3/general/tasks";
  const statuses = {
    draft: "草稿", ready: "待规划", running: "执行中", waiting: "等待中",
    blocked: "受阻", verifying: "验证中", completed: "已完成", failed: "失败", cancelled: "已取消",
  };
  const fields = {
    goal: "目标", open_questions: "未解决的问题", domain: "领域", deliverables: "交付物",
    success_criteria: "通过条件", coding_policy: "编码策略",
  };
  let session;
  let current;
  let pending;
  let busy = false;

  async function call(path, body) {
    const response = await fetch(path, body === undefined ? undefined : {
      method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": session.csrf_token},
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) {
      if (response.status === 401) byId("general-auth").hidden = false;
      throw Object.assign(new Error(result.message || "任务请求失败。"), {status: response.status});
    }
    return result;
  }

  async function action(button, execute) {
    if (busy) return;
    busy = true;
    const error = byId("general-error");
    error.hidden = true;
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

  function paint(task, graph) {
    const changed = current?.task_id !== task.task_id || current.revision !== task.revision;
    current = task;
    byId("general-task").hidden = false;
    byId("general-id-input").value = task.task_id;
    const url = new URL(location.href);
    url.searchParams.set("task", task.task_id);
    history.replaceState(null, "", url);
    byId("general-title").textContent = task.spec.goal || "未填写目标";
    byId("general-current-id").textContent = `${task.task_id} · 修订 ${task.revision}`;
    const clarifications = task.clarifications.map((field) => fields[field] || field);
    const status = task.state === "ready" && graph ? "计划已生成" : statuses[task.state];
    byId("general-status").textContent = clarifications.length ? "待补充" : status;
    byId("general-clarifications").textContent = `待补充 ${clarifications.join("、")}`;
    byId("general-clarifications").hidden = !clarifications.length;
    byId("general-editor").hidden = !["draft", "waiting", "ready", "blocked"].includes(task.state);
    if (changed) byId("general-spec").value = JSON.stringify(task.spec, null, 2);
    byId("general-outcome").textContent = task.outcome?.summary || "";
    byId("general-outcome").hidden = !task.outcome;
    byId("general-plan").hidden = task.state !== "ready" || Boolean(graph);
    const steps = byId("general-steps");
    steps.replaceChildren();
    for (const node of graph?.graph.nodes || []) {
      const step = document.createElement("li");
      step.textContent = `${node.goal} · ${graph.progress[node.id].state}`;
      steps.append(step);
    }
    steps.hidden = steps.childElementCount === 0;
  }

  async function load(taskId) {
    if (!/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(taskId)) {
      throw new Error("请输入有效的任务 ID。");
    }
    const path = `${tasks}/${taskId}`;
    let task;
    let graph;
    try {
      task = await call(path);
      if (task.plan_version) {
        try { graph = await call(`${path}/graph`); }
        catch (error) { if (error.status !== 404) throw error; }
      }
    } catch (error) {
      if ([401, 403, 404].includes(error.status) && current?.task_id === taskId) {
        byId("general-task").hidden = true;
        current = null;
      }
      throw error;
    }
    paint(task, graph?.graph.task_revision === task.revision ? graph : null);
  }

  byId("general-task-create").addEventListener("submit", (event) => {
    event.preventDefault();
    void action(byId("general-create"), async () => {
      const goal = byId("general-goal").value.trim();
      const project = byId("general-project").value;
      if (!goal) throw new Error("请输入任务需求。");
      if (pending?.spec.goal !== goal || pending?.project_id !== project) {
        pending = {project_id: project, request_id: crypto.randomUUID(), spec: {goal}};
      }
      const task = await call(tasks, pending);
      pending = null;
      byId("general-goal").value = "";
      paint(task);
    });
  });

  byId("general-task-open").addEventListener("submit", (event) => {
    event.preventDefault();
    void action(event.submitter, () => load(byId("general-id-input").value.trim()));
  });
  byId("general-refresh").addEventListener("click", (event) => {
    void action(event.currentTarget, () => load(current.task_id));
  });
  byId("general-task-revise").addEventListener("submit", (event) => {
    event.preventDefault();
    void action(event.submitter, async () => {
      let spec;
      try { spec = JSON.parse(byId("general-spec").value); }
      catch { throw new Error("任务规范必须是有效的 JSON。"); }
      paint(await call(`${tasks}/${current.task_id}/revisions`, {
        spec, expected_revision: current.revision, expected_version: current.version,
      }));
    });
  });
  byId("general-plan").addEventListener("click", (event) => {
    void action(event.currentTarget, async () => {
      const task = current;
      await call(`${tasks}/${task.task_id}/graph`, {
        expected_revision: task.revision, expected_version: task.version,
      });
      await load(task.task_id);
    });
  });

  void action(null, async () => {
    session = await call("/api/v3/auth/session");
    if (!session.authenticated) {
      byId("general-auth").hidden = false;
      return;
    }
    const projects = await call("/api/v3/harness/projects");
    const selector = byId("general-project");
    for (const project of projects) {
      selector.add(new Option(project.repository || project.project_id, project.project_id));
    }
    if (projects.length === 1) selector.value = projects[0].project_id;
    byId("general-content").hidden = false;
    const initial = new URL(location.href).searchParams.get("task");
    if (initial) await load(initial);
  });
})();
