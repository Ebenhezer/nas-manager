import { apiFetch, getJson } from "./api.js";
import { formatBytes } from "./format.js";

const root = document.getElementById("backups-root");
const canWrite = root.dataset.canWrite === "true";
const canRestore = root.dataset.canRestore === "true";
const canRemotes = root.dataset.canRemotes === "true";
const banner = document.getElementById("error-banner");
const infoBanner = document.getElementById("info-banner");
const workerBadge = document.getElementById("worker-badge");

const state = {
  status: null,
  repositories: [],
  jobs: [],
  connections: [],
  snapshots: [],
  runs: [],
  snapshotRepository: null,
  browsing: null,
  restoring: null,
};

const STATUS_KIND = {
  succeeded: "success",
  partial: "warning",
  failed: "danger",
  cancelled: "secondary",
  running: "primary",
  queued: "info",
  ok: "success",
};

function $(id) {
  return document.getElementById(id);
}

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

function badgeCell(text, kind) {
  const td = document.createElement("td");
  td.append(badge(text, kind));
  return td;
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

function emptyRow(body, text, columns) {
  const tr = document.createElement("tr");
  const td = cell(text, "text-body-secondary");
  td.colSpan = columns;
  tr.append(td);
  body.append(tr);
}

function when(value) {
  return value ? new Date(`${value}${value.endsWith("Z") ? "" : "Z"}`).toLocaleString() : "–";
}

function options(select, items, selected) {
  select.replaceChildren();
  for (const [value, label] of items) {
    const option = document.createElement("option");
    option.value = String(value);
    option.textContent = label;
    if (String(value) === String(selected)) option.selected = true;
    select.append(option);
  }
}

function lines(value) {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function toggle(id, visible) {
  $(id).classList.toggle("d-none", !visible);
}

// --------------------------------------------------------------------------
// Confirmation dialog (the server lists the risks; we show them first)
// --------------------------------------------------------------------------

function confirmDialog(title, planLines, warnings, action) {
  $("confirm-modal-title").textContent = title;
  const plan = $("confirm-modal-plan");
  plan.replaceChildren();
  for (const line of planLines) {
    const p = document.createElement("p");
    p.className = "mb-1";
    p.textContent = line;
    plan.append(p);
  }
  const warningList = $("confirm-modal-warnings");
  warningList.replaceChildren();
  for (const warning of warnings) {
    const li = document.createElement("li");
    li.textContent = warning;
    warningList.append(li);
  }
  const modal = bootstrap.Modal.getOrCreateInstance($("confirm-modal"));
  $("confirm-modal-accept").onclick = async () => {
    modal.hide();
    try {
      await action();
    } catch (error) {
      showError(error.message);
    }
  };
  modal.show();
}

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

function repositoryName(id) {
  const repo = state.repositories.find((r) => r.id === id);
  return repo ? repo.name : `#${id}`;
}

function connectionName(id) {
  const conn = state.connections.find((c) => c.id === id);
  return conn ? conn.name : `connection #${id}`;
}

// --------------------------------------------------------------------------
// Repositories
// --------------------------------------------------------------------------

function renderRepositories() {
  const body = $("repositories-body");
  body.replaceChildren();
  if (state.repositories.length === 0) {
    emptyRow(body, "No repositories yet.", 5);
    return;
  }
  for (const repo of state.repositories) {
    const tr = document.createElement("tr");
    tr.append(cell(repo.name));
    const where =
      repo.target_kind === "local"
        ? `${repo.local_root}/${repo.repo_path}`
        : `${connectionName(repo.connection_id)}:/${repo.repo_path}`;
    tr.append(cell(where, "text-break small"));
    tr.append(
      repo.initialized_at ? cell(when(repo.initialized_at)) : badgeCell("not initialized", "warning"),
    );
    tr.append(
      repo.last_verify_status
        ? badgeCell(
            `${repo.last_verify_status} · ${when(repo.last_verified_at)}`,
            STATUS_KIND[repo.last_verify_status] || "danger",
          )
        : cell("never", "text-body-secondary"),
    );
    const actions = cell("", "text-end text-nowrap");
    actions.append(
      button("Snapshots", "secondary", async () => {
        state.snapshotRepository = repo.id;
        $("snapshot-repository").value = String(repo.id);
        await loadSnapshots();
      }),
    );
    if (canWrite) {
      actions.append(
        button("Initialize", "primary", async () => {
          const result = await apiFetch(`/api/v1/backup-repositories/${repo.id}/init`, {
            method: "POST",
          });
          showInfo(
            result.created
              ? `Repository ${repo.name} created.`
              : `Existing repository adopted: ${result.snapshots} snapshot(s) found.`,
          );
          await refresh();
        }),
      );
      if (repo.initialized_at) {
        actions.append(
          button("Verify", "success", async () => {
            await apiFetch(`/api/v1/backup-repositories/${repo.id}/verify`, {
              method: "POST",
              body: { full: true },
            });
            showInfo("Full verification queued.");
            await loadRuns();
          }),
        );
        actions.append(
          button("Prune…", "warning", async () => {
            const preview = await apiFetch(`/api/v1/backup-repositories/${repo.id}/prune`, {
              method: "POST",
              body: { dry_run: true },
            });
            showInfo(`Prune preview queued as run #${preview.id}; see Recent runs.`);
            await withConfirmation("Prune repository", (confirm) =>
              apiFetch(`/api/v1/backup-repositories/${repo.id}/prune`, {
                method: "POST",
                body: { dry_run: false, confirm },
              }),
            );
            await loadRuns();
          }),
        );
      }
      actions.append(
        button("Forget", "danger", async () => {
          if (!window.confirm(`Forget repository ${repo.name}? The data on the target stays.`)) {
            return;
          }
          await apiFetch(`/api/v1/backup-repositories/${repo.id}`, { method: "DELETE" });
          await refresh();
        }),
      );
    }
    tr.append(actions);
    body.append(tr);
  }
}

function setupRepositoryForm() {
  const form = $("repository-form");
  if (!form) return;
  const kind = $("repo-kind");
  const sync = () => {
    toggle("repo-root-group", kind.value === "local");
    toggle("repo-connection-group", kind.value === "remote");
  };
  kind.addEventListener("change", sync);
  $("add-repository").addEventListener("click", () => {
    form.reset();
    options($("repo-root"), state.status.local_roots.map((r) => [r, r]));
    options(
      $("repo-connection"),
      state.connections.map((c) => [c.id, `${c.name} (${c.provider})`]),
    );
    sync();
    form.classList.remove("d-none");
  });
  $("repository-cancel").addEventListener("click", () => form.classList.add("d-none"));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    hideError();
    const passphrase = $("repo-passphrase").value;
    if (passphrase !== $("repo-passphrase2").value) {
      showError("The passphrases do not match.");
      return;
    }
    const body = {
      name: $("repo-name").value.trim(),
      target_kind: kind.value,
      repo_path: $("repo-path").value.trim(),
      passphrase,
    };
    if (kind.value === "local") body.local_root = $("repo-root").value;
    else body.connection_id = Number($("repo-connection").value);
    (async () => {
      const repo = await apiFetch("/api/v1/backup-repositories", { method: "POST", body });
      form.classList.add("d-none");
      try {
        const result = await apiFetch(`/api/v1/backup-repositories/${repo.id}/init`, {
          method: "POST",
        });
        showInfo(
          result.created
            ? `Repository ${repo.name} created. Store the passphrase safely.`
            : `Existing repository adopted: ${result.snapshots} snapshot(s) found.`,
        );
      } finally {
        await refresh();
      }
    })().catch((error) => showError(error.message));
  });
}

