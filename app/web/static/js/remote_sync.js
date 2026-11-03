import { apiFetch } from "./api.js";
import { formatBytes } from "./format.js";

const root = document.getElementById("remote-sync-root");
const canWrite = root.dataset.canWrite === "true";
const banner = document.getElementById("error-banner");
const infoBanner = document.getElementById("info-banner");
const workerBadge = document.getElementById("worker-badge");
const connectionsBody = document.getElementById("connections-body");
const jobsBody = document.getElementById("jobs-body");

const state = { providers: [], connections: [], jobs: [], status: null, historyJob: null };

function showError(message) {
  banner.textContent = message;
  banner.classList.remove("d-none");
}

function hideError() {
  banner.classList.add("d-none");
}

function showInfo(message) {
  infoBanner.textContent = message;
  infoBanner.classList.remove("d-none");
}

function cell(text, className = "") {
  const td = document.createElement("td");
  if (className) td.className = className;
  td.textContent = text;
  return td;
}

function badge(text, kind) {
  const span = document.createElement("span");
  span.className = `badge text-bg-${kind}`;
  span.textContent = text;
  return span;
}

function button(label, kind, handler) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `btn btn-sm btn-outline-${kind} ms-1`;
  btn.textContent = label;
  btn.addEventListener("click", () => {
    hideError();
    handler().catch((error) => showError(error.message));
  });
  return btn;
}

function when(value) {
  return value ? new Date(`${value}${value.endsWith("Z") ? "" : "Z"}`).toLocaleString() : "–";
}

const STATUS_KIND = {
  succeeded: "success",
  partial: "warning",
  failed: "danger",
  cancelled: "secondary",
  running: "primary",
  queued: "info",
};

// --------------------------------------------------------------------------
// Dialogs
// --------------------------------------------------------------------------

function confirmDialog(title, planLines, warnings, action) {
  document.getElementById("confirm-modal-title").textContent = title;
  const plan = document.getElementById("confirm-modal-plan");
  plan.replaceChildren();
  for (const line of planLines) {
    const p = document.createElement("p");
    p.className = "mb-1";
    p.textContent = line;
    plan.append(p);
  }
  const warningList = document.getElementById("confirm-modal-warnings");
  warningList.replaceChildren();
  for (const warning of warnings) {
    const li = document.createElement("li");
    li.textContent = warning;
    warningList.append(li);
  }
  const accept = document.getElementById("confirm-modal-accept");
  const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById("confirm-modal"));
  accept.onclick = async () => {
    modal.hide();
    try {
      await action();
    } catch (error) {
      showError(error.message);
    }
  };
  modal.show();
}

/** Send a request; if the server asks for confirmation, show its warnings first. */
async function withConfirmation(title, send) {
  try {
    return await send(false);
  } catch (error) {
    if (error.code !== "confirmation_required") throw error;
    const warnings = (error.details && error.details.warnings) || [];
    return new Promise((resolve) => {
      confirmDialog(title, [error.message], warnings, async () => resolve(await send(true)));
    });
  }
}

// --------------------------------------------------------------------------
// Status
// --------------------------------------------------------------------------

function renderStatus() {
  const status = state.status;
  workerBadge.className = `badge text-bg-${status.worker_online ? "success" : "danger"}`;
  workerBadge.textContent = status.worker_online
    ? `Worker online · ${status.running_runs} running · ${status.queued_runs} queued`
    : "Worker offline: runs stay queued until it starts (python -m app.worker)";
}

// --------------------------------------------------------------------------
// Connections
// --------------------------------------------------------------------------

function providerSpec(name) {
  return state.providers.find((p) => p.name === name);
}

