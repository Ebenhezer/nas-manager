import { apiFetch } from "./api.js";
import { formatBytes } from "./format.js";

const banner = document.getElementById("error-banner");
const statusCard = document.getElementById("status-card");
const uploadersBody = document.getElementById("uploaders-body");
const filesBody = document.getElementById("files-body");
const filesTotal = document.getElementById("files-total");
const fileFilter = document.getElementById("file-filter");
const cleanupBtn = document.getElementById("cleanup-btn");
const purgeForm = document.getElementById("purge-form");
const canWrite = cleanupBtn !== null;

function showError(message) {
  banner.textContent = message;
  banner.classList.remove("d-none");
}

function hideError() {
  banner.classList.add("d-none");
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function renderStatus(status) {
  statusCard.replaceChildren();
  const card = document.createElement("div");
  card.className = "card";
  const header = document.createElement("div");
  header.className = "card-header";
  header.textContent = "Status";
  card.append(header);
  const list = document.createElement("dl");
  list.className = "row mb-0 px-3 py-2";
  const percent =
    status.total_used_bytes > 0 && status.total_uploaders > 0
      ? ` (${formatBytes(status.total_used_bytes)})`
      : "";
  const rows = [
    ["Public uploads", status.enabled ? "enabled" : "disabled"],
    ["Folder", status.dir],
    ["Max file size", formatBytes(status.max_file_bytes)],
    ["Default quota", formatBytes(status.default_quota_bytes)],
    ["Uploaders", String(status.total_uploaders)],
    ["Files", `${String(status.total_files)}${percent}`],
  ];
  for (const [term, definition] of rows) {
    const dt = document.createElement("dt");
    dt.className = "col-sm-3 text-body-secondary";
    dt.textContent = term;
    const dd = document.createElement("dd");
    dd.className = "col-sm-9";
    dd.textContent = definition;
    list.append(dt, dd);
  }
  card.append(list);
  statusCard.append(card);
}

function renderUploaders(uploaders) {
  uploadersBody.replaceChildren();
  if (uploaders.length === 0) {
    const row = document.createElement("tr");
    row.append(cell("No uploaders yet.", "text-body-secondary"));
    uploadersBody.append(row);
    return;
  }
  for (const uploader of uploaders) {
    const row = document.createElement("tr");
    row.append(cell(uploader.uploader_key));

    const quotaCell = document.createElement("td");
    if (canWrite) {
      const form = document.createElement("form");
      form.className = "d-flex gap-1";
      const input = document.createElement("input");
      input.type = "number";
      input.className = "form-control form-control-sm";
      input.min = "0";
      input.value = String(uploader.quota_bytes);
      input.setAttribute("aria-label", `Quota for ${uploader.uploader_key}`);
      const save = document.createElement("button");
      save.type = "submit";
      save.className = "btn btn-sm btn-outline-secondary";
      save.textContent = "Save";
      form.append(input, save);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        await saveQuota(uploader.uploader_key, Number(input.value));
      });
      quotaCell.append(form);
    } else {
      quotaCell.textContent = formatBytes(uploader.quota_bytes);
    }
    row.append(quotaCell);

    const usedRatio = uploader.quota_bytes > 0 ? uploader.used_bytes / uploader.quota_bytes : 0;
    const usedCell = document.createElement("td");
    usedCell.style.minWidth = "140px";
    const bar = document.createElement("div");
    bar.className = "progress";
    const barInner = document.createElement("div");
    barInner.className = "progress-bar";
    barInner.style.width = `${Math.min(100, usedRatio * 100)}%`;
    barInner.textContent = formatBytes(uploader.used_bytes);
    bar.append(barInner);
    usedCell.append(bar);
    row.append(usedCell);
    row.append(cell(String(uploader.file_count)));

    const statusCell = document.createElement("td");
    if (uploader.over_quota) {
      statusCell.className = "text-danger fw-semibold";
      statusCell.textContent = "over quota";
    } else {
      statusCell.textContent = "under quota";
    }
    row.append(statusCell);

    if (canWrite) {
      const actions = document.createElement("td");
      actions.className = "text-end";
      row.append(actions);
    }
    uploadersBody.append(row);
  }
}