// --------------------------------------------------------------------------
// Jobs
// --------------------------------------------------------------------------

function describeSource(job) {
  if (job.source_kind === "files") return `${job.source_root}/${job.source_path}`.replace(/\/$/, "");
  if (job.source_kind === "media") return `media libraries/${job.source_path}`.replace(/\/$/, "");
  if (job.source_kind === "system") return "system state";
  return `${connectionName(job.source_connection_id)}:/${job.source_path}`;
}

function renderJobs() {
  const body = $("jobs-body");
  body.replaceChildren();
  if (state.jobs.length === 0) {
    emptyRow(body, "No backup jobs yet.", 8);
    return;
  }
  for (const job of state.jobs) {
    const tr = document.createElement("tr");
    tr.append(cell(job.name));
    tr.append(badgeCell(job.direction, job.direction === "pull" ? "info" : "secondary"));
    tr.append(cell(describeSource(job), "text-break small"));
    tr.append(cell(repositoryName(job.repository_id)));
    tr.append(
      cell(
        job.schedule_minutes
          ? `every ${job.schedule_minutes} min${job.enabled ? "" : " (disabled)"}`
          : "manual",
      ),
    );
    const keeps = [job.keep_last, job.keep_daily, job.keep_weekly, job.keep_monthly];
    tr.append(
      cell(
        keeps.some((n) => n > 0)
          ? `${job.keep_last} last · ${job.keep_daily}d · ${job.keep_weekly}w · ${job.keep_monthly}m`
          : "keep all",
        "small",
      ),
    );
    tr.append(
      job.last_status
        ? badgeCell(`${job.last_status} · ${when(job.last_run_at)}`, STATUS_KIND[job.last_status] || "secondary")
        : cell("never", "text-body-secondary"),
    );
    const actions = cell("", "text-end text-nowrap");
    if (canWrite) {
      actions.append(
        button("Run now", "primary", async () => {
          await apiFetch(`/api/v1/backup-jobs/${job.id}/run`, { method: "POST" });
          showInfo(`Backup of ${job.name} queued.`);
          await loadRuns();
        }),
      );
      actions.append(
        button(job.enabled ? "Disable" : "Enable", "secondary", async () => {
          await withConfirmation(job.enabled ? "Disable job" : "Enable job", (confirm) =>
            apiFetch(`/api/v1/backup-jobs/${job.id}`, {
              method: "PATCH",
              body: { enabled: !job.enabled, confirm },
            }),
          );
          await refresh();
        }),
      );
      actions.append(
        button("Delete", "danger", async () => {
          if (!window.confirm(`Delete job ${job.name}? Its snapshots stay in the repository.`)) {
            return;
          }
          await apiFetch(`/api/v1/backup-jobs/${job.id}`, { method: "DELETE" });
          await refresh();
        }),
      );
    }
    tr.append(actions);
    body.append(tr);
  }
}

