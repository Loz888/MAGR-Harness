/* The UI is a client of the durable repository agent, never a simulated answer generator. */
"use strict";

const $ = (id) => document.getElementById(id);
const API = "/api/v3/repository-analyses";
const ACTIVE = new Set(["queued", "running"]);
const LABELS = {
  queued: "等待执行",
  running: "正在分析",
  succeeded: "分析完成",
  failed: "分析失败",
  paused: "已暂停",
  cancelled: "已中止",
  waiting_input: "需要补充信息",
};
const TOOLS = {
  repository_metadata: "查看仓库",
  list_directory: "浏览目录",
  read_file: "阅读源码",
  search_code: "搜索代码",
  read_issue: "阅读 Issue",
  read_pull_request: "阅读 PR",
};
const ERRORS = {
  AUTH_REQUIRED: "请先登录后继续。",
  LOGIN_FAILED: "用户名或密码不正确。",
  CSRF_REQUIRED: "登录状态已更新，请刷新页面后重试。",
  SETTINGS_STALE: "配置已被更新，请重新打开设置再保存。",
  ANALYSIS_BUSY: "有分析正在执行，请先中止或等待完成后再保存设置。",
  REPO_ANALYSIS_AUTHORIZATION_CHANGED:
    "使用许可已更新，请新建分析以使用新配置。",
  ORIGIN_DENIED:
    "当前访问地址未获服务器允许，请使用 http://127.0.0.1:18080 访问。",
  MODEL_CREDENTIAL_REQUIRED:
    "服务器尚未配置 DeepSeek 密钥。请在设置中查看配置步骤。",
  REPO_ANALYSIS_PROFILE_REQUIRED:
    "服务器尚未配置真实分析授权，请在设置中查看配置步骤。",
  REPO_ANALYSIS_PROFILE_INVALID:
    "服务器的分析授权无效，请检查授权人、预算和配置文件。",
  REPO_ANALYSIS_SCOPE_DENIED:
    "当前许可未允许分析此仓库，请在模型与使用设置中检查仓库范围和有效期。",
  GITHUB_READ_CREDENTIAL_REQUIRED: "服务器尚未配置 GitHub 只读凭据。",
  GITHUB_NOT_FOUND: "没有找到仓库或指定文件，请检查 URL、分支和读取权限。",
  REPOSITORY_NOT_PUBLIC: "当前仅支持公开的 GitHub 仓库。",
  GITHUB_FORBIDDEN: "GitHub 拒绝了读取请求，请检查服务器上的只读凭据权限。",
  GITHUB_RATE_LIMITED: "GitHub 请求额度暂时用尽，请稍后再试。",
  REF_NOT_FOUND: "没有找到指定分支、Tag 或 Commit，请核对仓库版本。",
  TOKEN_BUDGET_EXHAUSTED: "本轮累计 Token 上限不足以容纳下一次输入与输出预留。它与美元费用上限分别约束，当前证据和未完成项已保留。",
  AGENT_STALLED:
    "本轮调查进展已保留。可以补充目标或线索，沿已有证据继续调查。",
  INPUT_REQUIRED: "请在下方回复，已有证据与调查进度会保留。",
  EVIDENCE_REJECTED: "当前回答尚未通过核验，以下保留已确认的证据和待解决问题。",
  INSUFFICIENT_EVIDENCE:
    "现有源码证据不足以回答问题，可以补充更具体的目标后继续。",
  MODEL_RATE_LIMITED: "模型服务暂时限流，本次进展已保留。",
  BUDGET_EXHAUSTED: "下一次调用超出本轮费用或调用次数额度，当前结果已保留。",
  INPUT_TOKEN_BUDGET: "本轮累计输入 Token 上限不足以容纳下一次请求。它与美元费用上限分别约束，当前结果已保留。",
  CONTEXT_LIMIT: "必要信息与最新工具结果仍超过本轮上下文窗口。可在模型设置中调整窗口后继续。",
  CONTEXT_POLICY_INVALID: "上下文窗口或压缩阈值无效，请检查模型能力和输出预留。",
  ANALYSIS_CALL_PENDING: "中止请求已接收，当前调用的用量或停止状态仍在核实。请等待回执后再继续追问。",
  ANALYSIS_USAGE_UNRESOLVED: "旧调用用量尚未核实，费用预占已保留。可以继续追问，新问题会沿用已有记忆并使用会话剩余额度，不会重发旧请求。",
  MODEL_RESPONSE_LIMIT: "模型响应已返回，但回执保存超限，本轮分析未完成。以下草稿尚未通过核验；已保留可确认的证据和费用预占。",
  MODEL_RESPONSE_RECORDING_FAILED: "模型用量已核实，但本地回执保存失败。未核验草稿已保留；可查看日志并继续追问。",
  DEADLINE_EXCEEDED: "本次分析已达到授权的时间上限，以下保留已获得的结果。",
  USAGE_UNKNOWN: "本轮未收到完整用量回执，已保留证据和草稿。可以继续追问；未决费用预占仍计入会话总额度。",
  MODEL_PROVIDER_ERROR:
    "模型服务返回错误，请检查密钥、模型名称和供应商账户额度。",
  MODEL_INVALID_RESPONSE:
    "模型输出没有通过结构校验，源码证据已保留，可以继续追问。",
  MODEL_OUTPUT_TRUNCATED:
    "模型回复达到单次输出上限，源码证据已保留，可以继续追问。",
  EXECUTION_FAILED: "执行失败。请查看服务器日志，并保留本次运行记录以便排查。",
  REPORT_VERIFICATION_FAILED:
    "生成的报告没有通过源码引用核验，不能作为成功结论。",
  INVALID_INPUT: "输入格式有误，请检查 GitHub 仓库地址、问题和分支。",
  REQUEST_ID_REUSED: "该请求标识已被其他内容使用，请新建一次请求。",
  HARNESS_APPROVAL_REQUIRED: "编码任务需要先勾选确认，再提交。",
  HARNESS_TEST_COMMAND_REQUIRED:
    "这个项目无法自动探测测试命令，请在“受控适配器参数”中填写。",
  HARNESS_DETERMINISTIC_HINT_REQUIRED:
    "本地确定性适配器需要目标文件与替换前文本；请展开“受控适配器参数”填写。",
  HARNESS_DETERMINISTIC_MISS:
    "目标文件中没有找到替换前文本，请核对文件与文本后再提交。",
  HARNESS_LIVE_MODEL_UNAVAILABLE:
    "服务器未授权真实付费模型调用：需要有效的 DeepSeek 凭据与付费预算。",
  HARNESS_PROJECT_UNKNOWN: "项目尚未注册，请重新选择或注册项目。",
  HARNESS_TASK_UNKNOWN: "没有找到这次编码任务的结果。",
  HARNESS_COMMAND_DENIED: "测试命令未获租约授权，已拒绝执行。",
  HARNESS_PATCH_UNAVAILABLE: "这次任务没有产生可下载的补丁。",
};

const CODING = {
  projects: [],
  busy: false,
  mode: "coding",
  result: null,
  taskId: "",
  stream: null,
  cursor: 0,
  questionId: "",
  messages: [],
};