function renderConnections() {
  connectionsBody.replaceChildren();
  if (state.connections.length === 0) {
    const tr = document.createElement("tr");
    tr.append(cell("No connections yet.", "text-body-secondary"));
    tr.firstChild.colSpan = 6;
    connectionsBody.append(tr);
    return;
  }
  for (const conn of state.connections) {
    const tr = document.createElement("tr");
    tr.append(cell(conn.name));
    const spec = providerSpec(conn.provider);
    tr.append(cell(spec ? spec.label : conn.provider));
    const creds = document.createElement("td");
    if (!conn.credentials_readable) {
      creds.append(badge("cannot decrypt", "danger"));
    } else {
      creds.textContent = conn.secrets_set.length ? conn.secrets_set.join(", ") : "none";
      if (conn.oauth_connected === false) creds.append(" ", badge("not authorized", "warning"));
    }
    tr.append(creds);
    const test = document.createElement("td");
    if (conn.last_tested_at) {
      test.append(badge(conn.last_test_ok ? "ok" : "failed", conn.last_test_ok ? "success" : "danger"));
      test.title = conn.last_test_message || "";
      test.append(` ${when(conn.last_tested_at)}`);
    } else {
      test.textContent = "never";
    }
    tr.append(test);
    tr.append(cell(String(conn.job_count)));
    if (canWrite) {
      const actions = document.createElement("td");
      actions.className = "text-end text-nowrap";
      actions.append(button("Test", "secondary", () => testConnection(conn)));
      if (conn.provider === "gdrive") {
        actions.append(button("Connect", "primary", () => startOAuth(conn)));
      }
      actions.append(button("Edit", "secondary", async () => openConnectionModal(conn)));
      actions.append(button("Delete", "danger", () => deleteConnection(conn)));
      tr.append(actions);
    }
    connectionsBody.append(tr);
  }
}

async function testConnection(conn) {
  const result = await apiFetch(`/api/v1/remotes/${conn.id}/test`, { method: "POST" });
  if (result.ok) showInfo(`${conn.name}: ${result.message}`);
  else showError(`${conn.name}: ${result.message}`);
  await loadConnections();
}

async function startOAuth(conn) {
  const result = await apiFetch(`/api/v1/remotes/${conn.id}/oauth/start`, { method: "POST" });
  window.location.assign(result.authorization_url);
}

async function deleteConnection(conn) {
  confirmDialog(`Delete ${conn.name}?`, ["The stored credentials are removed. Remote data is not touched."], [], async () => {
    await apiFetch(`/api/v1/remotes/${conn.id}`, { method: "DELETE" });
    await loadConnections();
  });
}

let editingConnection = null;

function fieldInput(field, conn) {
  const col = document.createElement("div");
  col.className = field.kind === "textarea" || field.name === "private_key" || field.name === "host_key" ? "col-12" : "col-md-6";
  const id = `cf-${field.name}`;
  let input;
  if (field.kind === "bool") {
    const wrap = document.createElement("div");
    wrap.className = "form-check mt-4";
    input = document.createElement("input");
    input.type = "checkbox";
    input.className = "form-check-input";
    input.id = id;
    input.checked = Boolean(conn ? conn.config[field.name] : field.default);
    const label = document.createElement("label");
    label.className = "form-check-label";
    label.htmlFor = id;
    label.textContent = field.label;
    wrap.append(input, label);
    col.append(wrap);
  } else {
    const label = document.createElement("label");
    label.className = "form-label";
    label.htmlFor = id;
    label.textContent = field.label + (field.required ? " *" : "");
    if (field.kind === "choice") {
      input = document.createElement("select");
      input.className = "form-select";
      for (const choice of field.choices) {
        const option = document.createElement("option");
        option.value = choice;
        option.textContent = choice;
        input.append(option);
      }
    } else if (field.name === "private_key") {
      input = document.createElement("textarea");
      input.className = "form-control font-monospace";
      input.rows = 4;
    } else {
      input = document.createElement("input");
      input.className = "form-control";
      input.type = field.kind === "int" ? "number" : field.secret ? "password" : "text";
      if (field.secret) input.autocomplete = "new-password";
    }
    input.id = id;
    input.spellcheck = false;
    if (field.secret) {
      input.placeholder = conn && conn.secrets_set.includes(field.name) ? "•••••• (stored; leave empty to keep)" : "";
    } else {
      const value = conn ? conn.config[field.name] : field.default;
      if (value !== undefined && value !== null) input.value = String(value);
    }
    col.append(label, input);
    if (field.name === "host_key") {
      const probe = document.createElement("button");
      probe.type = "button";
      probe.className = "btn btn-sm btn-outline-secondary mt-2";
      probe.textContent = "Fetch host key";
      probe.addEventListener("click", () => probeHostKey(input).catch((error) => showError(error.message)));
      col.append(probe);
    }
  }
  input.dataset.field = field.name;
  input.dataset.kind = field.kind;
  if (field.help) {
    const help = document.createElement("div");
    help.className = "form-text";
    help.textContent = field.help;
    col.append(help);
  }
  return col;
}