function setupJobForm() {
  const form = $("job-form");
  if (!form) return;
  const source = $("job-source");
  const sync = () => {
    toggle("job-root-group", source.value === "files");
    toggle("job-connection-group", source.value === "remote");
    toggle("job-path-group", source.value !== "system");
  };
  source.addEventListener("change", sync);
  $("add-job").addEventListener("click", () => {
    form.reset();
    options(
      $("job-repository"),
      state.repositories.map((r) => [r.id, `${r.name} (${r.target_kind})`]),
    );
    options($("job-root"), state.status.file_roots.map((r) => [r, r]));
    options(
      $("job-connection"),
      state.connections.map((c) => [c.id, `${c.name} (${c.provider})`]),
    );
    sync();
    form.classList.remove("d-none");
  });
  $("job-cancel").addEventListener("click", () => form.classList.add("d-none"));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    hideError();
    const schedule = $("job-schedule").value;
    const body = {
      name: $("job-name").value.trim(),
      repository_id: Number($("job-repository").value),
      source_kind: source.value,
      source_path: source.value === "system" ? "" : $("job-path").value.trim(),
      exclude_patterns: lines($("job-excludes").value),
      schedule_minutes: schedule ? Number(schedule) : null,
      enabled: $("job-enabled").checked,
      keep_last: Number($("job-keep-last").value || 0),
      keep_daily: Number($("job-keep-daily").value || 0),
      keep_weekly: Number($("job-keep-weekly").value || 0),
      keep_monthly: Number($("job-keep-monthly").value || 0),
    };
    if (source.value === "files") body.source_root = $("job-root").value;
    if (source.value === "remote") body.source_connection_id = Number($("job-connection").value);
    withConfirmation("Create backup job", (confirm) =>
      apiFetch("/api/v1/backup-jobs", { method: "POST", body: { ...body, confirm } }),
    )
      .then(async () => {
        form.classList.add("d-none");
        await refresh();
      })
      .catch((error) => showError(error.message));
  });
}