function saved(key) {
  try {
    return sessionStorage.getItem(key) || "";
  } catch {
    return "";
  }
}
function save(key, value) {
  try {
    if (value) sessionStorage.setItem(key, value);
    else sessionStorage.removeItem(key);
  } catch {
    /* Memory-only use remains possible. */
  }
}
const state = {
  csrf: "",
  username: "",
  isOwner: true,
  setupRequired: false,
  revision: 0,
  pendingSubmit: false,
  profile: null,
  runs: [],
  selected: null,
  details: new Map(),
  demo: saved("mgar-demo") === "1",
  busy: false,
  stopping: false,
  polling: false,
  selection: 0,
  signature: "",
  request: null,
  timer: null,
  connected: false,
  stream: null,
  streamVersion: 0,
  cursor: 0,
  drafts: new Map(),
  progress: new Map(),
  progressTimer: null,
  continuationWaits: new Map(),
};
function isDemo() {
  const run = state.details.get(state.selected)?.run;
  return run ? run.execution_mode === "fixture" : state.demo;
}
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function notify(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = !message;
}
function explain(error) {
  return ERRORS[error.code] || error.message || "请求未完成，请检查服务连接。";
}
async function call(method, path, body, refreshCsrf = true) {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: {
      "X-CSRF-Token": state.csrf,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    data = text;
  }
  if (!response.ok) {
    if (data?.code === "CSRF_REQUIRED" && refreshCsrf) {
      // Authentication rejected the write before business handling. Refresh only
      // this session check, once; never retry an ambiguous or unrelated failure.
      const session = await call(
        "GET",
        "/api/v3/auth/session",
        undefined,
        false,
      );
      if (session.authenticated) {
        state.csrf = session.csrf_token;
        return call(method, path, body, false);
      }
      showLogin();
    }
    const error = new Error(
      typeof data === "object"
        ? data.message
        : "服务器返回 HTTP " + response.status,
    );
    error.code = data && data.code;
    error.status = response.status;
    if (response.status === 401 && !path.startsWith("/api/v3/auth/")) {
      showLogin();
    }
    throw error;
  }
  return data;
}
function showLogin() {
  closeStream();
  window.ProductAgent?.exit();
  state.connected = false;
  state.csrf = "";
  $("app-shell").hidden = true;
  $("auth-screen").hidden = false;
  if ($("settings-dialog").open) $("settings-dialog").close();
}
function configureLogin(setup) {
  state.setupRequired = setup;
  $("auth-eyebrow").textContent = setup ? "第一次见面，你好" : "欢迎回来";
  $("auth-title").textContent = setup ? "创建你的管理员账户" : "登录 MAGR-Harness";
  $("auth-description").textContent = setup
    ? "为这台电脑上的 MAGR-Harness 设置用户名与密码。"
    : "使用你的账户，继续项目对话。";
  $("login-submit").textContent = setup ? "创建账户并开始" : "登录";
  $("password").autocomplete = setup ? "new-password" : "current-password";
  $("password").minLength = setup ? 10 : 1;
  $("password").placeholder = setup ? "设置至少 10 位密码" : "输入密码";
  $("confirm-password-row").hidden = !setup;
  $("confirm-password").required = setup;
  $("auth-footnote").textContent = setup
    ? "管理员可配置模型并为项目添加成员。"
    : "登录后可直接提交问题，配置会保留在本机服务中。";
}
async function showSettings() {
  if (!state.connected) return;
  if (!state.isOwner) {
    $("logout").click();
    return;
  }
  if (!$("settings-dialog").open) $("settings-dialog").showModal();
  $("settings-error").textContent = "";
  try {
    await loadSettings();
  } catch (error) {
    $("settings-error").textContent = explain(error);
  }
}
function renderProfile() {
  const profile = state.profile;
  $("readiness").hidden = !profile;
  $("checklist").replaceChildren();
  $("setup-help").replaceChildren();
  if (!profile) return;
  const checks = profile.checks || {};
  const rows = CODING.mode === "coding" ? [
    [checks.model_configured, "模型密钥"],
    [checks.paid_enabled && checks.authorization_valid, "模型使用许可"],
  ] : [
    [checks.model_configured, "模型密钥"],
    [checks.github_configured, "仓库读取凭据"],
    [checks.paid_enabled && checks.authorization_valid, "分析使用许可"],
  ];
  for (const [ok, label] of rows) {
    const item = element("li");
    item.append(
      element("span", ok ? "pass" : "missing", ok ? "✓" : "○"),
      element("span", "", label + (ok ? "已就绪" : "待填写")),
    );
    $("checklist").append(item);
  }
  $("profile-summary").textContent = profile.model
    ? "当前模型：" +
      profile.model.model_id +
      " · 每次最多 $" +
      (profile.limits.max_cost_micros / 1000000).toFixed(2)
    : "填写上方设置并保存，即可开始真实分析。";
  $("model-label").textContent = profile.model
    ? profile.model.model_id
    : "REPOSITORY AGENT";
  $("connection-label").textContent = state.username + " · 模型设置";
  $("account-name").textContent = "已登录：" + state.username;
  $("connection-dot").className =
    "status-dot " + ((CODING.mode === "coding" ? checks.model_configured && checks.paid_enabled && checks.authorization_valid : profile.ready) ? "ready" : "error");
}
function updateTokenBudgetMode() {
  const manual = $("token-budget-mode").value === "manual";
  $("token-limit").disabled = !manual;
  $("token-budget-settings").hidden = !manual;
  $("token-budget-settings").open = manual;
  $("token-budget-help").textContent = manual
    ? "累计 Token 是独立上限，可能先于美元费用上限触发；下一次请求还需留出输入与输出空间。旧设置保持原值，修改并保存只对后续分析生效。"
    : "按已授权的美元费用、时限和进展运行；累计 Token 仅保留 10,000,000 的安全硬上限。选择并保存后对后续分析生效。";
}
async function loadSettings() {
  const data = await call("GET", "/api/v3/settings/analysis");
  state.revision = data.revision;
  state.profile = data.profile;
  renderProfile();
  const profile = state.profile,
    checks = profile.checks || {},
    limits = profile.limits || {};
  $("model-id").value = profile.model?.model_id || "deepseek-chat";
  $("model-key").value = "";
  $("github-key").value = "";
  $("model-key").placeholder = checks.model_configured
    ? "已配置，留空保留"
    : "粘贴 DeepSeek API Key";
  $("github-key").placeholder = checks.github_configured
    ? "已配置，留空保留"
    : "粘贴 GitHub 只读 Token";
  $("model-key-help").textContent = checks.model_configured
    ? "密钥已加密保存，填写新值可替换。"
    : "填写后加密保存在运行此服务的电脑上。";
  $("github-key-help").textContent = checks.github_configured
    ? "读取凭据已配置，留空可保留。"
    : "本地项目可留空。GitHub 分析需要仓库只读权限。";
  const repos = profile.allowed_repositories || ["*"];
  $("all-repositories").checked = repos.includes("*");
  $("repository-scope").value = repos.filter((x) => x !== "*").join("\n");
  $("repository-scope-row").hidden = $("all-repositories").checked;
  $("cost-limit").value = (limits.max_cost_micros || 500000) / 1000000;
  $("token-limit").value =
    limits.max_total_tokens || limits.max_input_tokens || 500000;
  $("token-budget-mode").value = profile.token_budget_mode || (data.revision ? "manual" : "cost");
  updateTokenBudgetMode();
  const contextPolicy = profile.model?.context_policy || {};
  $("context-mode").value = contextPolicy.mode || "auto";
  $("context-window").value = contextPolicy.window_tokens || profile.model?.context_tokens || 131072;
  $("context-window").disabled = $("context-mode").value === "auto";
  $("context-prepare").value = Math.round((contextPolicy.prepare_ratio ?? 0.65) * 100);
  $("context-force").value = Math.round((contextPolicy.force_ratio ?? 0.85) * 100);
  $("context-target").value = Math.round((contextPolicy.target_ratio ?? 0.50) * 100);
  $("context-capability").textContent = "当前模型API窗口：" + (profile.model?.context_tokens || 131072).toLocaleString() + " Token。输出与协议空间单独预留；累计Token额度不会自动增加。";
  $("allow-paid").checked = Boolean(
    checks.paid_enabled && checks.authorization_valid,
  );
  $("save-model-settings").textContent = state.pendingSubmit
    ? "保存并开始分析"
    : "保存设置";
  if (CODING.mode === "coding" && window.ProductAgent) {
    await window.ProductAgent.prepareModelSettings();
  } else $("product-model-notice").hidden = true;
}
async function enterApp(session) {
  configureLogin(false);
  state.username = session.username;
  state.isOwner = session.is_owner !== false;
  state.csrf = session.csrf_token;
  state.connected = true;
  $("auth-screen").hidden = true;
  $("app-shell").hidden = false;
  $("login-error").textContent = "";
  $("password").value = "";
  $("confirm-password").value = "";
  $("mode-analysis").hidden = !state.isOwner;
  $("product-add-project").hidden = !state.isOwner;
  $("product-project-settings").hidden = !state.isOwner;
  $("open-settings").querySelector(".settings-icon").hidden = !state.isOwner;
  if (!state.isOwner) {
    $("connection-label").textContent = state.username + " · 退出登录";
    setCodingMode("coding");
    await window.ProductAgent?.enter();
    return;
  }
  await loadSettings();
  if (CODING.mode === "coding" && window.ProductAgent) {
    setCodingMode("coding");
    await window.ProductAgent.enter();
    return;
  }
  await loadHistory();
  const previous = saved("mgar-selected");
  if (previous) {
    try {
      await selectRun(previous);
    } catch {
      save("mgar-selected", "");
    }
  }
  if (
    (!state.profile.ready || !state.profile.automatic_execution) &&
    !state.demo
  ) {
    await showSettings();
    if (state.profile.ready)
      notify(
        "运行策略已升级为自动持续分析，保存一次设置即可启用。原有密钥可留空保留。",
      );
  }
}
async function login(event) {
  event.preventDefault();
  if (
    state.setupRequired &&
    $("password").value !== $("confirm-password").value
  ) {
    $("login-error").textContent = "两次输入的密码不一致。";
    return;
  }
  $("login-submit").disabled = true;
  $("login-error").textContent = "";
  try {
    const session = await call(
      "POST",
      "/api/v3/auth/" + (state.setupRequired ? "setup" : "login"),
      { username: $("username").value.trim(), password: $("password").value },
    );
    await enterApp(session);
  } catch (error) {
    if (state.connected) notify(explain(error), true);
    else $("login-error").textContent = explain(error);
  } finally {
    $("login-submit").disabled = false;
  }
}
async function saveModelSettings(event) {
  event.preventDefault();
  $("settings-error").textContent = "";
  const allowed = $("all-repositories").checked
    ? ["*"]
    : $("repository-scope")
        .value.split(/[\n,]+/)
        .map((x) => x.trim())
        .filter(Boolean);
  if (!allowed.length) {
    $("settings-error").textContent = "请至少填写一个允许分析的仓库。";
    return;
  }
  if (state.pendingSubmit && !$("allow-paid").checked) {
    $("settings-error").textContent =
      "开始真实分析前，请确认使用许可与费用上限。";
    return;
  }
  const body = {
    expected_revision: state.revision,
    model_id: $("model-id").value.trim(),
    allowed_repositories: allowed,
    allow_paid: $("allow-paid").checked,
    authorization_days: Number($("authorization-days").value),
    token_budget_mode: $("token-budget-mode").value,
    context_policy: {
      mode: $("context-mode").value,
      window_tokens: $("context-mode").value === "manual" ? Number($("context-window").value) : null,
      prepare_ratio: Number($("context-prepare").value) / 100,
      force_ratio: Number($("context-force").value) / 100,
      target_ratio: Number($("context-target").value) / 100,
      reserve_tokens: state.profile?.model?.context_policy?.reserve_tokens || 1024,
    },
    limits: {
      max_model_calls: null,
      max_tool_calls: null,
      max_input_tokens: Number($("token-limit").value),
      max_total_tokens: Number($("token-limit").value),
      max_cost_micros: Math.round(Number($("cost-limit").value) * 1000000),
      max_seconds: 3600,
    },
  };
  if ($("model-key").value.trim())
    body.deepseek_api_key = $("model-key").value.trim();
  if ($("github-key").value.trim())
    body.github_read_token = $("github-key").value.trim();
  $("save-model-settings").disabled = true;
  try {
    const data = await call("PUT", "/api/v3/settings/analysis", body);
    state.profile = data.profile;
    state.revision = data.revision;
    $("model-key").value = "";
    $("github-key").value = "";
    renderProfile();
    if (CODING.mode === "coding" && window.ProductAgent) {
      const profile = await window.ProductAgent.refresh();
      if (profile?.model?.available) {
        $("settings-dialog").close();
        notify("模型设置已保存。项目权限可在当前项目中设置。");
      } else {
        $("settings-error").textContent = "设置已保存，请选择在用模型，并补齐模型密钥与使用许可。";
      }
      return;
    }
    if (state.profile.ready) {
      $("settings-dialog").close();
      notify("设置已保存，可以开始分析。");
      if (state.pendingSubmit) {
        state.pendingSubmit = false;
        $("composer").requestSubmit();
      }
    } else {
      $("settings-error").textContent =
        "设置已保存，请补齐上方凭据并确认使用许可。";
    }
  } catch (error) {
    $("settings-error").textContent = explain(error);
  } finally {
    $("save-model-settings").disabled = false;
  }
}
function updateMode() {
  if (CODING.mode === "coding") {
    $("mode-badge").textContent = "项目助手";
    $("mode-badge").classList.remove("demo");
    return;
  }
  $("mode-badge").textContent = isDemo()
    ? "离线演示 · 合成样例"
    : "真实仓库分析";
  $("mode-badge").classList.toggle("demo", isDemo());
  $("demo-mode").checked = state.demo;
}
function renderHistory() {
  if (CODING.mode === "coding" && window.ProductAgent) {
    window.ProductAgent.renderHistory();
    return;
  }
  const selected = state.runs.find((run) => run.run_id === state.selected);
  const threads = new Map();
  for (const run of state.runs)
    if (!threads.has(run.thread_id)) threads.set(run.thread_id, run);
  const host = $("history");
  host.replaceChildren();
  for (const run of threads.values()) {
    const status = run.run_id === state.selected
      ? state.details.get(run.run_id)?.run.run_status || run.run_status : run.run_status;
    const button = element(
      "button",
      "history-item" +
        (selected && selected.thread_id === run.thread_id ? " active" : ""),
    );
    button.type = "button";
    button.title = run.task;
    const title = element("div", "history-title");
    title.append(element("strong", "", run.task));
    if (ACTIVE.has(status)) {
      const indicator = element("span", "history-running-indicator");
      indicator.setAttribute("role", "status");
      indicator.setAttribute("aria-label", "正在运行");
      title.append(indicator);
    }
    button.append(
      title,
      element(
        "small",
        "",
        run.repository + " · " + (LABELS[status] || status),
      ),
    );
    button.addEventListener("click", () =>
      selectRun(run.run_id).catch((error) => notify(explain(error), true)),
    );
    host.append(button);
  }
  if (!threads.size)
    host.append(
      element("p", "sidebar-empty", "还没有分析记录，提交第一个问题开始。"),
    );
}
async function loadHistory() {
  if (!state.connected) return;
  state.runs = await call("GET", API + "?limit=100");
  renderHistory();
}
function newChat() {
  if (CODING.mode === "coding" && window.ProductAgent) {
    window.ProductAgent.newChat();
    return;
  }
  closeStream();
  state.selection++;
  state.selected = null;
  state.signature = "";
  state.request = null;
  state.pendingSubmit = false;
  save("mgar-selected", "");
  $("welcome").hidden = false;
  $("conversation").hidden = true;
  $("repository-url").disabled = false;
  $("repository-ref").disabled = false;
  $("repository-ref").value = "";
  $("question").value = "";
  $("send-label").textContent = "开始分析";
  $("page-title").textContent = "新的仓库分析";
  setSidebar(false);
  renderHistory();
  updateComposer();
  updateMode();
  $("repository-url").focus();
}
function isAnalysisInputWait(detail) {
  return detail?.run?.run_status === "waiting_input" && detail.run.stop_reason === "INPUT_REQUIRED"
    && detail.interaction?.can_message === true && Boolean(detail.interaction?.question);
}
function updateComposer() {
  const detail = state.details.get(state.selected);
  const run = detail?.run;
  const active = run && ACTIVE.has(run.run_status);
  const paused = run?.run_status === "paused";
  const hasText = Boolean($("question").value.trim());
  const answering = isAnalysisInputWait(detail);
  const controlled = active || answering;
  const stopping = controlled && !hasText;
  $("send").disabled = state.busy || state.stopping;
  // The running action must bypass native empty-textarea validation.
  $("send").type = controlled || paused ? "button" : "submit";
  $("send").classList.toggle("is-stop", Boolean(stopping));
  $("send-icon").textContent = stopping ? "■" : "↑";
  // Reading or thinking does not lock the conversation. Clear only the exact
  // text acknowledged by the server, so typing during submission is preserved.
  $("question").disabled = state.stopping;
  $("question").maxLength = 8000;
  $("new-chat").disabled = state.busy;
  for (const button of $("history").querySelectorAll("button"))
    button.disabled = state.busy;
  $("send-label").textContent = state.stopping
    ? "正在中止…"
    : state.busy
    ? "正在提交…"
    : active
      ? hasText ? "发送补充" : "中止"
      : paused
        ? "继续执行"
      : answering
        ? hasText ? "回复并继续" : "中止"
      : state.selected
        ? "继续追问"
        : "开始分析";
  if (run) {
    $("repository-url").value = "https://github.com/" + run.repository;
    $("repository-url").disabled = true;
    $("repository-ref").value = run.commit;
    $("repository-ref").disabled = true;
    $("page-title").textContent = run.repository;
  }
}
async function selectRun(id) {
  const version = ++state.selection;
  const detail = await call("GET", API + "/" + encodeURIComponent(id));
  if (version !== state.selection) return;
  state.selected = id;
  state.details.set(id, detail);
  state.drafts.set(id, detail.draft || {});
  state.cursor = detail.last_event_id || 0;
  const ancestors = [];
  let current = detail.run;
  const seen = new Set([id]);
  while (
    current.parent_run_id &&
    ancestors.length < 30 &&
    !seen.has(current.parent_run_id)
  ) {
    seen.add(current.parent_run_id);
    const parent =
      state.details.get(current.parent_run_id) ||
      (await call(
        "GET",
        API + "/" + encodeURIComponent(current.parent_run_id),
      ));
    ancestors.push(parent);
    current = parent.run;
  }
  if (version !== state.selection) return;
  for (const parent of ancestors) state.details.set(parent.run.run_id, parent);
  save("mgar-selected", id);
  state.signature = "";
  $("welcome").hidden = true;
  $("conversation").hidden = false;
  setSidebar(false);
  updateComposer();
  updateMode();
  renderHistory();
  renderConversation();
  subscribeRun(id);
}
// Build every node as text. Model/repository HTML and unsafe Markdown URLs are never executed.
function inline(parent, text) {
  const pattern =
    /(\x60[^\x60]+\x60|\*\*[^*]+\*\*|\[[^\]]+\]\(https:\/\/[^)\s]+\))/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(offset, match.index)));
    const value = match[0];
    if (value.charCodeAt(0) === 96)
      parent.append(element("code", "", value.slice(1, -1)));
    else if (value.startsWith("**"))
      parent.append(element("strong", "", value.slice(2, -2)));
    else {
      const parts = value.match(/^\[([^\]]+)\]\((.+)\)$/);
      try {
        const url = new URL(parts[2]);
        if (
          url.protocol !== "https:" ||
          url.hostname !== "github.com" ||
          url.username ||
          url.password ||
          url.port
        )
          throw new Error("Untrusted URL");
        const link = element("a", "", parts[1]);
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        parent.append(link);
      } catch {
        parent.append(document.createTextNode(value));
      }
    }
    offset = match.index + value.length;
  }
  parent.append(document.createTextNode(text.slice(offset)));
}
function markdown(text) {
  const host = element("div");
  let fence = null,
    paragraph = [],
    list = null;
  const flush = () => {
    if (paragraph.length) {
      const p = element("p");
      inline(p, paragraph.join("\n"));
      host.append(p);
      paragraph = [];
    }
    list = null;
  };
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (/^(\x60){3}/.test(line)) {
      if (fence) {
        host.append(fence);
        fence = null;
      } else {
        flush();
        fence = element("pre");
        fence.append(element("code"));
      }
      continue;
    }
    if (fence) {
      fence.firstChild.append(
        document.createTextNode(raw.replace(/^ {4}/, "") + "\n"),
      );
      continue;
    }
    if (!line) {
      flush();
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)/);
    if (heading) {
      flush();
      const node = element("h" + heading[1].length);
      inline(node, heading[2]);
      host.append(node);
      continue;
    }
    if (/^---+$/.test(line)) {
      flush();
      host.append(element("hr"));
      continue;
    }
    if (line.startsWith("> ")) {
      flush();
      const quote = element("blockquote");
      inline(quote, line.slice(2));
      host.append(quote);
      continue;
    }
    if (/^[-*] /.test(line)) {
      if (paragraph.length) flush();
      if (!list) {
        list = element("ul");
        host.append(list);
      }
      const item = element("li");
      inline(item, line.slice(2));
      list.append(item);
      continue;
    }
    if (list) list = null;
    paragraph.push(raw);
  }
  flush();
  if (fence) host.append(fence);
  return host;
}
function runUsageText(run, budget, conversation) {
  const limits = conversation || run.limits || {};
  const accounting = conversation || budget;
  const parts = [run.execution_mode === "fixture"
    ? "合成样例演示" : "固定版本 " + run.commit.slice(0, 12)];
  const usage = conversation || run;
  const total = usage.input_tokens_used + (usage.output_tokens_used || 0);
  if (limits.max_total_tokens) {
    parts.push((conversation ? "会话累计 Token " : "累计 Token ") + total.toLocaleString() + " / " + limits.max_total_tokens.toLocaleString());
  } else {
    parts.push("已使用 " + total.toLocaleString() + " Token");
    if (limits.max_input_tokens) parts.push("输入上限 " + limits.max_input_tokens.toLocaleString());
  }
  if (Number.isFinite(accounting?.settled_micros)) {
    parts.push("已核算 US$" + (accounting.settled_micros / 1000000).toFixed(6));
  }
  const costLimit = accounting?.limit_micros ?? limits.max_cost_micros;
  if (Number.isFinite(costLimit)) parts.push("费用上限 US$" + (costLimit / 1000000).toFixed(2));
  if (accounting?.reserved_micros > 0) parts.push("预占 US$" + (accounting.reserved_micros / 1000000).toFixed(6));
  return parts.join(" · ");
}
function analysisProgressText(message) {
  const payload = message.payload || {};
  const tool = TOOLS[payload.tool] || "工具";
  switch (message.kind) {
    case "tool_reused": return (TOOLS[payload.tool] || "源码") + " · 已复用本会话的授权证据";
    case "tool_reserved": return "正在" + tool;
    case "tool_observed": return tool + "完成" + (Number.isFinite(payload.byte_count)
      ? " · 已读取 " + payload.byte_count.toLocaleString() + " 字节" : "");
    case "tool_failed": return tool + "未完成，正在核对反馈";
    case "model_reserved": return "正在准备下一次分析请求";
    case "model_stream_started": return payload.phase === "validation"
      ? "正在核对回答完整性" : "模型已响应，正在分析代码与证据";
    case "analysis_waiting": return "正在等待模型服务响应";
    case "acceptance_started": return "正在核对源码引用与回答完整性";
    case "acceptance_passed": return "核验通过，正在整理报告";
    case "draft_rejected":
      if (payload.reason === "MODEL_OUTPUT_TRUNCATED") return "回复达到输出上限，正在压缩后重试";
      if (payload.reason?.startsWith("MODEL_INVALID")) return "回复格式有误，正在重新生成";
      return "核验发现缺失，正在补充调查";
    case "analysis_user_message": return "补充要求已接收，等待安全点处理";
    case "analysis_input_applied": return "已处理补充要求，继续调查";
    case "analysis_replanned": return "已调整调查方向，继续搜索与核验";
    case "analysis_question": return "等待你补充信息，调查进度已保留";
    default: return "";
  }
}
function renderAnalysisProgress(host, detail) {
  // Only a fixed allowlist of public event fields enters the loading display.
  // Ignore reasoning, arbitrary provider text and unknown event kinds entirely.
  const entries = new Map();
  for (const item of (detail.events || []).slice(-200)) {
    const text = analysisProgressText(item);
    if (text) entries.set(item.event_id, {event_id: item.event_id, text});
  }
  for (const item of state.progress.get(detail.run.run_id) || []) entries.set(item.event_id, item);
  const recent = [...entries.values()].sort((a, b) => a.event_id - b.event_id).slice(-4);
  const current = recent.at(-1)?.text || (detail.run.run_status === "queued"
    ? "等待执行，准备读取仓库" : "正在调查代码与整理证据");
  const heading = element("div", "analysis-progress-heading");
  heading.append(element("span", "analysis-spinner"), element("strong", "", current));
  const log = element("ul", "analysis-progress-log");
  for (const item of recent.slice(0, -1)) log.append(element("li", "", item.text));
  host.replaceChildren(heading, log);
}
function renderTurn(detail) {
  const run = detail.run;
  const article = element("article", "turn");
  article.dataset.runId = run.run_id;
  article.append(element("div", "user-message", run.task));
  for (const message of detail.interaction?.messages || []) {
    const item = element("div", "analysis-user-update");
    item.append(element("div", "user-message", message.body), element("small", "analysis-message-status",
      message.status === "applied" ? "已纳入本轮调查" : "已接收 · 等待安全点处理"));
    article.append(item);
  }
  const heading = element("div", "assistant-heading");
  heading.append(
    element("span", "assistant-icon", "R"),
    element("span", "", "RepoSage"),
    element(
      "span",
      "run-status " + run.run_status,
      run.run_status === "failed" && run.stop_reason === "USAGE_UNKNOWN"
        ? "本轮中断，可继续追问" : LABELS[run.run_status] || run.run_status,
    ),
  );
  article.append(
    heading,
    element(
      "div",
      "run-meta",
      runUsageText(run, detail.budget, run.run_id === state.selected ? detail.conversation_budget : null),
    ),
  );
  if (ACTIVE.has(run.run_status)) {
    const progress = element("section", "analysis-progress");
    progress.dataset.analysisProgress = run.run_id;
    progress.setAttribute("role", "status");
    progress.setAttribute("aria-live", "polite");
    renderAnalysisProgress(progress, detail);
    article.append(progress);
  }
  const activity = element("details", "activity");
  activity.dataset.activity = run.run_id;
  const calls = detail.tool_calls || [];
  activity.append(
    element("summary", "", "执行过程 · " + calls.length + " 次工具调用"),
  );
  const log = element("ol");
  if (!calls.length)
    log.append(element("li", "", "Agent 将根据问题选择搜索、读取目录或源码。"));
  for (const call of calls) {
    const row = element("li");
    row.append(
      element("span", "tool-name", TOOLS[call.tool] || call.tool),
      element("span", "", call.result_uri || call.error_code || "正在读取…"),
      element("span", "tool-status", call.status),
    );
    log.append(row);
  }
  activity.append(log);
  article.append(activity);
  const question = detail.interaction?.question;
  if (run.run_status === "waiting_input" && question && detail.interaction?.can_message) {
    const waiting = element("section", "analysis-question");
    waiting.setAttribute("role", "status");
    waiting.append(element("strong", "", "等待你的回复"), element("p", "", question.question),
      element("p", "analysis-question-note", "在下方输入框回复，继续本轮调查。"));
    if (Array.isArray(question.options) && run.run_id === state.selected) {
      const options = element("div", "analysis-question-options");
      for (const option of question.options.slice(0, 8)) {
        const text = typeof option === "string" ? option : option.label;
        if (!text || typeof text !== "string") continue;
        const button = element("button", "ghost-button", text);
        button.type = "button";
        button.addEventListener("click", () => {
          $("question").value = text.slice(0, 8000);
          updateComposer();
          $("question").focus();
        });
        options.append(button);
      }
      waiting.append(options);
    }
    article.append(waiting);
  }
  const result = element("div", "run-result");
  const reason = run.stop_reason || run.run_reason_code;
  if (
    reason &&
    reason !== "complete" &&
    !["queued", "running", "paused"].includes(run.run_status) && !question
  ) {
    result.append(
      element(
        "div",
        "run-error",
        (ERRORS[reason] || "本次分析已停止，请保留运行记录并查看服务器日志。") +
          "",
      ),
    );
  }
  const draft = element("div", "stream-draft");
  const currentDraft = state.drafts.get(run.run_id) || detail.draft || {};
  renderDraftContent(draft, currentDraft, ACTIVE.has(run.run_status));
  draft.hidden = Boolean(detail.report) || !Object.keys(currentDraft).length;
  result.append(draft);
  if (detail.report) result.append(markdown(detail.report));
  else
    result.append(
      element(
        "p",
        "empty-report",
        ACTIVE.has(run.run_status)
          ? "正在调查代码，完成后结论会自动显示在这里。"
          : run.run_status === "paused"
            ? "分析已暂停，可以恢复或补充问题。"
            : run.run_status === "waiting_input" && question
              ? "调查进度已保存，收到你的回复后继续。"
            : run.run_status === "cancelled"
              ? "分析已中止，当前证据和上下文已保留；调用停稳后可继续追问。"
            : "本次运行还没有生成报告。",
      ),
    );
  if (detail.claims?.length) {
    const evidence = element("details", "evidence-list");
    evidence.append(
      element(
        "summary",
        "",
        "查看 " + detail.claims.length + " 条已核验的源码引用",
      ),
    );
    for (const claim of detail.claims) {
      const block = element("div", "evidence-claim");
      block.append(
        element("strong", "", claim.statement),
        element("blockquote", "", claim.quote),
        element(
          "small",
          "",
          "行 " +
            claim.start_line +
            "–" +
            claim.end_line +
            " · 证据 " +
            claim.evidence_id,
        ),
      );
      evidence.append(block);
    }
    result.append(evidence);
  }
  article.append(result);
  if (!ACTIVE.has(run.run_status) && !question && detail.continuation?.state !== "ready") {
    const message = detail.continuation?.state === "inflight"
      || detail.continuation?.explicit_followup_allowed === false
      ? "当前调用仍在结束。确认回执后可沿已有上下文继续，页面最多自动追踪两分钟。"
      : detail.continuation?.state === "unresolved" ? ERRORS.ANALYSIS_USAGE_UNRESOLVED : "";
    if (message) article.append(element("p", "continuation-note", message));
  }
  const controls = element("div", "run-controls");
  if (run.execution_mode === "live" && window.ProductAgent?.importGitHubSnapshot) {
    const repair = element("button", "", "在隔离工作区修复并准备 PR");
    repair.type = "button";
    repair.addEventListener("click", () => window.ProductAgent.importGitHubSnapshot(run));
    controls.append(repair);
  }
  if (detail.report) {
    const button = element("button", "", "下载报告");
    button.addEventListener("click", () => {
      const url = URL.createObjectURL(
        new Blob([detail.report], { type: "text/markdown;charset=utf-8" }),
      );
      const link = element("a");
      link.href = url;
      link.download = "reposage-" + run.run_id + ".md";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
    controls.append(button);
  }
  article.append(controls);
  return article;
}
function renderConversation() {
  if (!state.selected) return;
  const chain = [],
    seen = new Set();
  let detail = state.details.get(state.selected);
  while (detail && !seen.has(detail.run.run_id)) {
    chain.unshift(detail);
    seen.add(detail.run.run_id);
    detail = state.details.get(detail.run.parent_run_id);
  }
  const signature = JSON.stringify(chain);
  if (signature === state.signature) return;
  state.signature = signature;
  const nearBottom =
    window.scrollY + window.innerHeight >=
    document.documentElement.scrollHeight - 160;
  const opened = new Set(
    [...document.querySelectorAll("details[data-activity][open]")].map(
      (item) => item.dataset.activity,
    ),
  );
  $("conversation").replaceChildren(...chain.map(renderTurn));
  for (const node of document.querySelectorAll("details[data-activity]"))
    if (opened.has(node.dataset.activity)) node.open = true;
  if (nearBottom)
    window.scrollTo({
      top: document.documentElement.scrollHeight,
      behavior: "instant",
    });
}
async function control(id, action, button) {
  if (state.stopping) return;
  state.stopping = true;
  button.disabled = true;
  updateComposer();
  try {
    await call("POST", API + "/" + id + "/" + action);
    if (state.selected === id) await selectRun(id);
    await loadHistory();
    if (action === "cancel") notify("中止请求已接收。当前调用停稳并核实用量后，可沿已有上下文继续追问。");
  } catch (error) {
    notify(explain(error), true);
  } finally {
    state.stopping = false;
    updateComposer();
  }
}
function repositoryValid(value) {
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      url.hostname === "github.com" &&
      !url.username &&
      !url.password &&
      !url.port &&
      !url.search &&
      !url.hash &&
      /^\/[^/]+\/[^/]+(?:\/(?:tree|commit)\/.+)?\/?$/.test(url.pathname)
    );
  } catch {
    return false;
  }
}
async function submit(event) {
  event.preventDefault();
  if (state.busy) return;
  const task = $("question").value.trim();
  const repository = $("repository-url").value.trim();
  if (!task) return;
  if (!state.connected) {
    showLogin();
    return;
  }
  if (!state.selected && !repositoryValid(repository)) {
    notify(
      "请输入完整的 GitHub 仓库 URL，例如 https://github.com/owner/repository。",
      true,
    );
    return;
  }
  state.busy = true;
  const parent = state.selected;
  const previousDetail = state.details.get(parent);
  const interactive = previousDetail && (ACTIVE.has(previousDetail.run.run_status)
    || isAnalysisInputWait(previousDetail));
  updateComposer();
  notify(interactive ? "正在提交补充信息…" : parent ? "正在提交追问…" : "正在连接仓库并固定代码版本…");
  try {
    // Existing running requests own frozen authority. An inbox message must not
    // depend on the latest settings or issue another paid model request here.
    if (!isDemo() && !interactive) {
      state.profile = await call("GET", API + "/profile");
      renderProfile();
      if (!state.profile.ready || !state.profile.automatic_execution) {
        state.pendingSubmit = true;
        showSettings();
        notify("补齐模型设置后即可继续，问题和仓库地址已保留。");
        return;
      }
    }
    let path = API,
      body;
    if (parent) {
      path += "/" + parent + "/messages";
      body = { body: task };
    } else {
      body = {
        repository_url: repository,
        task,
        execution_mode: state.demo ? "fixture" : "live",
      };
      if (!state.demo) body.limits = state.profile.limits;
      const ref = $("repository-ref").value.trim();
      if (ref) {
        if (/^[a-fA-F0-9]{40}$/.test(ref)) body.commit = ref;
        else body.ref = ref;
      }
    }
    // Keep the same id after an ambiguous network failure; the server owns idempotency.
    const fingerprint = JSON.stringify({ path, body });
    if (state.request?.fingerprint !== fingerprint)
      state.request = { fingerprint, id: crypto.randomUUID() };
    body.client_request_id = state.request.id;
    const run = await call("POST", path, body);
    state.request = null;
    // The POST succeeded even if a later refresh fails. Keep its identity so
    // a temporary read failure cannot turn another click into duplicate work.
    state.selected = run.run_id;
    const sameRun = parent === run.run_id;
    state.details.set(run.run_id, sameRun ? {...previousDetail, run} : {
      run, tool_calls: [], claims: [], report: null,
    });
    save("mgar-selected", run.run_id);
    if ($("question").value.trim() === task) $("question").value = "";
    $("welcome").hidden = true;
    $("conversation").hidden = false;
    renderConversation();
    if (sameRun) {
      const detail = await call("GET", API + "/" + run.run_id);
      if (state.selected === run.run_id) {
        state.details.set(run.run_id, detail);
        state.drafts.set(run.run_id, detail.draft || {});
        state.cursor = Math.max(state.cursor, detail.last_event_id || 0);
        renderConversation();
        renderHistory();
        if ((!state.stream || state.stream.readyState === 2) && ACTIVE.has(run.run_status))
          subscribeRun(run.run_id);
      }
    } else await selectRun(run.run_id);
    await loadHistory();
    notify(sameRun ? "补充信息已接收，助手会沿本轮证据继续处理。" : "");
  } catch (error) {
    if (
      error.status === 403 &&
      [
        "REPO_ANALYSIS_SCOPE_DENIED",
        "REPO_ANALYSIS_PROFILE_REQUIRED",
        "REPO_ANALYSIS_PROFILE_INVALID",
      ].includes(error.code)
    ) {
      state.pendingSubmit = true;
      await showSettings();
      $("settings-error").textContent = explain(error);
    }
    notify(explain(error), true);
  } finally {
    state.busy = false;
    updateComposer();
  }
}
function closeStream() {
  state.streamVersion++;
  if (state.stream) state.stream.close();
  state.stream = null;
  clearTimeout(state.progressTimer);
}
function renderDraftContent(host, draft, active = true) {
  host.replaceChildren(
    element(
      "p",
      "draft-note",
      active
        ? "正在生成 · 完成后核对源码与回答完整性"
        : "未完成草稿 · 尚未通过核验",
    ),
  );
  for (const [field, title] of [
    ["conclusion", "当前结论"],
    ["structure", "项目结构"],
  ]) {
    if (draft[field])
      host.append(element("h3", "", title), element("p", "", draft[field]));
  }
}
function subscribeRun(id) {
  closeStream();
  const detail = state.details.get(id);
  if (!state.connected || !detail || !ACTIVE.has(detail.run.run_status)) return;
  const version = state.streamVersion;
  const source = new EventSource(
    API + "/" + id + "/events?after=" + state.cursor,
  );
  state.stream = source;
  source.addEventListener("update", (event) => {
    if (version !== state.streamVersion || id !== state.selected) return;
    const message = JSON.parse(event.data);
    if (message.event_id <= state.cursor) return;
    state.cursor = message.event_id;
    const payload = message.payload || {};
    const progressText = analysisProgressText(message);
    if (progressText) {
      const recent = state.progress.get(id) || [];
      recent.push({event_id: message.event_id, text: progressText});
      state.progress.set(id, recent.slice(-4));
      const host = document.querySelector('.turn[data-run-id="' + id + '"] [data-analysis-progress]');
      if (host) renderAnalysisProgress(host, state.details.get(id));
    }
    const draft = state.drafts.get(id) || {};
    if (
      (message.kind === "model_stream_started" &&
        payload.phase === "analysis") ||
      message.kind === "draft_rejected"
    ) {
      state.drafts.set(id, {});
      const host = document.querySelector(
        '.turn[data-run-id="' + id + '"] .stream-draft',
      );
      if (host) host.hidden = true;
    }
    if (message.kind === "answer_delta") {
      draft[payload.field] =
        Array.from(draft[payload.field] || "")
          .slice(0, payload.offset)
          .join("") + payload.text;
      state.drafts.set(id, draft);
      const host = document.querySelector(
        '.turn[data-run-id="' + id + '"] .stream-draft',
      );
      if (host) {
        host.hidden = false;
        renderDraftContent(host, draft);
      }
    }
    const labels = {
      model_stream_started:
        payload.phase === "validation" ? "正在核对回答完整性" : "正在分析",
      tool_reserved: TOOLS[payload.tool] || "正在读取代码",
      tool_observed: "正在整理源码证据",
      acceptance_started: "正在核对回答完整性",
      analysis_waiting: "正在等待模型服务",
      acceptance_passed: "核验通过",
      draft_rejected: analysisProgressText(message),
    };
    const label = document.querySelector(
      '.turn[data-run-id="' + id + '"] .run-status',
    );
    if (label && labels[message.kind]) label.textContent = labels[message.kind];
    if (
      ["tool_observed", "tool_failed", "model_settled", "analysis_user_message",
        "analysis_input_applied", "analysis_question", "analysis_replanned"].includes(message.kind)
    ) {
      clearTimeout(state.progressTimer);
      state.progressTimer = setTimeout(async () => {
        try {
          const data = await call("GET", API + "/" + id);
          if (version !== state.streamVersion || id !== state.selected) return;
          if ((data.last_event_id || 0) >= state.cursor) {
            const previousStatus = state.details.get(id)?.run.run_status;
            state.details.set(id, data);
            state.drafts.set(id, data.draft || {});
            state.cursor = data.last_event_id || state.cursor;
            renderConversation();
            if (previousStatus !== data.run.run_status) renderHistory();
            updateComposer();
          }
        } catch (error) {
          notify(explain(error), true);
        }
      }, 200);
    }
  });
  source.addEventListener("done", async () => {
    if (version !== state.streamVersion || id !== state.selected) return;
    closeStream();
    try {
      await selectRun(id);
      await loadHistory();
    } catch (error) {
      notify(explain(error), true);
    }
  });
  source.addEventListener("auth_required", () => {
    closeStream();
    showLogin();
  });
  source.onopen = () => {
    if ($("notice").textContent === "连接恢复中，分析仍在后台继续。")
      notify("");
  };
  source.onerror = () => {
    if (version === state.streamVersion && id === state.selected)
      notify("连接恢复中，分析仍在后台继续。");
  };
}
function canPollAnalysisDetail(detail) {
  if (!detail) return false;
  const run = detail.run;
  if (ACTIVE.has(run.run_status) || run.run_status === "paused") return true;
  // Unknown or orphaned terminal calls need reconciliation, not an endless
  // progress loop. Only a confirmed in-flight cancellation may settle itself.
  if (run.run_status !== "cancelled" || detail.continuation?.state !== "inflight") {
    state.continuationWaits.delete(run.run_id);
    return false;
  }
  const wait = state.continuationWaits.get(run.run_id) || {started: Date.now(), notified: false};
  state.continuationWaits.set(run.run_id, wait);
  if (Date.now() - wait.started < 120000) return true;
  if (!wait.notified) {
    wait.notified = true;
    notify("当前调用尚未结束，已停止自动刷新。稍后刷新页面或再次追问以核对回执。", true);
  }
  return false;
}
async function poll() {
  if (CODING.mode === "coding" && window.ProductAgent) {
    if (state.connected && !document.hidden) await window.ProductAgent.poll();
    return;
  }
  if (!state.connected || state.polling || document.hidden) return;
  state.polling = true;
  const selected = state.selected,
    version = state.selection;
  try {
    if (
      selected &&
      canPollAnalysisDetail(state.details.get(selected)) &&
      (!state.stream || state.stream.readyState !== EventSource.OPEN)
    ) {
      const detail = await call("GET", API + "/" + selected);
      if (selected === state.selected && version === state.selection) {
        if ((detail.last_event_id || 0) >= state.cursor) {
          state.details.set(selected, detail);
          state.drafts.set(selected, detail.draft || {});
          state.cursor = detail.last_event_id || 0;
        }
        renderConversation();
        updateComposer();
      }
    }
    await loadHistory();
  } catch (error) {
    notify(explain(error), true);
  } finally {
    state.polling = false;
  }
}
function setSidebar(open) {
  $("sidebar").classList.toggle("open", open);
  $("toggle-sidebar").setAttribute("aria-expanded", String(open));
}
function bindAgentUI() {
  $("open-settings").addEventListener("click", showSettings);
  $("close-settings").addEventListener("click", () => {
    state.pendingSubmit = false;
    $("settings-dialog").close();
  });
  $("settings-dialog").addEventListener("cancel", () => {
    state.pendingSubmit = false;
  });
  $("login-form").addEventListener("submit", login);
  $("model-settings-form").addEventListener("submit", saveModelSettings);
  $("context-mode").addEventListener("change", () => {
    $("context-window").disabled = $("context-mode").value === "auto";
  });
  $("token-budget-mode").addEventListener("change", updateTokenBudgetMode);
  $("all-repositories").addEventListener("change", () => {
    $("repository-scope-row").hidden = $("all-repositories").checked;
  });
  $("logout").addEventListener("click", async () => {
    try {
      await call("POST", "/api/v3/auth/logout");
      state.details.clear();
      state.runs = [];
      state.selected = null;
      state.pendingSubmit = false;
      save("mgar-selected", "");
      $("conversation").replaceChildren();
      newChat();
      showLogin();
      configureLogin(false);
    } catch (error) {
      notify(explain(error), true);
    }
  });
  $("new-chat").addEventListener("click", newChat);
  $("refresh-history").addEventListener("click", () =>
    (CODING.mode === "coding" && window.ProductAgent
      ? window.ProductAgent.refresh()
      : loadHistory()).catch((error) => notify(explain(error), true)),
  );
  $("toggle-sidebar").addEventListener("click", () => setSidebar(!$("sidebar").classList.contains("open")));
  $("close-sidebar").addEventListener("click", () => {
    setSidebar(false); $("toggle-sidebar").focus();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && $("sidebar").classList.contains("open")) {
      setSidebar(false); $("toggle-sidebar").focus();
    }
  });
  $("demo-mode").addEventListener("change", () => {
    state.demo = $("demo-mode").checked;
    save("mgar-demo", state.demo ? "1" : "");
    updateMode();
    if (state.selected) newChat();
  });
  $("composer").addEventListener("submit", submit);
  $("send").addEventListener("click", (event) => {
    const detail = state.details.get(state.selected);
    const run = detail?.run;
    const controlled = run && (ACTIVE.has(run.run_status) || isAnalysisInputWait(detail));
    if (!run || (!controlled && run.run_status !== "paused")) return;
    event.preventDefault();
    if (controlled && $("question").value.trim()) {
      submit(event);
      return;
    }
    control(run.run_id, run.run_status === "paused" ? "resume" : "cancel", $("send"));
  });
  $("question").addEventListener("input", updateComposer);
  $("question").addEventListener("keydown", (event) => {
    if (
      event.key !== "Enter" ||
      event.shiftKey ||
      event.isComposing ||
      event.keyCode === 229
    )
      return;
    event.preventDefault();
    if (!event.repeat && !$("send").disabled) $("send").click();
  });
  for (const button of document.querySelectorAll("[data-prompt]"))
    button.addEventListener("click", () => {
      $("question").value = button.dataset.prompt;
      updateComposer();
      $("question").focus();
    });
  window.addEventListener("pagehide", () => {
    clearInterval(state.timer);
    closeStream();
  });
  window.addEventListener("pageshow", () => {
    clearInterval(state.timer);
    state.timer = setInterval(poll, 10000);
    if (state.selected && state.connected && !state.stream)
      subscribeRun(state.selected);
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) poll();
  });
}
function setCodingMode(mode) {
  if (!state.isOwner && mode !== "coding") return;
  const coding = mode === "coding";
  document.body.classList.toggle("product-mode", coding);
  CODING.mode = mode;
  $("mode-analysis").classList.toggle("is-active", !coding);
  $("mode-coding").classList.toggle("is-active", coding);
  $("mode-analysis").setAttribute("aria-pressed", String(!coding));
  $("mode-coding").setAttribute("aria-pressed", String(coding));
  $("coding-panel").hidden = true;
  $("product-panel").hidden = !coding;
  $("welcome").hidden = coding ? true : Boolean(state.selected);
  $("conversation").hidden = coding ? true : !$("conversation").childElementCount;
  document.querySelector(".composer-wrap").hidden = coding;
  if (coding) {
    closeStream();
    $("new-chat").replaceChildren(element("span", "", "＋"), document.createTextNode(" 新建对话"));
    $("page-title").textContent = "新的项目对话";
    $("mode-badge").textContent = "项目助手";
    window.ProductAgent?.enter().catch((error) => notify(explain(error), true));
  } else {
    window.ProductAgent?.leave();
    $("new-chat").replaceChildren(element("span", "", "＋"), document.createTextNode(" 新建分析"));
    $("page-title").textContent = "新的仓库分析";
    $("mode-badge").textContent = "REPOSITORY AGENT";
    updateMode();
    loadHistory().catch((error) => notify(explain(error), true));
  }
}
function setCodingError(message) {
  $("coding-error").textContent = message || "";
}
async function loadCodingProjects() {
  const projects = await call("GET", "/api/v3/harness/projects");
  CODING.projects = Array.isArray(projects) ? projects : [];
  const select = $("coding-project");
  select.replaceChildren();
  if (!CODING.projects.length) {
    select.disabled = true;
    $("coding-no-project").hidden = false;
    return;
  }
  select.disabled = false;
  $("coding-no-project").hidden = true;
  for (const project of CODING.projects) {
    const option = document.createElement("option");
    option.value = project.project_id;
    option.textContent = project.repository + " · " + project.path;
    select.append(option);
  }
}
async function registerCodingProject() {
  const path = $("coding-path").value.trim();
  if (!path) {
    setCodingError("请输入项目目录的绝对路径。");
    return;
  }
  $("coding-register").disabled = true;
  setCodingError("");
  try {
    await call("POST", "/api/v3/harness/projects", { path });
    $("coding-path").value = "";
    await loadCodingProjects();
  } catch (error) {
    setCodingError(explain(error));
  } finally {
    $("coding-register").disabled = false;
  }
}
async function submitCoding(event) {
  event.preventDefault();
  if (CODING.busy) return;
  setCodingError("");
  const projectId = $("coding-project").value;
  const objective = $("coding-objective").value.trim();
  const kind = $("coding-kind").value;
  const live = $("coding-live").checked;
  const approved = $("coding-approve").checked;
  const target = $("coding-target").value.trim();
  const oldText = $("coding-old").value;
  if (!projectId) {
    setCodingError("请先注册并选择项目。");
    return;
  }
  if (!objective) {
    setCodingError("请描述这次任务的目标。");
    return;
  }
  if (kind !== "inspect" && !approved) {
    setCodingError(explain({ code: "HARNESS_APPROVAL_REQUIRED" }));
    return;
  }
  if (kind !== "inspect" && !live && (!target || !oldText)) {
    setCodingError(
      "本地确定性适配器需要目标文件与替换前文本，请展开“受控适配器参数”填写。",
    );
    return;
  }
  CODING.busy = true;
  $("coding-submit").disabled = true;
  $("coding-submit").textContent = "任务运行中…";
  $("coding-result").hidden = false;
  try {
    const result = await call("POST", "/api/v3/harness/tasks", {
      project_id: projectId,
      objective: objective,
      task_kind: kind,
      test_command: $("coding-test").value.trim(),
      target_path: target,
      old_text: oldText,
      new_text: $("coding-new").value,
      approved: approved,
      mode: live ? "live" : "deterministic",
      background: true,
    });
    openCodingTask(result.task_id, result.status);
  } catch (error) {
    setCodingError(explain(error));
    resetCodingSubmit();
  }
}
function resetCodingSubmit() {
  CODING.busy = false;
  $("coding-submit").disabled = false;
  $("coding-submit").textContent = "开始编码任务";
}
function codingLog(kind, text) {
  const host = $("coding-live-log");
  const item = document.createElement("li");
  item.className = kind;
  item.textContent = text;
  host.append(item);
  host.scrollTop = host.scrollHeight;
}
function setCodingStatus(status) {
  $("coding-live").hidden = false;
  $("coding-live-status").textContent = status || "运行中";
}
function setCodingCursor(cursor) {
  $("coding-live-cursor").textContent = "cursor " + cursor;
}
function closeCodingStream() {
  if (CODING.stream) {
    CODING.stream.close();
    CODING.stream = null;
  }
}
function openCodingTask(taskId, status) {
  closeCodingStream();
  CODING.taskId = taskId;
  const savedCursor = Number(
    sessionStorage.getItem("mgar-coding-cursor:" + taskId) || "0",
  );
  CODING.cursor = Number.isFinite(savedCursor) && savedCursor > 0 ? savedCursor : 0;
  $("coding-live").hidden = false;
  $("coding-live-log").replaceChildren();
  $("coding-cancel-receipt").textContent = "";
  $("coding-question").hidden = true;
  $("coding-question-text").textContent = "";
  $("coding-question").dataset.questionId = "";
  setCodingStatus(status || "queued");
  setCodingCursor(CODING.cursor);
  subscribeCoding();
}
function subscribeCoding() {
  const id = CODING.taskId;
  if (!id) return;
  closeCodingStream();
  const source = new EventSource(
    "/api/v3/harness/tasks/" + id + "/events?after=" + CODING.cursor + "&follow=true",
  );
  CODING.stream = source;
  source.addEventListener("update", (event) => {
    if (id !== CODING.taskId) return;
    let record;
    try {
      record = JSON.parse(event.data);
    } catch {
      return;
    }
    const cursor = Number(record.cursor || 0);
    if (cursor <= CODING.cursor) return;
    CODING.cursor = cursor;
    sessionStorage.setItem("mgar-coding-cursor:" + id, String(cursor));
    setCodingCursor(cursor);
    handleCodingEvent(record);
  });
  source.addEventListener("done", async (event) => {
    if (id !== CODING.taskId) return;
    closeCodingStream();
    let done = {};
    try {
      done = JSON.parse(event.data || "{}");
    } catch {
      done = {};
    }
    setCodingStatus(done.status || "finished");
    await finishCodingTask();
  });
  source.addEventListener("auth_required", () => {
    closeCodingStream();
    showLogin();
  });
  source.onerror = () => {
    // The browser reconnects automatically and sends Last-Event-ID.  Fall back
    // to one status read so a terminal result is not hidden by a broken stream.
    finishCodingTask().catch(() => {});
  };
}
function handleCodingEvent(record) {
  const kind = record.kind;
  const payload = record.payload || {};
  switch (kind) {
    case "run_started":
      codingLog("", "任务开始");
      setCodingStatus("running");
      break;
    case "approval_recorded":
      codingLog("", "审批与租约已记录");
      break;
    case "plan_bound":
      codingLog("", "计划已绑定");
      break;
    case "model_requested":
      codingLog("", "请求模型");
      break;
    case "model_completed":
      codingLog("", "模型返回 · finish=" + (payload.finish_reason || ""));
      break;
    case "model_discarded_stale":
      codingLog("warn", "旧模型结果已作废，将在安全点应用新指示");
      break;
    case "replanned":
      codingLog(
        "",
        "安全点重规划 · " + (payload.instructions || []).join(" / "),
      );
      break;
    case "instruction_applied":
      codingLog("ok", "已应用补充指示：" + (payload.text || ""));
      setCodingMessageStatus(payload.message_id, "applied");
      break;
    case "inbox_received":
      codingLog("", "收到消息（" + (payload.kind || "") + "）");
      break;
    case "tool_started":
      codingLog("", "工具开始 · " + (payload.name || ""));
      break;
    case "tool_completed":
      codingLog(
        payload.status === "ok" ? "ok" : "warn",
        "工具结束 · " + (payload.name || "") + " · " + (payload.status || ""),
      );
      break;
    case "patch_applied":
      codingLog("ok", "补丁已应用 · " + String(payload.tree || "").slice(0, 12));
      break;
    case "test_completed":
      codingLog(
        payload.tests_failed ? "bad" : "ok",
        "测试 " + payload.tests_run + " 运行 / " + payload.tests_failed + " 失败",
      );
      break;
    case "command_completed":
      codingLog(
        payload.exit_code === 0 ? "ok" : "bad",
        "命令 " +
          (payload.argv || []).join(" ") +
          " · exit " +
          payload.exit_code,
      );
      break;
    case "ask_user":
      showCodingQuestion(payload.question_id, payload.question);
      break;
    case "ask_user_answered":
      hideCodingQuestion();
      codingLog("ok", "问题已回答，任务继续");
      break;
    case "pause_requested":
      setCodingStatus("pausing");
      break;
    case "paused":
      setCodingStatus("paused");
      break;
    case "resume_requested":
      codingLog("", "恢复请求已发送");
      break;
    case "resumed":
      setCodingStatus("running");
      break;
    case "cancel_requested":
      setCodingStatus("cancelling（已确认接收，实际停止以回执为准）");
      break;
    case "cancel_observed":
      setCodingStatus("cancelled（已观察执行器回执）");
      break;
    case "artifact_ready":
      codingLog("ok", "产物已就绪");
      break;
    case "run_failed":
      codingLog("bad", "任务失败：" + (payload.message || payload.code || ""));
      break;
    default:
      codingLog("", kind);
  }
}
function showCodingQuestion(questionId, question) {
  CODING.questionId = questionId || "";
  $("coding-question").hidden = false;
  $("coding-question").dataset.questionId = questionId || "";
  $("coding-question-text").textContent = question || "需要补充信息";
  setCodingStatus("waiting_input");
}
function hideCodingQuestion() {
  CODING.questionId = "";
  $("coding-question").hidden = true;
  $("coding-question-text").textContent = "";
  $("coding-question").dataset.questionId = "";
}
function appendCodingMessage(message, duplicate) {
  if (!message || !message.message_id) return;
  CODING.messages.push(message);
  const host = $("coding-messages");
  const item = document.createElement("li");
  item.dataset.messageId = message.message_id;
  item.className = message.status === "applied" ? "ok" : "";
  item.textContent =
    "[" +
    (message.status || "received") +
    "] " +
    (message.kind || "") +
    " · " +
    (message.text || "") +
    (duplicate ? "（重复，未再次应用）" : "");
  host.append(item);
}
function setCodingMessageStatus(messageId, status) {
  for (const message of CODING.messages) {
    if (message.message_id === messageId) message.status = status;
  }
  const item = $("coding-messages").querySelector(
    'li[data-message-id="' + messageId + '"]',
  );
  if (!item) return;
  item.classList.toggle("ok", status === "applied");
  item.textContent = item.textContent.replace(/^\[[^\]]*\]/, "[" + status + "]");
}
async function sendCodingMessage() {
  const id = CODING.taskId;
  if (!id) {
    setCodingError("请先开始一个任务。");
    return;
  }
  const text = $("coding-message").value.trim();
  if (!text) return;
  $("coding-send").disabled = true;
  const messageId =
    "m-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
  try {
    const result = await call(
      "POST",
      "/api/v3/harness/tasks/" + id + "/messages",
      { kind: "instruction", text, message_id: messageId },
    );
    appendCodingMessage(result.message, result.duplicate);
    $("coding-message").value = "";
    codingLog(
      "",
      result.duplicate
        ? "重复消息已忽略，不会再次应用"
        : "已收到补充指示，将在安全点应用",
    );
  } catch (error) {
    setCodingError(explain(error));
  } finally {
    $("coding-send").disabled = false;
  }
}
async function sendCodingAnswer() {
  const id = CODING.taskId;
  const questionId = CODING.questionId || $("coding-question").dataset.questionId;
  const text = $("coding-answer").value.trim();
  if (!id || !questionId) {
    setCodingError("当前没有待回答的问题。");
    return;
  }
  if (!text) return;
  $("coding-answer-send").disabled = true;
  const messageId =
    "a-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
  try {
    const result = await call(
      "POST",
      "/api/v3/harness/tasks/" + id + "/messages",
      { kind: "answer", text, message_id: messageId, reply_to: questionId },
    );
    appendCodingMessage(result.message, result.duplicate);
    $("coding-answer").value = "";
    hideCodingQuestion();
  } catch (error) {
    setCodingError(explain(error));
  } finally {
    $("coding-answer-send").disabled = false;
  }
}
async function controlCoding(action) {
  const id = CODING.taskId;
  if (!id) return;
  try {
    const result = await call(
      "POST",
      "/api/v3/harness/tasks/" + id + "/" + action,
    );
    if (action === "cancel") {
      setCodingStatus("cancelling");
      const receipt = result.cancel_receipt || {};
      $("coding-cancel-receipt").textContent =
        "取消回执：" +
        (result.requested_at || receipt.requested_at || "") +
        " · 状态=" +
        (receipt.status || "requested") +
        " · 退款=" +
        (result.refund || receipt.refund || "not_promised") +
        "（实际停止以 Runner/provider 回执为准）";
      codingLog(
        "warn",
        "取消请求已确认接收；实际停止以 Runner/provider 回执为准，不承诺退款。",
      );
    } else {
      setCodingStatus(result.status || action);
    }
  } catch (error) {
    setCodingError(explain(error));
  }
}
async function finishCodingTask() {
  if (!CODING.taskId) return;
  try {
    const result = await call("GET", "/api/v3/harness/tasks/" + CODING.taskId);
    renderCodingResult(result);
    const terminal = [
      "completed",
      "blocked",
      "denied",
      "failed",
      "cancelled",
      "incomplete",
      "budget_exhausted",
      "unknown",
    ].includes(result.status);
    if (terminal) {
      closeCodingStream();
      resetCodingSubmit();
    }
  } catch (error) {
    setCodingError(explain(error));
  }
}
function renderCodingResult(result) {
  CODING.result = result;
  $("coding-result").hidden = false;
  const badge = $("coding-result-badge");
  const ok = result.status === "completed";
  badge.textContent =
    (ok ? "完成" : result.status || "未完成") +
    " · " +
    (result.live_model ? "真实模型" : "确定性适配器");
  badge.classList.toggle("live", Boolean(result.live_model));
  $("coding-result-title").textContent = result.summary || "运行结果";
  const stats = [
    ["状态", result.status],
    ["测试", (result.tests?.run || 0) + " / " + (result.tests?.failed || 0) + " 失败"],
    ["模型调用", result.model_calls],
    ["输出字节", result.output_bytes],
    ["费用(µ)", result.cost_micros],
    ["协议", result.protocol],
  ];
  const list = $("coding-stats");
  list.replaceChildren();
  for (const [key, value] of stats) {
    const cell = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = key;
    const detail = document.createElement("dd");
    detail.textContent = String(value);
    cell.append(term, detail);
    list.append(cell);
  }
  const files = $("coding-files");
  files.replaceChildren();
  for (const change of result.changes || []) {
    const item = document.createElement("li");
    item.textContent = change.change + " · " + change.path;
    files.append(item);
  }
  const commands = $("coding-commands");
  commands.replaceChildren();
  for (const command of result.commands || []) {
    const item = document.createElement("li");
    item.className = command.exit_code === 0 ? "ok" : "bad";
    item.textContent =
      (command.argv || []).join(" ") +
      " → " +
      command.status +
      " (exit " +
      command.exit_code +
      ")";
    commands.append(item);
  }
  $("coding-diff").textContent = result.diff || "(无差异)";
  $("coding-protocol").textContent =
    "工作区 " +
    (result.workspace_path || "-") +
    " · 输出 " +
    result.output_bytes +
    " B · " +
    (result.live_model ? "LIVE 模型" : "本地确定性适配器（live 门 NOT_RUN）");
  const patch = $("coding-patch");
  if (result.patch_url) {
    patch.href = result.patch_url;
    patch.hidden = false;
  } else {
    patch.hidden = true;
  }
}
function startAgentUI() {
  const contract = document.querySelector('meta[name="reposage-ui-contract"]')?.content;
  const required = ["send", "send-icon", "send-label", "question", "token-budget-mode",
    "token-budget-help", "context-mode", "app-shell", "auth-screen", "notice"];
  window.RepoSageUIReady = contract === "reposage-ui-v2" && required.every((id) => $(id));
  if (!window.RepoSageUIReady) {
    // A stale document must never bind a new controller to missing elements.
    // Keep the document and unsent text available behind a blocking native dialog.
    const dialog = element("dialog", "page-reload-dialog");
    dialog.id = "page-reload-dialog";
    dialog.setAttribute("aria-labelledby", "page-reload-title");
    const title = element("h2", "", "页面已更新");
    title.id = "page-reload-title";
    const message = element("p", "", "当前页面与界面脚本版本不一致，请刷新后继续。服务器中的分析和历史记录不受影响。");
    const refresh = element("button", "send-button", "刷新页面");
    refresh.type = "button";
    refresh.addEventListener("click", () => location.reload());
    dialog.addEventListener("cancel", (event) => event.preventDefault());
    dialog.append(title, message, refresh);
    document.body.append(dialog);
    dialog.showModal();
    return;
  }
  bindAgentUI();
  $("mode-analysis").addEventListener("click", () => setCodingMode("analysis"));
  $("mode-coding").addEventListener("click", () => setCodingMode("coding"));
  $("coding-form").addEventListener("submit", submitCoding);
  $("coding-refresh").addEventListener("click", () =>
    loadCodingProjects().catch((error) => setCodingError(explain(error))),
  );
  $("coding-register").addEventListener("click", registerCodingProject);
  $("coding-pause").addEventListener("click", () => controlCoding("pause"));
  $("coding-resume").addEventListener("click", () => controlCoding("resume"));
  $("coding-cancel").addEventListener("click", () => controlCoding("cancel"));
  $("coding-send").addEventListener("click", sendCodingMessage);
  $("coding-answer-send").addEventListener("click", sendCodingAnswer);
  updateMode();
  save("mgar-token", ""); // Migrate away from browser-stored operator tokens.
  (async () => {
    try {
      const session = await call("GET", "/api/v3/auth/session");
      configureLogin(session.setup_required);
      if (session.authenticated) await enterApp(session);
    } catch (error) {
      $("login-error").textContent = explain(error);
    }
  })();

}
startAgentUI();
