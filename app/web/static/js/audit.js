import { apiFetch } from "./api.js";

const body = document.getElementById("audit-body");
const banner = document.getElementById("error-banner");
const total = document.getElementById("audit-total");
const form = document.getElementById("audit-filter");
const LIMIT = 50;
const FILTERS = ["q", "category", "outcome", "actor", "event", "ip", "request_id"];
let offset = 0;
let current = null;

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text;
  return td;
}

function outcomeBadge(outcome) {
  const span = document.createElement("span");
  span.className = `badge ${outcome === "success" ? "text-bg-success" : "text-bg-danger"}`;
  span.textContent = outcome;
  return span;
}

function fmtTime(value) {
  if (!value) return "–";
  return String(value).replace("T", " ").replace("Z", "").slice(0, 19);
}

function targetText(event) {
  return event.target_type ? `${event.target_type}:${event.target_id ?? "?"}` : "–";
}

function showDetails(event) {
  current = event;
  document.getElementById("event-modal-title").textContent = `#${event.id} · ${event.event}`;
  const fields = document.getElementById("event-modal-fields");
  fields.replaceChildren();
  const pairs = [
    ["Time (UTC)", fmtTime(event.created_at)],
    ["Category", event.category],
    ["Outcome", event.outcome],
    ["Actor", event.actor_username || "–"],
    ["Target", targetText(event)],
    ["IP address", event.ip_address || "–"],
    ["User agent", event.user_agent || "–"],
    ["Request id", event.request_id || "–"],
    ["Chain hash", event.chain_hash || "not sealed yet"],
  ];
  for (const [label, value] of pairs) {
    const dt = document.createElement("dt");
    dt.className = "col-4";
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.className = "col-8 text-break font-monospace";
    dd.textContent = value;
    fields.append(dt, dd);
  }
  document.getElementById("event-modal-details").textContent = event.details
    ? JSON.stringify(event.details, null, 2)
    : "(none)";
  document.getElementById("event-modal-request").disabled = !event.request_id;
  bootstrap.Modal.getOrCreateInstance(document.getElementById("event-modal")).show();
}

function renderPage(page) {
  body.replaceChildren();
  total.textContent = `${page.total} event${page.total === 1 ? "" : "s"}`;
  for (const event of page.events) {
    const row = document.createElement("tr");
    row.className = "cursor-pointer";
    row.tabIndex = 0;
    row.append(cell(fmtTime(event.created_at)), cell(event.category), cell(event.event));
    const outcomeCell = document.createElement("td");
    outcomeCell.append(outcomeBadge(event.outcome));
    row.append(outcomeCell);
    row.append(
      cell(event.actor_username || "–"),
      cell(targetText(event)),
      cell(event.ip_address || "–"),
    );
    const sealed = cell(event.sealed ? "✓" : "…");
    sealed.className = "text-end";
    sealed.title = event.sealed ? "Sealed into the hash chain" : "Waiting for the worker to seal it";
    row.append(sealed);
    row.addEventListener("click", () => showDetails(event));
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter") showDetails(event);
    });
    body.append(row);
  }
  if (!page.events.length) {
    const row = document.createElement("tr");
    const td = cell("No events match the filter.");
    td.colSpan = 8;
    td.className = "text-body-secondary text-center";
    row.append(td);
    body.append(row);
  }
}

/** datetime-local values are wall-clock text; the page treats them as UTC. */
function utc(value) {
  return value ? `${value}${value.length === 16 ? ":00" : ""}Z` : "";
}

function filterParams() {
  const data = new FormData(form);
  const params = new URLSearchParams();
  for (const name of FILTERS) {
    const value = String(data.get(name) || "").trim();
    if (value) params.set(name, value);
  }
  if (data.get("since")) params.set("since", utc(String(data.get("since"))));
  if (data.get("until")) params.set("until", utc(String(data.get("until"))));
  return params;
}

function updateExportLinks(params) {
  for (const format of ["csv", "jsonl"]) {
    const exportParams = new URLSearchParams(params);
    exportParams.set("format", format);
    document.getElementById(`export-${format}`).href =
      `/api/v1/audit/events/export?${exportParams.toString()}`;
  }
}

async function refresh() {
  const params = filterParams();
  updateExportLinks(params);
  params.set("limit", String(LIMIT));
  params.set("offset", String(offset));
  try {
    const page = await apiFetch(`/api/v1/audit/events?${params.toString()}`);
    banner.classList.add("d-none");
    renderPage(page);
    document.getElementById("audit-prev").disabled = offset <= 0;
    document.getElementById("audit-next").disabled = offset + page.events.length >= page.total;
  } catch (error) {
    banner.textContent = `Could not load audit log: ${error.message}`;
    banner.classList.remove("d-none");
  }
}

async function loadFacets() {
  try {
    const facets = await apiFetch("/api/v1/audit/facets");
    const select = document.getElementById("af-category");
    for (const category of facets.categories) {
      const option = document.createElement("option");
      option.textContent = category;
      select.append(option);
    }
    const list = document.getElementById("af-events");
    for (const name of facets.events) {
      const option = document.createElement("option");
      option.value = name;
      list.append(option);
    }
  } catch (_ignored) {
    // Filters still work as free text without the suggestions.
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  offset = 0;
  refresh();
});

form.addEventListener("reset", () => {
  offset = 0;
  setTimeout(refresh, 0);
});

document.getElementById("audit-prev").addEventListener("click", () => {
  offset = Math.max(0, offset - LIMIT);
  refresh();
});

document.getElementById("audit-next").addEventListener("click", () => {
  offset += LIMIT;
  refresh();
});

document.getElementById("event-modal-request").addEventListener("click", () => {
  if (!current || !current.request_id) return;
  form.reset();
  document.getElementById("af-request").value = current.request_id;
  bootstrap.Modal.getOrCreateInstance(document.getElementById("event-modal")).hide();
  // The reset handler refreshes on the next tick, after the value is set.
});

loadFacets();
refresh();
