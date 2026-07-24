const state = {
  snapshot: null,
  listMode: "tasks",
  selectedType: null,
  selectedId: null,
  detail: null,
  detailTab: "overview",
  detailFetchKey: null,
  controlToken: null,
};

const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
}[char]));

function formatSeconds(value) {
  if (value == null || Number.isNaN(Number(value))) return "-";
  const seconds = Number(value);
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function relativeTime(value) {
  if (!value) return "-";
  const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function statusClass(value) {
  return String(value || "unknown").replace(/[^a-z_]/g, "");
}

function showToast(message, kind = "success") {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast ${kind}`;
  clearTimeout(showToast.timeout);
  showToast.timeout = setTimeout(() => toast.classList.add("hidden"), 5000);
}

function confirmButton(button, confirmationText) {
  if (button.dataset.confirmPending === "true") {
    clearTimeout(button.confirmTimeout);
    button.dataset.confirmPending = "false";
    button.textContent = button.dataset.originalText || button.textContent;
    return true;
  }
  button.dataset.originalText = button.textContent;
  button.dataset.confirmPending = "true";
  button.textContent = confirmationText;
  button.confirmTimeout = setTimeout(() => {
    button.dataset.confirmPending = "false";
    button.textContent = button.dataset.originalText;
  }, 5000);
  return false;
}

async function controlRequest(path, body = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Lean-Agent-Token": state.controlToken || "",
    },
    body: JSON.stringify(body),
  });
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
  return value;
}

function renderSnapshot(snapshot) {
  state.snapshot = snapshot;
  state.controlToken = snapshot.control_token || state.controlToken;
  $("projectPath").textContent = snapshot.project;
  const counts = snapshot.counts || {};
  const activeStates = ["planning", "proving", "lean_checking", "reviewing", "auditing", "explaining"];
  $("countActive").textContent = activeStates.reduce((sum, key) => sum + (counts[key] || 0), 0);
  $("countQueued").textContent = (counts.queued || 0) + (counts.blocked || 0);
  $("countSucceeded").textContent = counts.succeeded || 0;
  $("countFailed").textContent = (counts.failed || 0) + (counts.cancelled || 0);
  const active = snapshot.active?.[0];
  const worker = snapshot.worker || {};
  $("activeProcess").textContent = active
    ? `${active.state} / ${active.activity_text || active.active_kind || "process"} / PID ${active.active_pid || active.worker_pid || "pending"}`
    : worker.running ? `queue worker / PID ${worker.pid}` : "Idle";
  const startButton = $("startWorkerButton");
  startButton.disabled = Boolean(worker.running || active);
  if (startButton.dataset.confirmPending !== "true") {
    startButton.textContent = worker.running ? `队列运行中 · PID ${worker.pid}` : "启动队列";
  }
  renderList();
  renderTaskOptions();
  refreshSelectedDetail(snapshot);
}

function renderTaskOptions() {
  const files = state.snapshot?.lean_files || [];
  $("leanFiles").innerHTML = files.map((file) => `<option value="${escapeHtml(file)}"></option>`).join("");
  const tasks = state.snapshot?.tasks || [];
  $("taskDependencies").innerHTML = tasks.map((task) =>
    `<option value="${escapeHtml(task.id)}">${escapeHtml(task.id)} · ${escapeHtml(task.target_file)} · ${escapeHtml(task.state)}</option>`
  ).join("");
  renderProviderOptions();
}

function providerIds() {
  return Object.keys(state.snapshot?.providers || { default: {} });
}

function providerOptions(selected = "default") {
  return providerIds().map((providerId) =>
    `<option value="${escapeHtml(providerId)}"${providerId === selected ? " selected" : ""}>${escapeHtml(providerId)}</option>`
  ).join("");
}

function renderProviderOptions() {
  const taskProvider = $("taskProvider");
  const selected = taskProvider.value || "default";
  taskProvider.innerHTML = providerOptions(selected);
}

function laneTemplate(index, lane = {}) {
  const config = state.snapshot?.configuration || {};
  const effort = config.reasoning_effort || "medium";
  const optionSet = (selected) => ["low", "medium", "high", "xhigh"].map((value) =>
    `<option${value === selected ? " selected" : ""}>${value}</option>`
  ).join("");
  return `<div class="lane-row" data-lane-index="${index}">
    <div class="lane-row-header"><strong>Lane ${index + 1}</strong><button class="icon-button remove-lane" type="button" title="删除路线" aria-label="删除路线">&times;</button></div>
    <div class="lane-fields">
      <label class="field"><span>Provider</span><select class="lane-provider">${providerOptions(lane.provider || "default")}</select></label>
      <label class="field lane-model"><span>模型</span><input class="lane-model-input" value="${escapeHtml(lane.model || "")}" placeholder="留空使用 Provider 默认模型"></label>
      <label class="field lane-prompt"><span>路线提示词</span><textarea class="lane-prompt-input" rows="3" placeholder="例如：优先使用代数化简和 nlinarith。">${escapeHtml(lane.prompt || "")}</textarea></label>
      <label class="field"><span>Plan</span><select class="lane-plan-effort">${optionSet(lane.plan_effort || effort)}</select></label>
      <label class="field"><span>Prove</span><select class="lane-prove-effort">${optionSet(lane.prove_effort || effort)}</select></label>
      <label class="field"><span>Review</span><select class="lane-review-effort">${optionSet(lane.review_effort || effort)}</select></label>
    </div>
  </div>`;
}

function renderLanes(lanes = null) {
  const profiles = providerIds();
  const initial = lanes || [
    { id: "lane-1", provider: "default" },
    { id: "lane-2", provider: profiles.find((value) => value !== "default") || "default" },
  ];
  $("laneList").innerHTML = initial.map((lane, index) => laneTemplate(index, lane)).join("");
}

function collectLanes() {
  return Array.from(document.querySelectorAll(".lane-row"), (row, index) => ({
    id: `lane-${index + 1}`,
    provider: row.querySelector(".lane-provider").value,
    model: row.querySelector(".lane-model-input").value.trim(),
    prompt: row.querySelector(".lane-prompt-input").value.trim(),
    plan_effort: row.querySelector(".lane-plan-effort").value,
    prove_effort: row.querySelector(".lane-prove-effort").value,
    review_effort: row.querySelector(".lane-review-effort").value,
  }));
}

function populateTaskDefaults() {
  const config = state.snapshot?.configuration || {};
  $("taskModel").value = "";
  $("taskProvider").innerHTML = providerOptions("default");
  $("taskModel").placeholder = config.model
    ? `留空使用 ${config.model}`
    : "留空使用项目默认模型";
  $("apiTimeout").value = config.timeout_seconds || 180;
  $("apiRetries").value = config.api_timeout_retries ?? 1;
  const effort = config.reasoning_effort || "medium";
  $("planEffort").value = effort;
  $("proveEffort").value = effort;
  $("reviewEffort").value = effort;
  $("multiProver").checked = false;
  $("laneEditor").classList.add("hidden");
  renderLanes();
}

function renderList() {
  const items = state.listMode === "tasks" ? state.snapshot?.tasks || [] : state.snapshot?.workflows || [];
  $("emptyList").classList.toggle("hidden", items.length > 0);
  $("itemList").innerHTML = items.map((item) => {
    const isTask = state.listMode === "tasks";
    const id = isTask ? item.id : item.run_id;
    const name = item.target_file || "Untitled";
    const status = isTask ? item.state : item.status;
    const attempts = isTask ? item.attempt : item.attempt_count;
    const selected = state.selectedType === (isTask ? "task" : "workflow") && state.selectedId === id;
    return `<button class="list-item${selected ? " selected" : ""}" data-type="${isTask ? "task" : "workflow"}" data-id="${escapeHtml(id)}">
      <span class="item-name"><strong>${escapeHtml(name)}</strong><small>${escapeHtml(id)}</small></span>
      <span class="status ${statusClass(status)}">${escapeHtml(status)}</span>
      <span class="item-attempt">${attempts ?? "-"}</span>
      <span class="item-time">${relativeTime(item.updated_at)}</span>
    </button>`;
  }).join("");
  document.querySelectorAll(".list-item").forEach((button) => {
    button.addEventListener("click", () => selectItem(button.dataset.type, button.dataset.id));
  });
}

async function selectItem(type, id) {
  state.selectedType = type;
  state.selectedId = id;
  state.detail = null;
  state.detailFetchKey = null;
  location.hash = `${type}:${id}`;
  renderList();
  await fetchDetail(true);
}

function refreshSelectedDetail(snapshot) {
  if (!state.selectedId) return;
  const collection = state.selectedType === "task" ? snapshot.tasks : snapshot.workflows;
  const item = collection?.find((row) => (state.selectedType === "task" ? row.id : row.run_id) === state.selectedId);
  if (!item) return;
  const key = `${item.updated_at}|${item.state || item.status}|${item.active_pid || ""}|${item.race_updated_at || ""}`;
  if (key !== state.detailFetchKey) fetchDetail(false, key);
}

async function fetchDetail(force = false, key = null) {
  if (!state.selectedId) return;
  try {
    const resource = state.selectedType === "task" ? "tasks" : "workflows";
    const response = await fetch(`/api/${resource}/${encodeURIComponent(state.selectedId)}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.detail = await response.json();
    state.detailFetchKey = key || `${Date.now()}`;
    renderDetail();
  } catch (error) {
    if (force) showDetailError(error.message);
  }
}

function workflowData() {
  return state.selectedType === "task" ? state.detail?.workflow : state.detail;
}

function renderDetail() {
  const task = state.selectedType === "task" ? state.detail?.task : null;
  const workflow = workflowData();
  const manifest = workflow?.manifest || {};
  const status = task?.state || manifest.status || "unknown";
  $("detailEmpty").classList.add("hidden");
  $("detailContent").classList.remove("hidden");
  $("detailId").textContent = task ? `TASK ${task.id}` : `WORKFLOW ${manifest.run_id || state.selectedId}`;
  $("detailTitle").textContent = task?.target_file || manifest.target_file || "Workflow";
  $("detailStatus").textContent = status;
  $("detailStatus").className = `status-badge status ${statusClass(status)}`;
  $("cancelTaskButton").classList.toggle("hidden", !task || ["succeeded", "failed", "cancelled"].includes(status));
  $("retryTaskButton").classList.toggle("hidden", !task || !["failed", "cancelled"].includes(status));
  const meta = [];
  if (task?.attempt != null) meta.push(`Attempt ${task.attempt}`);
  if (task?.active_pid) meta.push(`${task.active_kind || "process"} PID ${task.active_pid}`);
  if (task?.activity_text) meta.push(task.activity_text);
  if (manifest.run_id) meta.push(manifest.run_id);
  if (workflow?.timings?.total_seconds != null) meta.push(`Total ${formatSeconds(workflow.timings.total_seconds)}`);
  $("detailMeta").innerHTML = meta.map((item) => `<span>${escapeHtml(item)}</span>`).join("");
  renderTab();
}

function showDetailError(message) {
  $("detailEmpty").classList.add("hidden");
  $("detailContent").classList.remove("hidden");
  $("detailTitle").textContent = "无法读取详情";
  $("tabBody").innerHTML = `<div class="notice">${escapeHtml(message)}</div>`;
}

function latestAttempt(workflow) {
  const attempts = workflow?.attempts || [];
  return attempts.length ? attempts[attempts.length - 1] : null;
}

function kv(label, value) {
  return `<div class="kv"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? "-")}</strong></div>`;
}

function renderRaceLane(lane) {
  const workflow = lane.workflow || {};
  const currentStep = workflow.current_step;
  const step = currentStep ? `${currentStep.index || "-"} / ${currentStep.id || "-"}` : "-";
  const latestCheck = workflow.latest_check;
  const error = workflow.error || lane.error;
  const model = lane.model || workflow.models?.prove || "provider default";
  return `<article class="race-lane">
    <header><div><strong>${escapeHtml(lane.id)}</strong><span>${escapeHtml(lane.provider || "default")} / ${escapeHtml(model)}</span></div><span class="status ${statusClass(lane.status)}">${escapeHtml(lane.status)}</span></header>
    <div class="race-lane-grid">
      ${kv("Phase", workflow.phase || lane.phase)}${kv("Activity", lane.activity || "-")}
      ${kv("Plan step", step)}${kv("Lean candidates", `${workflow.attempt_count ?? lane.attempt ?? 0} / ${workflow.max_attempts ?? "-"}`)}
      ${kv("Lean check", latestCheck?.ok == null ? "-" : latestCheck.ok ? "passed" : "failed")}${kv("Elapsed", formatSeconds(workflow.total_seconds))}
      ${kv("Process", lane.active_pid ? `${lane.active_kind || "process"} / PID ${lane.active_pid}` : "Idle")}${kv("Run ID", lane.run_id || "-")}
    </div>
    ${error ? `<div class="lane-error">${escapeHtml(error)}</div>` : ""}
  </article>`;
}

function renderOverview(task, workflow, race) {
  const manifest = workflow?.manifest || {};
  const attempt = latestAttempt(workflow);
  return `<section class="section"><h3>运行状态</h3><div class="kv-grid">
    ${kv("任务", manifest.task || task?.task_text)}${kv("阶段", task?.state || manifest.phase)}
    ${kv("模型", task?.settings?.model || manifest.settings?.models?.prove || "environment default")}${kv("最大尝试", task?.settings?.max_attempts || manifest.settings?.max_attempts)}
    ${kv("尝试", task?.attempt ?? manifest.completed_attempt)}${kv("当前步骤", manifest.current_step?.id || "-")}
    ${kv("Lean 检查", attempt?.check?.ok == null ? "-" : attempt.check.ok ? "passed" : "failed")}${kv("流式状态", task?.activity_text || "-")}
    ${kv("活动进程", task?.active_pid ? `${task.active_kind} / PID ${task.active_pid}` : "Idle")}${kv("自然语言证明", manifest.explanation_status || "not requested")}
    ${kv("目标形式化", manifest.formal_goal?.validated == null ? "not used" : manifest.formal_goal.validated ? "validated" : "invalid")}${kv("Import 策略", manifest.settings?.effective_import_policy || task?.settings?.import_policy || "auto")}
  </div></section>
  ${manifest.error || task?.error ? `<section class="section"><h3>错误</h3><div class="notice">${escapeHtml(manifest.error || task.error)}</div></section>` : ""}
  <section class="section"><h3>结果</h3><div class="kv-grid">
    ${kv("Workflow", manifest.run_id)}${kv("总耗时", formatSeconds(workflow?.timings?.total_seconds))}
    ${kv("Agent Protocol", `${state.snapshot?.agent_protocol?.protocol || "-"}/v${state.snapshot?.agent_protocol?.protocol_version || "-"}`)}
    ${kv("候选 SHA", attempt?.candidate_sha256)}${kv("Review", attempt?.review_verdict)}
  </div></section>
  ${race ? `<section class="section"><h3>Prover Race</h3><div class="kv-grid">
    ${kv("Race", race.race_id)}${kv("策略", race.strategy)}${kv("状态", race.status)}${kv("胜者", race.winner_lane_id || "-")}
  </div><div class="race-lanes">${(race.lanes || []).map(renderRaceLane).join("")}</div></section>` : ""}`;
}

function renderMarkdown(markdown) {
  if (!markdown) return `<div class="notice">尚未生成自然语言证明。</div>`;
  const lines = markdown.split(/\r?\n/);
  let html = "", inList = false;
  const closeList = () => { if (inList) { html += "</ul>"; inList = false; } };
  for (const raw of lines) {
    const line = escapeHtml(raw).replace(/`([^`]+)`/g, "<code>$1</code>");
    if (line.startsWith("# ")) { closeList(); html += `<h1>${line.slice(2)}</h1>`; }
    else if (line.startsWith("## ")) { closeList(); html += `<h2>${line.slice(3)}</h2>`; }
    else if (line.startsWith("### ")) { closeList(); html += `<h3>${line.slice(4)}</h3>`; }
    else if (/^- /.test(line)) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${line.slice(2)}</li>`; }
    else if (line.trim()) { closeList(); html += `<p>${line}</p>`; }
  }
  closeList();
  return html;
}