// --------------------------------------------------------------------------
// Snapshots, browsing and restore
// --------------------------------------------------------------------------

function renderSnapshotSelector() {
  const select = $("snapshot-repository");
  if (state.snapshotRepository === null && state.repositories.length) {
    state.snapshotRepository = state.repositories[0].id;
  }
  options(
    select,
    state.repositories.map((r) => [r.id, r.name]),
    state.snapshotRepository,
  );
}

function renderSnapshots() {
  const body = $("snapshots-body");
  body.replaceChildren();
  if (state.snapshots.length === 0) {
    emptyRow(body, "No snapshots in this repository.", 7);
    return;
  }
  for (const snap of state.snapshots) {
    const tr = document.createElement("tr");
    tr.append(cell(when(snap.taken_at)));
    tr.append(cell(snap.job_name || "–"));
    tr.append(cell(snap.source_label || snap.source_kind, "small text-break"));
    tr.append(cell(String(snap.file_count)));
    tr.append(cell(formatBytes(snap.total_bytes)));
    if (snap.verify_status) {
      const td = badgeCell(snap.verify_status, STATUS_KIND[snap.verify_status] || "danger");
      if (snap.verify_error) td.title = snap.verify_error;
      tr.append(td);
    } else {
      tr.append(cell("–", "text-body-secondary"));
    }
    const actions = cell("", "text-end text-nowrap");
    actions.append(button("Browse", "secondary", () => openBrowser(snap)));
    if (canRestore) actions.append(button("Restore…", "primary", async () => openRestore(snap)));
    tr.append(actions);
    body.append(tr);
  }
}

async function openBrowser(snap) {
  state.browsing = snap;
  $("browse-modal-title").textContent = `Snapshot ${snap.snapshot_id}`;
  $("browse-prefix").value = "";
  bootstrap.Modal.getOrCreateInstance($("browse-modal")).show();
  await loadEntries();
}

async function loadEntries() {
  const snap = state.browsing;
  const prefix = $("browse-prefix").value.trim();
  const body = $("browse-body");
  body.replaceChildren();
  $("browse-summary").textContent = "Loading…";
  const result = await getJson(
    `/api/v1/backup-snapshots/${snap.id}/entries?prefix=${encodeURIComponent(prefix)}`,
  );
  $("browse-summary").textContent = `${result.total} file(s)${result.truncated ? `, showing the first ${result.entries.length}` : ""}.`;
  for (const entry of result.entries) {
    const tr = document.createElement("tr");
    tr.append(cell(entry.path, "text-break"));
    tr.append(cell(formatBytes(entry.size), "text-end"));
    body.append(tr);
  }
}

function openRestore(snap) {
  state.restoring = snap;
  const form = $("restore-form");
  form.reset();
  $("restore-modal-title").textContent = `Restore ${snap.snapshot_id}`;
  options($("restore-root"), state.status.file_roots.map((r) => [r, r]));
  options(
    $("restore-connection"),
    state.connections.map((c) => [c.id, `${c.name} (${c.provider})`]),
  );
  $("restore-kind").value = snap.source_kind === "system" ? "staging" : "files";
  $("restore-path").value = `restored/${snap.snapshot_id}`;
  syncRestore();
  bootstrap.Modal.getOrCreateInstance($("restore-modal")).show();
}

function syncRestore() {
  const kind = $("restore-kind").value;
  toggle("restore-root-group", kind === "files");
  toggle("restore-connection-group", kind === "remote");
  toggle("restore-path-group", kind !== "staging");
  toggle("restore-staging-hint", kind === "staging");
  $("restore-overwrite").disabled = kind === "staging";
}