function renderConnectionFields(providerName, conn) {
  const container = document.getElementById("connection-fields");
  container.replaceChildren();
  const spec = providerSpec(providerName);
  if (!spec) return;
  for (const field of spec.fields) container.append(fieldInput(field, conn));
}

async function probeHostKey(target) {
  const host = document.getElementById("cf-host").value.trim();
  const port = Number(document.getElementById("cf-port").value || 22);
  const key = await apiFetch("/api/v1/remotes/sftp/host-key", { method: "POST", body: { host, port } });
  const modal = bootstrap.Modal.getInstance(document.getElementById("connection-modal"));
  modal.hide();
  confirmDialog(
    "Trust this SSH host key?",
    [`${key.host}:${key.port} presented a ${key.key_type} key.`, `Fingerprint: ${key.fingerprint}`],
    ["Compare the fingerprint with the server (ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub) before trusting it."],
    async () => {
      target.value = key.host_key;
      modal.show();
    },
  );
}

function openConnectionModal(conn) {
  editingConnection = conn;
  document.getElementById("connection-modal-title").textContent = conn ? `Edit ${conn.name}` : "Add connection";
  document.getElementById("connection-name").value = conn ? conn.name : "";
  const select = document.getElementById("connection-provider");
  select.replaceChildren();
  for (const spec of state.providers) {
    const option = document.createElement("option");
    option.value = spec.name;
    option.textContent = spec.label;
    select.append(option);
  }
  select.value = conn ? conn.provider : "sftp";
  select.disabled = Boolean(conn);
  renderConnectionFields(select.value, conn);
  bootstrap.Modal.getOrCreateInstance(document.getElementById("connection-modal")).show();
}

function collectConnection() {
  const config = {};
  const secrets = {};
  for (const input of document.querySelectorAll("#connection-fields [data-field]")) {
    const name = input.dataset.field;
    const kind = input.dataset.kind;
    if (kind === "bool") config[name] = input.checked;
    else if (kind === "secret") {
      if (input.value) secrets[name] = input.value;
    } else if (kind === "int") {
      if (input.value !== "") config[name] = Number(input.value);
    } else config[name] = input.value.trim();
  }
  return { config, secrets };
}

async function saveConnection(event) {
  event.preventDefault();
  hideError();
  const { config, secrets } = collectConnection();
  const name = document.getElementById("connection-name").value.trim();
  try {
    if (editingConnection) {
      await apiFetch(`/api/v1/remotes/${editingConnection.id}`, { method: "PATCH", body: { name, config, secrets } });
    } else {
      const provider = document.getElementById("connection-provider").value;
      await apiFetch("/api/v1/remotes", { method: "POST", body: { name, provider, config, secrets } });
    }
    bootstrap.Modal.getInstance(document.getElementById("connection-modal")).hide();
    await loadConnections();
  } catch (error) {
    showError(error.message);
  }
}

// --------------------------------------------------------------------------
// Jobs
// --------------------------------------------------------------------------

const DIRECTION_LABEL = { push: "Push →", pull: "← Pull", bidirectional: "Two-way ↔" };