function renderFiles(files, total) {
  filesBody.replaceChildren();
  filesTotal.textContent = `${String(files.length)} of ${String(total)} files shown`;
  if (files.length === 0) {
    const row = document.createElement("tr");
    row.append(cell("No files.", "text-body-secondary"));
    filesBody.append(row);
    return;
  }
  for (const file of files) {
    const row = document.createElement("tr");
    row.append(cell(file.uploader_key));
    row.append(cell(file.filename));
    row.append(cell(formatBytes(file.size_bytes)));
    row.append(cell(file.ip_address ?? ""));
    row.append(cell(file.created_at));
    if (canWrite) {
      const actions = document.createElement("td");
      actions.className = "text-end";
      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn btn-sm btn-outline-danger";
      del.textContent = "Delete";
      del.addEventListener("click", async () => deleteFile(file.id));
      actions.append(del);
      row.append(actions);
    }
    filesBody.append(row);
  }
}

async function refresh() {
  hideError();
  try {
    const status = await apiFetch("/api/v1/public/uploads/status");
    renderStatus(status);
    renderUploaders(status.uploaders);
    updateFilter(status.uploaders);
    const page = await apiFetch("/api/v1/public/uploads/files");
    renderFiles(page.files, page.total);
  } catch (error) {
    showError(error.message);
  }
}

function updateFilter(uploaders) {
  const selected = fileFilter.value;
  fileFilter.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "All uploaders";
  fileFilter.append(all);
  for (const uploader of uploaders) {
    const option = document.createElement("option");
    option.value = uploader.uploader_key;
    option.textContent = uploader.uploader_key;
    fileFilter.append(option);
  }
  if (uploaders.some((uploader) => uploader.uploader_key === selected)) {
    fileFilter.value = selected;
  }
}

fileFilter.addEventListener("change", async () => {
  const query = fileFilter.value ? `?uploader_key=${encodeURIComponent(fileFilter.value)}` : "";
  try {
    const page = await apiFetch(`/api/v1/public/uploads/files${query}`);
    renderFiles(page.files, page.total);
  } catch (error) {
    showError(error.message);
  }
});

async function saveQuota(uploaderKey, quotaBytes) {
  hideError();
  try {
    await apiFetch(`/api/v1/public/uploads/quotas/${encodeURIComponent(uploaderKey)}`, {
      method: "PATCH",
      body: { quota_bytes: quotaBytes },
    });
    await refresh();
  } catch (error) {
    showError(error.message);
  }
}

async function deleteFile(fileId) {
  if (!window.confirm("Delete this file? This cannot be undone.")) {
    return;
  }
  hideError();
  try {
    await apiFetch(`/api/v1/public/uploads/files/${fileId}`, { method: "DELETE" });
    await refresh();
  } catch (error) {
    showError(error.message);
  }
}

async function runCleanup(olderThanDays) {
  hideError();
  const body = olderThanDays === null ? {} : { older_than_days: olderThanDays };
  try {
    const result = await apiFetch("/api/v1/public/uploads/cleanup", {
      method: "POST",
      body,
    });
    banner.textContent = `Cleanup: ${result.removed_temp_files} temp file(s) removed, ` +
      `${result.removed_files} file(s) purged (${formatBytes(result.removed_bytes)}), ` +
      `${result.reconciled_uploaders} uploader(s) reconciled.`;
    banner.classList.remove("d-none");
    banner.classList.add("alert-success");
    await refresh();
  } catch (error) {
    banner.classList.remove("alert-success");
    showError(error.message);
  }
}

if (cleanupBtn !== null) {
  cleanupBtn.addEventListener("click", () => runCleanup(null));
}
if (purgeForm !== null) {
  purgeForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const days = Number(document.getElementById("purge-days").value);
    runCleanup(days);
  });
}

refresh();