function setupRestoreForm() {
  const form = $("restore-form");
  if (!form) return;
  $("restore-kind").addEventListener("change", syncRestore);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    hideError();
    const kind = $("restore-kind").value;
    const target = { kind, path: kind === "staging" ? "" : $("restore-path").value.trim() };
    if (kind === "files") target.root = $("restore-root").value;
    if (kind === "remote") target.connection_id = Number($("restore-connection").value);
    const body = {
      target,
      paths: lines($("restore-paths").value),
      overwrite: $("restore-overwrite").checked && kind !== "staging",
      dry_run: $("restore-dry-run").checked,
    };
    const snap = state.restoring;
    withConfirmation("Restore and replace files", (confirm) =>
      apiFetch(`/api/v1/backup-snapshots/${snap.id}/restore`, {
        method: "POST",
        body: { ...body, confirm },
      }),
    )
      .then(async (run) => {
        bootstrap.Modal.getOrCreateInstance($("restore-modal")).hide();
        showInfo(
          kind === "staging"
            ? `Restore run #${run.id} queued. When it succeeds, stop the services and run: python -m app.cli restore-system --run ${run.id} --yes`
            : `Restore run #${run.id} queued.`,
        );
        await loadRuns();
      })
      .catch((error) => showError(error.message));
  });
}

// --------------------------------------------------------------------------
// Runs
// --------------------------------------------------------------------------

function runResult(run) {
  if (run.error) return run.error;
  const summary = [];
  if (run.snapshot_id) summary.push(`snapshot ${run.snapshot_id}`);
  if (run.skipped) summary.push(`${run.skipped} skipped`);
  if (run.errors) summary.push(`${run.errors} error(s)`);
  if (run.dry_run) summary.push("dry run");
  return summary.join(" · ") || "–";
}

function renderRuns() {
  const body = $("runs-body");
  body.replaceChildren();
  if (state.runs.length === 0) {
    emptyRow(body, "No runs yet.", 9);
    return;
  }
  for (const run of state.runs) {
    const tr = document.createElement("tr");
    tr.append(cell(String(run.id)));
    tr.append(cell(run.kind));
    tr.append(badgeCell(run.status, STATUS_KIND[run.status] || "secondary"));
    tr.append(cell(String(run.files)));
    tr.append(cell(`${run.new_blobs} / ${run.reused}`));
    tr.append(cell(`${formatBytes(run.bytes_processed)} (${formatBytes(run.bytes_uploaded)} sent)`, "small"));
    tr.append(cell(when(run.started_at || run.created_at)));
    tr.append(cell(runResult(run), "small text-break"));
    const actions = cell("", "text-end");
    if (canWrite && (run.status === "queued" || run.status === "running")) {
      actions.append(
        button("Cancel", "danger", async () => {
          await apiFetch(`/api/v1/backup-runs/${run.id}/cancel`, { method: "POST" });
          await loadRuns();
        }),
      );
    }
    tr.append(actions);
    body.append(tr);
  }
}

// --------------------------------------------------------------------------
// Loading
// --------------------------------------------------------------------------

async function loadSnapshots() {
  if (state.snapshotRepository === null) {
    state.snapshots = [];
  } else {
    state.snapshots = await getJson(
      `/api/v1/backup-repositories/${state.snapshotRepository}/snapshots`,
    );
  }
  renderSnapshots();
}

async function loadRuns() {
  state.runs = await getJson("/api/v1/backup-runs?limit=25");
  renderRuns();
}

async function refresh() {
  const [status, repositories, jobs] = await Promise.all([
    getJson("/api/v1/backup/status"),
    getJson("/api/v1/backup-repositories"),
    getJson("/api/v1/backup-jobs"),
  ]);
  state.status = status;
  state.repositories = repositories;
  state.jobs = jobs;
  if (canRemotes) {
    state.connections = await getJson("/api/v1/remotes").catch(() => []);
  }
  renderStatus();
  renderRepositories();
  renderJobs();
  renderSnapshotSelector();
  await Promise.all([loadSnapshots(), loadRuns()]);
}

$("snapshot-repository").addEventListener("change", (event) => {
  state.snapshotRepository = Number(event.target.value);
  loadSnapshots().catch((error) => showError(error.message));
});
$("browse-form").addEventListener("submit", (event) => {
  event.preventDefault();
  loadEntries().catch((error) => showError(error.message));
});
setupRepositoryForm();
setupJobForm();
setupRestoreForm();
refresh().catch((error) => showError(error.message));
setInterval(() => {
  Promise.all([getJson("/api/v1/backup/status"), getJson("/api/v1/backup-runs?limit=25")])
    .then(([status, runs]) => {
      state.status = status;
      state.runs = runs;
      renderStatus();
      renderRuns();
    })
    .catch(() => {});
}, 5000);