function renderJobs() {
  jobsBody.replaceChildren();
  if (state.jobs.length === 0) {
    const tr = document.createElement("tr");
    tr.append(cell("No sync jobs yet.", "text-body-secondary"));
    tr.firstChild.colSpan = 8;
    jobsBody.append(tr);
    return;
  }
  const names = Object.fromEntries(state.connections.map((c) => [c.id, c.name]));
  for (const job of state.jobs) {
    const tr = document.createElement("tr");
    tr.append(cell(job.name));
    tr.append(cell(names[job.connection_id] || `#${job.connection_id}`));
    tr.append(cell(job.local_path || "/", "text-break"));
    tr.append(cell(job.remote_path || "/", "text-break"));
    const mode = document.createElement("td");
    mode.textContent = DIRECTION_LABEL[job.direction] || job.direction;
    const small = document.createElement("div");
    small.className = "small text-body-secondary";
    small.textContent = `conflicts: ${job.conflict_policy} · deletes: ${job.delete_policy}`;
    mode.append(small);
    tr.append(mode);
    const schedule = document.createElement("td");
    schedule.textContent = job.schedule_minutes ? `every ${job.schedule_minutes} min` : "manual";
    schedule.append(" ", badge(job.enabled ? "enabled" : "disabled", job.enabled ? "success" : "secondary"));
    tr.append(schedule);
    const last = document.createElement("td");
    if (job.last_status) last.append(badge(job.last_status, STATUS_KIND[job.last_status] || "secondary"), ` ${when(job.last_run_at)}`);
    else last.textContent = "never";
    tr.append(last);
    const actions = document.createElement("td");
    actions.className = "text-end text-nowrap";
    actions.append(button("History", "secondary", () => showHistory(job)));
    if (canWrite) {
      actions.append(button("Preview", "primary", () => queueRun(job, true)));
      actions.append(button("Run now", "success", () => queueRun(job, false)));
      actions.append(button("Edit", "secondary", async () => openJobModal(job)));
      actions.append(button("Delete", "danger", () => deleteJob(job)));
    }
    tr.append(actions);
    jobsBody.append(tr);
  }
}

let editingJob = null;

function fillSelect(select, items, selected) {
  select.replaceChildren();
  for (const [value, label] of items) {
    const option = document.createElement("option");
    option.value = String(value);
    option.textContent = label;
    select.append(option);
  }
  if (selected !== undefined && selected !== null) select.value = String(selected);
}

function openJobModal(job) {
  if (state.connections.length === 0) {
    showError("Add a connection first.");
    return;
  }
  editingJob = job;
  document.getElementById("job-modal-title").textContent = job ? `Edit ${job.name}` : "Add sync job";
  document.getElementById("job-name").value = job ? job.name : "";
  fillSelect(document.getElementById("job-connection"), state.connections.map((c) => [c.id, c.name]), job && job.connection_id);
  fillSelect(document.getElementById("job-local-root"), state.status.file_roots.map((r) => [r, r]), job && job.local_root);
  document.getElementById("job-local-path").value = job ? job.local_path : "";
  document.getElementById("job-remote-path").value = job ? job.remote_path : "";
  document.getElementById("job-direction").value = job ? job.direction : "push";
  document.getElementById("job-conflict").value = job ? job.conflict_policy : "keep_both";
  document.getElementById("job-delete").value = job ? job.delete_policy : "ignore";
  document.getElementById("job-schedule").value = job && job.schedule_minutes ? String(job.schedule_minutes) : "";
  document.getElementById("job-enabled").checked = job ? job.enabled : false;
  document.getElementById("job-excludes").value = job ? job.exclude_patterns.join("\n") : "";
  bootstrap.Modal.getOrCreateInstance(document.getElementById("job-modal")).show();
}

function collectJob() {
  const schedule = document.getElementById("job-schedule").value;
  return {
    name: document.getElementById("job-name").value.trim(),
    connection_id: Number(document.getElementById("job-connection").value),
    local_root: document.getElementById("job-local-root").value,
    local_path: document.getElementById("job-local-path").value.trim(),
    remote_path: document.getElementById("job-remote-path").value.trim(),
    direction: document.getElementById("job-direction").value,
    conflict_policy: document.getElementById("job-conflict").value,
    delete_policy: document.getElementById("job-delete").value,
    schedule_minutes: schedule ? Number(schedule) : null,
    enabled: document.getElementById("job-enabled").checked,
    exclude_patterns: document
      .getElementById("job-excludes")
      .value.split("\n")
      .map((line) => line.trim())
      .filter(Boolean),
  };
}