function renderProof(workflow) {
  const attempt = latestAttempt(workflow);
  return `<section class="section"><h3>Lean 候选证明</h3><pre>${escapeHtml(attempt?.candidate || workflow?.original || "No proof artifact")}</pre></section>
    <section class="section"><h3>自然语言证明</h3><div class="markdown">${renderMarkdown(workflow?.explanation)}</div></section>`;
}

function renderDiagnostics(workflow) {
  const attempts = workflow?.attempts || [];
  if (!attempts.length) return `<div class="notice">暂无 Lean 检查记录。</div>`;
  return attempts.slice().reverse().map((attempt) => `<section class="section"><h3>Attempt ${attempt.attempt} / ${attempt.check?.ok ? "passed" : "failed"}</h3>
    <pre class="${attempt.check?.ok ? "" : "error-pre"}">${escapeHtml(attempt.check?.output || "Lean check passed with no output.")}</pre></section>`).join("");
}

function renderReview(workflow) {
  const attempt = latestAttempt(workflow);
  return `<section class="section"><h3>正式 Lean 目标</h3><pre>${escapeHtml(JSON.stringify(workflow?.goal || workflow?.manifest?.formal_goal || {}, null, 2))}</pre></section>
    <section class="section"><h3>目标声明检查</h3><pre>${escapeHtml(JSON.stringify(workflow?.formal_goal_check || {}, null, 2))}</pre></section>
    <section class="section"><h3>Plan 前检索</h3><pre>${escapeHtml(JSON.stringify(workflow?.planning_retrieval || {}, null, 2))}</pre></section>
    <section class="section"><h3>Plan</h3><pre>${escapeHtml(JSON.stringify(workflow?.plan || {}, null, 2))}</pre></section>
    <section class="section"><h3>步骤检查点</h3><pre>${escapeHtml(JSON.stringify(workflow?.checkpoints || workflow?.manifest?.steps || [], null, 2))}</pre></section>
    <section class="section"><h3>Global Final Audit</h3><pre>${escapeHtml(JSON.stringify(workflow?.final_audit || workflow?.manifest?.final_audit || {}, null, 2))}</pre></section>
    <section class="section"><h3>Final Review</h3><pre>${escapeHtml(JSON.stringify(attempt?.review || workflow?.manifest?.final_review || {}, null, 2))}</pre></section>
    <section class="section"><h3>Agent Calls</h3><pre>${escapeHtml(JSON.stringify(workflow?.agent_calls || [], null, 2))}</pre></section>
    <section class="section"><h3>检索证据与 import 建议</h3><pre>${escapeHtml(JSON.stringify(attempt?.retrieval || workflow?.initial_retrieval || {}, null, 2))}</pre></section>`;
}

