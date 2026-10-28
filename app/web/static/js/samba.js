import { apiFetch } from "./api.js";

const banner = document.getElementById("error-banner");
const statusCard = document.getElementById("status-card");
const body = document.getElementById("shares-body");
const form = document.getElementById("share-form");
const applyBtn = document.getElementById("apply-btn");

function showError(message) {
  banner.textContent = message;
  banner.classList.remove("d-none");
  banner.classList.add("alert-danger");
}

function hideError() {
  banner.classList.add("d-none");
}

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text;
  return td;
}

function yesNo(value) {
  return value ? "yes" : "no";
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

  const rows = [
    ["Configuration path", status.config_path],
    ["Configuration exists", yesNo(status.config_exists)],
    ["Managed markers present", yesNo(status.managed_markers_present)],
    ["Managed shares", String(status.managed_share_count)],
    ["Backup directory", status.backup_dir],
    ["Pending changes", yesNo(status.pending_changes)],
    ["Privileged helper", status.helper_available ? "available" : "not available"],
    ["Reload required", yesNo(status.reload_required)],
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
  const note = document.createElement("div");
  note.className = "card-footer text-body-secondary small";
  note.textContent = status.message;
  card.append(note);
  statusCard.append(card);
}

function renderShares(shares) {
  body.replaceChildren();
  const canWrite = form !== null;
  for (const share of shares) {
    const row = document.createElement("tr");
    row.append(
      cell(share.name),
      cell(share.path),
      cell(yesNo(share.read_only)),
      cell(yesNo(share.browseable)),
      cell(yesNo(share.guest_ok)),
    );
    const enabledCell = document.createElement("td");
    const enabledBadge = document.createElement("span");
    enabledBadge.className = share.enabled ? "badge text-bg-success" : "badge text-bg-secondary";
    enabledBadge.textContent = share.enabled ? "enabled" : "disabled";
    enabledCell.append(enabledBadge);
    row.append(enabledCell);
    if (canWrite) {
      const actions = document.createElement("td");
      actions.className = "text-end";
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "btn btn-sm btn-outline-secondary ms-1";
      edit.textContent = "Edit";
      edit.addEventListener("click", () => startEdit(share));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn btn-sm btn-outline-danger ms-1";
      remove.textContent = "Delete";
      remove.addEventListener("click", () => removeShare(share));
      actions.append(edit, remove);
      row.append(actions);
    }
    body.append(row);
  }
  if (!shares.length) {
    const row = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 7;
    td.className = "text-body-secondary text-center";
    td.textContent = "No shares defined yet.";
    row.append(td);
    body.append(row);
  }
}

function formPayload() {
  const data = new FormData(form);
  const bool = (name) => form.elements.namedItem(name).checked;
  return {
    name: String(data.get("name") || "").trim(),
    path: String(data.get("path") || "").trim(),
    comment: String(data.get("comment") || "").trim(),
    read_only: bool("read_only"),
    browseable: bool("browseable"),
    guest_ok: bool("guest_ok"),
    valid_users: String(data.get("valid_users") || "").trim(),
    write_list: String(data.get("write_list") || "").trim(),
    enabled: bool("enabled"),
  };
}

async function submitShare(event) {
  event.preventDefault();
  hideError();
  const payload = formPayload();
  const id = document.getElementById("share-id").value;
  try {
    if (id) {
      const { name: _name, ...updates } = payload;
      await apiFetch(`/api/v1/samba/shares/${id}`, { method: "PATCH", body: updates });
    } else {
      await apiFetch("/api/v1/samba/shares", { method: "POST", body: payload });
    }
    resetForm();
    await refresh();
  } catch (error) {
    showError(error.message);
  }
}

function startEdit(share) {
  document.getElementById("share-id").value = String(share.id);
  document.getElementById("sf-name").value = share.name;
  document.getElementById("sf-name").readOnly = true;
  document.getElementById("sf-path").value = share.path;
  document.getElementById("sf-comment").value = share.comment;
  document.getElementById("sf-read-only").checked = share.read_only;
  document.getElementById("sf-browseable").checked = share.browseable;
  document.getElementById("sf-guest-ok").checked = share.guest_ok;
  document.getElementById("sf-valid-users").value = share.valid_users || "";
  document.getElementById("sf-write-list").value = share.write_list || "";
  document.getElementById("sf-enabled").checked = share.enabled;
  document.getElementById("share-form-header").textContent = `Edit share: ${share.name}`;
  document.getElementById("share-submit").textContent = "Save changes";
  document.getElementById("share-cancel").classList.remove("d-none");
  document.getElementById("sf-path").focus();
}

function resetForm() {
  form.reset();
  document.getElementById("share-id").value = "";
  document.getElementById("sf-name").readOnly = false;
  document.getElementById("share-form-header").textContent = "New share";
  document.getElementById("share-submit").textContent = "Create share";
  document.getElementById("share-cancel").classList.add("d-none");
}

async function removeShare(share) {
  if (!window.confirm(`Delete the share "${share.name}"? The directory is not touched.`)) return;
  try {
    await apiFetch(`/api/v1/samba/shares/${share.id}`, { method: "DELETE" });
    await refresh();
  } catch (error) {
    showError(error.message);
  }
}

async function applyConfig() {
  if (!window.confirm(
    "Write and validate the Samba configuration now? The previous config is backed up first.",
  )) {
    return;
  }
  hideError();
  try {
    const result = await apiFetch("/api/v1/samba/apply", { method: "POST", body: {} });
    banner.textContent = result.message;
    banner.classList.remove("d-none");
    banner.classList.remove("alert-danger");
    banner.classList.add("alert-success");
    await refresh();
  } catch (error) {
    showError(error.message);
  }
}

async function refresh() {
  hideError();
  try {
    const status = await apiFetch("/api/v1/samba/status");
    renderStatus(status);
    const shares = await apiFetch("/api/v1/samba/shares");
    renderShares(shares);
  } catch (error) {
    showError(`Could not load Samba data: ${error.message}`);
  }
}

form?.addEventListener("submit", submitShare);
document.getElementById("share-cancel")?.addEventListener("click", resetForm);
applyBtn?.addEventListener("click", applyConfig);

refresh();