async function saveJob(event) {
  event.preventDefault();
  hideError();
  const body = collectJob();
  const modal = bootstrap.Modal.getInstance(document.getElementById("job-modal"));
  try {
    const send = (confirm) =>
      editingJob
        ? apiFetch(`/api/v1/sync-jobs/${editingJob.id}`, { method: "PATCH", body: { ...body, confirm } })
        : apiFetch("/api/v1/sync-jobs", { method: "POST", body: { ...body, confirm } });
    modal.hide();
    await withConfirmation("Confirm sync job settings", send);
    await loadJobs();
  } catch (error) {
    showError(error.message);
    modal.show();
  }
}

async function deleteJob(job) {
  confirmDialog(
    `Delete ${job.name}?`,
    ["The job, its sync records and run history are removed. No files are touched on either side."],
    [],
    async () => {
      await apiFetch(`/api/v1/sync-jobs/${job.id}`, { method: "DELETE" });
      await loadJobs();
    },
  );
}

async function queueRun(job, dryRun) {
  const run = await apiFetch(`/api/v1/sync-jobs/${job.id}/run`, {
    method: "POST",
    body: { dry_run: dryRun },
  });
  await watchRun(run.id, dryRun ? `Preview of ${job.name}` : `Run of ${job.name}`);
  await loadJobs();
}

// --------------------------------------------------------------------------
// Runs
// --------------------------------------------------------------------------

function renderRunModal(run, title) {
  document.getElementById("run-modal-title").textContent = title;
  const status = document.getElementById("run-modal-status");
  status.replaceChildren(badge(run.status, STATUS_KIND[run.status] || "secondary"));
  if (run.error) status.append(` ${run.error}`);
  if (run.status === "queued" && state.status && !state.status.worker_online) {
    status.append(" Waiting for the worker, which is offline.");
  }
  const verb = run.dry_run ? "planned" : "done";
  document.getElementById("run-modal-counts").textContent =
    `${verb}: ${run.uploaded} up, ${run.downloaded} down, ${run.conflicts} conflicts, ` +
    `${run.archived} archived, ${run.skipped} skipped, ${run.errors} errors, ${formatBytes(run.bytes_transferred)}`;
  const body = document.getElementById("run-modal-actions");
  body.replaceChildren();
  const summary = run.summary || {};
  const rows = run.dry_run ? summary.actions || [] : summary.problems || [];
  for (const item of rows) {
    const tr = document.createElement("tr");
    tr.append(cell(item.kind ? `${item.op} (${item.kind})` : item.op));
    tr.append(cell(item.path, "text-break"));
    tr.append(cell(item.message || item.reason || ""));
    body.append(tr);
  }
  for (const item of summary.rejected || []) {
    const tr = document.createElement("tr");
    tr.append(cell(`rejected (${item.side})`), cell(item.path, "text-break"), cell(item.reason));
    body.append(tr);
  }
  if (summary.truncated) {
    const tr = document.createElement("tr");
    tr.append(cell("…list truncated", "text-body-secondary"));
    body.append(tr);
  }
}

async function watchRun(runId, title) {
  const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById("run-modal"));
  let run = await apiFetch(`/api/v1/sync-runs/${runId}`);
  renderRunModal(run, title);
  modal.show();
  const element = document.getElementById("run-modal");
  while (["queued", "running"].includes(run.status) && element.classList.contains("show")) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    run = await apiFetch(`/api/v1/sync-runs/${runId}`);
    renderRunModal(run, title);
  }
}