function renderTiming(workflow) {
  const timings = workflow?.timings;
  if (!timings) return `<div class="notice">该 workflow 没有耗时记录。</div>`;
  const entries = Object.entries(timings.phase_seconds || {});
  const max = Math.max(0.001, ...entries.map(([, value]) => Number(value)));
  return `<section class="section"><h3>阶段耗时 / 总计 ${formatSeconds(timings.total_seconds)}</h3>
    ${entries.map(([phase, value]) => `<div class="timing-row"><span class="timing-label">${escapeHtml(phase)}</span><progress class="timing-track" max="${max}" value="${Number(value)}"></progress><span class="timing-value">${formatSeconds(value)}</span></div>`).join("")}
  </section><section class="section"><h3>完整记录</h3><pre>${escapeHtml(JSON.stringify(timings.spans || [], null, 2))}</pre></section>`;
}

function renderEvents(task, workflow) {
  if (task?.events?.length) return `<div class="timeline">${task.events.map((event) => `<div class="timeline-row"><strong>${escapeHtml(event.event)}</strong><span>${escapeHtml(event.timestamp)}</span><span>${escapeHtml(JSON.stringify(event.details || {}))}</span></div>`).join("")}</div>`;
  const manifest = workflow?.manifest || {};
  return `<div class="timeline"><div class="timeline-row"><strong>workflow_created</strong><span>${escapeHtml(manifest.created_at)}</span></div><div class="timeline-row"><strong>${escapeHtml(manifest.status)}</strong><span>${escapeHtml(manifest.updated_at)}</span></div></div>`;
}

