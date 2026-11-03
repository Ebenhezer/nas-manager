import { apiFetch } from "./api.js";

const banner = document.getElementById("error-banner");
const notice = document.getElementById("notice-banner");
const workerBadge = document.getElementById("worker-badge");
const body = document.getElementById("destinations-body");
const form = document.getElementById("destination-form");
const canWrite = form !== null;
const state = { destinations: [], editing: null };

function showError(message) {
  banner.textContent = message;
  banner.classList.remove("d-none");
  notice.classList.add("d-none");
}

function showNotice(message) {
  notice.textContent = message;
  notice.classList.remove("d-none");
  banner.classList.add("d-none");
}

function fmtTime(value) {
  if (!value) return "–";
  return String(value).replace("T", " ").replace("Z", "").slice(0, 19);
}

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text;
  return td;
}

function badge(text, tone) {
  const span = document.createElement("span");
  span.className = `badge text-bg-${tone}`;
  span.textContent = text;
  return span;
}

function definitionList(target, pairs) {
  target.replaceChildren();
  for (const [label, value] of pairs) {
    const dt = document.createElement("dt");
    dt.className = "col-5";
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.className = "col-7 text-break";
    dd.textContent = value;
    target.append(dt, dd);
  }
}

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
  const list = document.getElementById("confirm-modal-warnings");
  list.replaceChildren();
  for (const warning of warnings) {
    const li = document.createElement("li");
    li.textContent = warning;
    list.append(li);
  }
  const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById("confirm-modal"));
  document.getElementById("confirm-modal-accept").onclick = async () => {
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
// Status cards
// --------------------------------------------------------------------------

function renderLocal(logging) {
  definitionList(document.getElementById("local-logging"), [
    ["Level / format", `${logging.level} · ${logging.format}`],
    ["Log file", logging.file || "disabled"],
    [
      "Rotation",
      logging.rotation === "daily"
        ? `daily (UTC), keep ${logging.backup_count}`
        : `at ${Math.round(logging.max_bytes / 1048576)} MiB, keep ${logging.backup_count}`,
    ],
    ["Compression", logging.compress ? "gzip" : "off"],
    ["journald", logging.journald ? `native (${logging.syslog_identifier})` : "off"],
    ["Console", logging.console ? "on" : "off"],
    [
      "Remote log forwarding",
      `${logging.remote_targets} destination(s) · ${logging.remote_dropped} dropped · ${logging.remote_send_errors} send errors (this process)`,
    ],
  ]);
}

function renderIntegrity(status) {
  const target = document.getElementById("integrity-badge");
  target.replaceChildren();
  const last = status.last_verify;
  if (!last) {
    target.append(badge("Not verified yet", "secondary"));
  } else if (last.ok) {
    target.append(badge(`Chain intact · ${last.checked} events checked`, "success"));
  } else {
    target.append(badge(`Chain broken at event #${last.first_break_id}: ${last.reason}`, "danger"));
  }
  const retention = status.retention_days ? `${status.retention_days} days` : "forever";
  let pruning = "–";
  if (status.expired) {
    pruning = `${status.expired} event(s) past retention`;
    if (status.prune_held_back_by_forwarding) pruning += "; waiting for syslog forwarding";
    if (status.prune_held_back_by_sealing) pruning += "; waiting for sealing";
  }
  definitionList(document.getElementById("integrity"), [
    ["Sealed up to", status.sealed_up_to ? `#${status.sealed_up_to}` : "–"],
    ["Not sealed yet", String(status.unsealed)],
    ["Chain starts after", status.anchor_id ? `#${status.anchor_id} (pruned)` : "the first event"],
    ["Retention", retention],
    ["Pruning", pruning],
    ["Last verified", last ? fmtTime(last.verified_at) : "–"],
  ]);
}

// --------------------------------------------------------------------------
// Destinations
// --------------------------------------------------------------------------

function forwardsText(dest) {
  const parts = [];
  if (dest.forward_audit) {
    let text = dest.audit_failures_only ? "audit failures" : "audit events";
    if (dest.audit_categories.length) text += ` (${dest.audit_categories.join(", ")})`;
    parts.push(text);
  }
  if (dest.forward_logs) parts.push(`logs ≥ ${dest.log_min_level}`);
  return parts.join(" + ") || "nothing";
}

function statusCell(dest) {
  const td = document.createElement("td");
  if (!dest.enabled) {
    td.append(badge("disabled", "secondary"));
  } else if (dest.failing) {
    td.append(badge(`failing (${dest.consecutive_failures})`, "danger"));
    if (dest.last_error) {
      const small = document.createElement("div");
      small.className = "small text-body-secondary text-break";
      small.textContent = dest.last_error;
      td.append(small);
    }
  } else {
    td.append(badge("ok", "success"));
  }
  return td;
}

function actionButton(label, tone, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `btn btn-sm btn-outline-${tone}`;
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

function renderDestinations() {
  body.replaceChildren();
  for (const dest of state.destinations) {
    const row = document.createElement("tr");
    const collector = `${dest.protocol.toUpperCase()} ${dest.host}:${dest.port} · ${dest.format} · ${dest.facility}`;
    row.append(
      cell(dest.name),
      cell(collector),
      cell(forwardsText(dest)),
      statusCell(dest),
      cell(dest.forward_audit ? String(dest.lag) : "–"),
      cell(fmtTime(dest.last_success_at)),
    );
    const actions = document.createElement("td");
    actions.className = "text-end text-nowrap";
    if (canWrite) {
      const group = document.createElement("div");
      group.className = "btn-group";
      group.append(
        actionButton("Test", "secondary", () => testDestination(dest)),
        actionButton("Edit", "primary", () => openForm(dest)),
        actionButton("Delete", "danger", () => deleteDestination(dest)),
      );
      actions.append(group);
    }
    row.append(actions);
    body.append(row);
  }
  if (!state.destinations.length) {
    const row = document.createElement("tr");
    const td = cell("No remote syslog destinations. Audit events stay in the local database and journal.");
    td.colSpan = 7;
    td.className = "text-body-secondary text-center";
    row.append(td);
    body.append(row);
  }
}

async function testDestination(dest) {
  try {
    const result = await apiFetch(`/api/v1/syslog/destinations/${dest.id}/test`, { method: "POST" });
    if (result.ok) showNotice(`${dest.name}: ${result.message}`);
    else showError(`${dest.name}: ${result.message}`);
  } catch (error) {
    showError(error.message);
  }
}

function deleteDestination(dest) {
  confirmDialog(
    "Delete destination",
    [`Stop forwarding to ${dest.name} (${dest.host}:${dest.port})?`],
    ["Events not yet forwarded to it will not be sent."],
    async () => {
      await apiFetch(`/api/v1/syslog/destinations/${dest.id}`, { method: "DELETE" });
      await refresh();
    },
  );
}

// --------------------------------------------------------------------------
// Form
// --------------------------------------------------------------------------

function field(id) {
  return document.getElementById(id);
}

function updateCaVisibility() {
  const tls = field("dest-protocol").value === "tls";
  field("dest-ca-group").classList.toggle("d-none", !tls);
}

function openForm(dest) {
  state.editing = dest || null;
  field("destination-form-title").textContent = dest ? `Edit ${dest.name}` : "New destination";
  field("dest-name").value = dest ? dest.name : "";
  field("dest-host").value = dest ? dest.host : "";
  field("dest-protocol").value = dest ? dest.protocol : "tls";
  field("dest-port").value = dest ? String(dest.port) : "";
  field("dest-format").value = dest ? dest.format : "rfc5424";
  field("dest-facility").value = dest ? dest.facility : "local0";
  field("dest-ca").value = "";
  field("dest-ca-hint").textContent =
    dest && dest.has_ca_pem ? "A CA certificate is stored; paste a new one to replace it." : "";
  field("dest-enabled").checked = dest ? dest.enabled : true;
  field("dest-audit").checked = dest ? dest.forward_audit : true;
  field("dest-failures").checked = dest ? dest.audit_failures_only : false;
  field("dest-logs").checked = dest ? dest.forward_logs : false;
  field("dest-level").value = dest ? dest.log_min_level : "WARNING";
  field("dest-backfill").checked = false;
  field("dest-backfill-group").classList.toggle("d-none", Boolean(dest));
  const selected = new Set(dest ? dest.audit_categories : []);
  for (const option of field("dest-categories").options) option.selected = selected.has(option.value);
  updateCaVisibility();
  form.classList.remove("d-none");
  field("dest-name").focus();
}

function formPayload() {
  const port = field("dest-port").value.trim();
  const payload = {
    name: field("dest-name").value.trim(),
    host: field("dest-host").value.trim(),
    protocol: field("dest-protocol").value,
    port: port ? Number(port) : null,
    format: field("dest-format").value,
    facility: field("dest-facility").value,
    enabled: field("dest-enabled").checked,
    forward_audit: field("dest-audit").checked,
    audit_failures_only: field("dest-failures").checked,
    audit_categories: Array.from(field("dest-categories").selectedOptions, (o) => o.value),
    forward_logs: field("dest-logs").checked,
    log_min_level: field("dest-level").value,
  };
  const ca = field("dest-ca").value.trim();
  if (ca) payload.ca_pem = ca;
  else if (payload.protocol !== "tls") payload.ca_pem = null;
  if (!state.editing) payload.backfill = field("dest-backfill").checked;
  return payload;
}

async function saveForm(event) {
  event.preventDefault();
  const payload = formPayload();
  const editing = state.editing;
  try {
    await withConfirmation("Unencrypted syslog", (confirm) =>
      editing
        ? apiFetch(`/api/v1/syslog/destinations/${editing.id}`, {
            method: "PATCH",
            body: { ...payload, confirm },
          })
        : apiFetch("/api/v1/syslog/destinations", { method: "POST", body: { ...payload, confirm } }),
    );
    form.classList.add("d-none");
    showNotice(`Saved ${payload.name}.`);
    await refresh();
  } catch (error) {
    showError(error.message);
  }
}

async function loadCategories() {
  const facets = await apiFetch("/api/v1/audit/facets").catch(() => ({ categories: [] }));
  const select = field("dest-categories");
  select.replaceChildren();
  for (const category of facets.categories) {
    const option = document.createElement("option");
    option.value = category;
    option.textContent = category;
    select.append(option);
  }
}

// --------------------------------------------------------------------------

async function refresh() {
  try {
    const [status, integrity] = await Promise.all([
      apiFetch("/api/v1/syslog/status"),
      apiFetch("/api/v1/audit/integrity").catch(() => null),
    ]);
    state.destinations = status.destinations;
    workerBadge.className = `badge text-bg-${status.worker_online ? "success" : "danger"}`;
    workerBadge.textContent = status.worker_online
      ? "Worker online · sealing and forwarding"
      : "Worker offline: audit events are not sealed or forwarded until it starts";
    renderLocal(status.logging);
    renderDestinations();
    if (integrity) renderIntegrity(integrity);
  } catch (error) {
    showError(`Could not load logging status: ${error.message}`);
  }
}

document.getElementById("verify-chain").addEventListener("click", async () => {
  try {
    const result = await apiFetch("/api/v1/audit/verify", { method: "POST" });
    if (result.ok) showNotice(`Audit chain intact: ${result.checked} sealed events checked.`);
    else showError(`Audit chain broken at event #${result.first_break_id}: ${result.reason}`);
    await refresh();
  } catch (error) {
    showError(error.message);
  }
});

if (canWrite) {
  document.getElementById("add-destination").addEventListener("click", () => openForm(null));
  document.getElementById("destination-cancel").addEventListener("click", () => form.classList.add("d-none"));
  field("dest-protocol").addEventListener("change", updateCaVisibility);
  form.addEventListener("submit", saveForm);
  loadCategories();
}

refresh();