async function showHistory(job) {
  state.historyJob = job;
  document.getElementById("history-card").classList.remove("d-none");
  document.getElementById("history-title").textContent = `Run history · ${job.name}`;
  const [runs, conflicts] = await Promise.all([
    apiFetch(`/api/v1/sync-jobs/${job.id}/runs`),
    apiFetch(`/api/v1/sync-jobs/${job.id}/conflicts`),
  ]);
  const body = document.getElementById("runs-body");
  body.replaceChildren();
  for (const run of runs) {
    const tr = document.createElement("tr");
    tr.append(cell(String(run.id)));
    const status = document.createElement("td");
    status.append(badge(run.status, STATUS_KIND[run.status] || "secondary"));
    tr.append(status);
    tr.append(cell(run.dry_run ? "preview" : run.trigger));
    for (const key of ["uploaded", "downloaded", "conflicts", "archived", "skipped", "errors"]) {
      tr.append(cell(String(run[key])));
    }
    tr.append(cell(formatBytes(run.bytes_transferred)));
    tr.append(cell(when(run.finished_at)));
    const actions = document.createElement("td");
    actions.className = "text-end text-nowrap";
    actions.append(button("Details", "secondary", () => watchRun(run.id, `Run #${run.id}`)));
    if (canWrite && ["queued", "running"].includes(run.status)) {
      actions.append(
        button("Cancel", "danger", async () => {
          await apiFetch(`/api/v1/sync-runs/${run.id}/cancel`, { method: "POST" });
          await showHistory(job);
        }),
      );
    }
    tr.append(actions);
    body.append(tr);
  }
  const list = document.getElementById("conflicts-list");
  list.replaceChildren();
  if (conflicts.length === 0) {
    const li = document.createElement("li");
    li.className = "list-group-item text-body-secondary";
    li.textContent = "None.";
    list.append(li);
  }
  for (const conflict of conflicts) {
    const li = document.createElement("li");
    li.className = "list-group-item d-flex justify-content-between align-items-start gap-2";
    const text = document.createElement("div");
    text.className = "text-break";
    text.textContent = `${conflict.path}: ${conflict.kind} → ${conflict.resolution}`;
    if (conflict.conflict_copy_path) {
      const copy = document.createElement("div");
      copy.className = "small text-body-secondary";
      copy.textContent = `other version: ${conflict.conflict_copy_path}`;
      text.append(copy);
    }
    li.append(text);
    if (canWrite) {
      li.append(
        button("Acknowledge", "secondary", async () => {
          await apiFetch(`/api/v1/sync-conflicts/${conflict.id}/acknowledge`, { method: "POST" });
          await showHistory(job);
        }),
      );
    }
    list.append(li);
  }
}

// --------------------------------------------------------------------------
// Loading
// --------------------------------------------------------------------------

async function loadConnections() {
  state.connections = await apiFetch("/api/v1/remotes");
  renderConnections();
}

async function loadJobs() {
  state.jobs = await apiFetch("/api/v1/sync-jobs");
  renderJobs();
  if (state.historyJob) await showHistory(state.historyJob);
}

async function loadStatus() {
  state.status = await apiFetch("/api/v1/remote-sync/status");
  renderStatus();
}

async function load() {
  const [providers] = await Promise.all([apiFetch("/api/v1/remotes/providers"), loadStatus()]);
  state.providers = providers;
  await loadConnections();
  await loadJobs();
}

function showOAuthOutcome() {
  const params = new URLSearchParams(window.location.search);
  const outcome = params.get("oauth");
  if (outcome === "connected") showInfo("Google Drive authorized.");
  else if (outcome === "error") showError(`Google Drive authorization failed: ${params.get("message") || "unknown error"}`);
  if (outcome) window.history.replaceState(null, "", window.location.pathname);
}

document.getElementById("add-connection")?.addEventListener("click", () => openConnectionModal(null));
document.getElementById("add-job")?.addEventListener("click", () => openJobModal(null));
document.getElementById("connection-provider")?.addEventListener("change", (event) =>
  renderConnectionFields(event.target.value, null),
);
document.getElementById("connection-form")?.addEventListener("submit", saveConnection);
document.getElementById("job-form")?.addEventListener("submit", saveJob);

showOAuthOutcome();
load().catch((error) => showError(error.message));
setInterval(() => loadStatus().catch(() => {}), 15000);