function renderTab() {
  if (!state.detail) return;
  const task = state.selectedType === "task" ? state.detail.task : null;
  const workflow = workflowData();
  const race = state.selectedType === "task" ? state.detail?.race : null;
  const renderers = {
    overview: () => renderOverview(task, workflow, race), proof: () => renderProof(workflow),
    diagnostics: () => renderDiagnostics(workflow), review: () => renderReview(workflow),
    timing: () => renderTiming(workflow), events: () => renderEvents(task, workflow),
  };
  $("tabBody").innerHTML = renderers[state.detailTab]();
}

async function addTask(event) {
  event.preventDefault();
  const submit = $("submitTaskButton");
  submit.disabled = true;
  try {
    const dependencies = Array.from($("taskDependencies").selectedOptions, (option) => option.value);
    const apiTimeout = $("apiTimeout").value.trim();
    const result = await controlRequest("/api/tasks", {
      target_file: $("taskFile").value.trim(),
      task_text: $("taskText").value.trim(),
      dependencies,
      settings: {
        provider: $("taskProvider").value || "default",
        model: $("taskModel").value.trim(),
        max_attempts: Number($("maxAttempts").value),
        max_attempts_per_step: Number($("maxAttemptsPerStep").value),
        lean_timeout: Number($("leanTimeout").value),
        api_timeout: apiTimeout ? Number(apiTimeout) : null,
        api_retries: Number($("apiRetries").value),
        import_policy: $("importPolicy").value,
        plan_effort: $("planEffort").value, prove_effort: $("proveEffort").value,
        review_effort: $("reviewEffort").value, explain: $("explainProof").checked,
        protect_existing_statements: $("protectExistingStatements").checked,
        formalize_goal: $("formalizeGoal").checked,
        protected_declarations: $("protectedDeclarations").value.split(",").map((value) => value.trim()).filter(Boolean),
        keep_failed: $("keepFailed").checked,
        race: $("multiProver").checked ? {
          strategy: "first_verified_wins",
          lanes: collectLanes(),
        } : null,
      },
    });
    $("taskDialog").close();
    $("taskForm").reset();
    showToast(`任务 ${result.task.id} 已加入队列${result.target_created ? `，已创建 ${result.task.target_file}` : ""}。`);
    await selectItem("task", result.task.id);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
}

async function taskAction(action) {
  if (!state.selectedId || state.selectedType !== "task") return;
  const button = action === "cancel" ? $("cancelTaskButton") : $("retryTaskButton");
  if (action === "cancel" && !confirmButton(button, "再次点击确认取消")) return;
  try {
    const result = await controlRequest(`/api/tasks/${encodeURIComponent(state.selectedId)}/${action}`);
    showToast(action === "cancel" ? `任务 ${result.task.id} 已请求取消。` : `任务 ${result.task.id} 已重新排队。`);
    state.detailFetchKey = null;
    await fetchDetail(true);
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function startWorker() {
  const queued = (state.snapshot?.counts?.queued || 0);
  if (!queued) { showToast("当前没有等待运行的任务。", "error"); return; }
  const button = $("startWorkerButton");
  if (!confirmButton(button, `再次点击启动 ${queued} 个任务`)) return;
  button.disabled = true;
  try {
    const result = await controlRequest("/api/queue/start");
    showToast(`队列 worker 已启动，PID ${result.worker.pid}。`);
  } catch (error) {
    showToast(error.message, "error");
    button.disabled = false;
  }
}

function populateConfigForm(providerId = "default") {
  const providers = state.snapshot?.providers || { default: state.snapshot?.configuration || {} };
  const isNew = providerId === "__new__";
  const config = isNew ? {} : providers[providerId] || {};
  $("configProviderSelect").innerHTML = [
    ...Object.keys(providers).map((id) => `<option value="${escapeHtml(id)}"${id === providerId ? " selected" : ""}>${escapeHtml(id)}</option>`),
    `<option value="__new__"${isNew ? " selected" : ""}>+ 新建 Provider</option>`,
  ].join("");
  $("configProviderId").value = isNew ? "" : providerId;
  $("configProviderId").readOnly = !isNew;
  $("configProviderKind").value = config.provider_kind || "openai-compatible";
  $("configApiBase").value = config.api_base || "";
  $("configModel").value = config.model || "";
  $("configApiMode").value = config.api_mode || "responses";
  $("configApiTransport").value = config.api_transport || "auto";
  $("configReasoning").value = config.reasoning_effort || "medium";
  $("configTimeout").value = config.timeout_seconds || 180;
  $("configRetries").value = config.api_timeout_retries ?? 1;
  $("configMaxOutput").value = config.max_output_tokens || 8192;
  $("configLake").value = config.lake || "lake";
  $("configApiKey").value = "";
  $("configClearKey").checked = false;
  $("configDisableStorage").checked = config.disable_response_storage !== false;
  $("configStreaming").checked = config.stream_responses !== false;
  const status = $("configKeyStatus");
  status.textContent = config.api_key_configured
    ? `API Key 已配置，来源：${config.api_key_source === "project" ? "项目加密存储" : "环境变量"}`
    : "尚未配置 API Key";
  status.className = `key-status${config.api_key_configured ? " configured" : ""}`;
}

async function saveConfiguration(event) {
  event.preventDefault();
  const button = $("saveConfigButton");
  button.disabled = true;
  try {
    const result = await controlRequest("/api/config", {
      provider_id: $("configProviderId").value.trim() || "default",
      configuration: {
        provider_kind: $("configProviderKind").value,
        api_base: $("configApiBase").value.trim(),
        model: $("configModel").value.trim(),
        api_mode: $("configApiMode").value,
        api_transport: $("configApiTransport").value,
        reasoning_effort: $("configReasoning").value,
        timeout_seconds: Number($("configTimeout").value),
        api_timeout_retries: Number($("configRetries").value),
        max_output_tokens: Number($("configMaxOutput").value),
        lake: $("configLake").value.trim(),
        disable_response_storage: $("configDisableStorage").checked,
        stream_responses: $("configStreaming").checked,
      },
      api_key: $("configApiKey").value || null,
      clear_api_key: $("configClearKey").checked,
    });
    state.snapshot.configuration = result.configuration;
    state.snapshot.providers = result.providers;
    $("configDialog").close();
    showToast("项目运行配置已保存，新启动的 API 请求将使用该配置。");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

document.querySelectorAll(".panel-tab").forEach((button) => button.addEventListener("click", () => {
  state.listMode = button.dataset.list;
  document.querySelectorAll(".panel-tab").forEach((item) => item.classList.toggle("active", item === button));
  renderList();
}));
document.querySelectorAll(".detail-tab").forEach((button) => button.addEventListener("click", () => {
  state.detailTab = button.dataset.tab;
  document.querySelectorAll(".detail-tab").forEach((item) => item.classList.toggle("active", item === button));
  renderTab();
}));
$("addTaskButton").addEventListener("click", () => { renderTaskOptions(); populateTaskDefaults(); $("taskDialog").showModal(); });
$("configButton").addEventListener("click", () => { populateConfigForm(); $("configDialog").showModal(); });
$("closeTaskDialog").addEventListener("click", () => $("taskDialog").close());
$("cancelTaskDialog").addEventListener("click", () => $("taskDialog").close());
$("taskForm").addEventListener("submit", addTask);
$("closeConfigDialog").addEventListener("click", () => $("configDialog").close());
$("cancelConfigDialog").addEventListener("click", () => $("configDialog").close());
$("configForm").addEventListener("submit", saveConfiguration);
$("multiProver").addEventListener("change", () => {
  $("laneEditor").classList.toggle("hidden", !$("multiProver").checked);
  if ($("multiProver").checked && !document.querySelector(".lane-row")) renderLanes();
});
$("addLaneButton").addEventListener("click", () => {
  const lanes = collectLanes();
  if (lanes.length >= 4) { showToast("最多支持 4 条并行路线。", "error"); return; }
  lanes.push({ id: `lane-${lanes.length + 1}`, provider: "default" });
  renderLanes(lanes);
});
$("laneList").addEventListener("click", (event) => {
  const button = event.target.closest(".remove-lane");
  if (!button) return;
  const rows = Array.from(document.querySelectorAll(".lane-row"));
  if (rows.length <= 2) { showToast("多 Prover 至少需要 2 条路线。", "error"); return; }
  const lanes = collectLanes();
  lanes.splice(rows.indexOf(button.closest(".lane-row")), 1);
  renderLanes(lanes);
});
$("configProviderSelect").addEventListener("change", () => {
  populateConfigForm($("configProviderSelect").value);
});
$("configProviderKind").addEventListener("change", () => {
  if ($("configProviderKind").value !== "deepseek") return;
  if (!$("configApiBase").value) $("configApiBase").value = "https://api.deepseek.com";
  if (!$("configModel").value) $("configModel").value = "deepseek-reasoner";
  $("configApiMode").value = "chat-completions";
  $("configStreaming").checked = false;
});
$("startWorkerButton").addEventListener("click", startWorker);
$("cancelTaskButton").addEventListener("click", () => taskAction("cancel"));
$("retryTaskButton").addEventListener("click", () => taskAction("retry"));

async function initialize() {
  try {
    const response = await fetch("/api/snapshot");
    renderSnapshot(await response.json());
    const hash = location.hash.slice(1).split(":");
    if (hash.length === 2) selectItem(hash[0], hash[1]);
  } catch (error) {
    $("connectionState").className = "connection offline";
    $("connectionState").lastChild.textContent = "离线";
  }
  const events = new EventSource("/api/events");
  events.addEventListener("snapshot", (event) => {
    $("connectionState").className = "connection online";
    $("connectionState").lastChild.textContent = "实时";
    renderSnapshot(JSON.parse(event.data));
  });
  events.onerror = () => {
    $("connectionState").className = "connection offline";
    $("connectionState").lastChild.textContent = "重连中";
  };
}

initialize();
