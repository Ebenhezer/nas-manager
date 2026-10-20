import { apiFetch } from "./api.js";

const body = document.getElementById("audit-body");
const banner = document.getElementById("error-banner");
const total = document.getElementById("audit-total");
const form = document.getElementById("audit-filter");
const LIMIT = 50;
let offset = 0;

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

function renderPage(page) {
  body.replaceChildren();
  total.textContent = `${page.total} event${page.total === 1 ? "" : "s"}`;
  for (const event of page.events) {
    const row = document.createElement("tr");
    const target = event.target_type
      ? `${event.target_type}:${event.target_id ?? "?"}`
      : "–";
    row.append(
      cell(fmtTime(event.created_at)),
      cell(event.category),
      cell(event.event),
    );
    const outcomeCell = document.createElement("td");
    outcomeCell.append(outcomeBadge(event.outcome));
    row.append(outcomeCell);
    row.append(cell(event.actor_username || "–"), cell(target), cell(event.ip_address || "–"));
    body.append(row);
  }
  if (!page.events.length) {
    const row = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 7;
    td.className = "text-body-secondary text-center";
    td.textContent = "No events match the filter.";
    row.append(td);
    body.append(row);
  }
}

function filters() {
  const data = new FormData(form);
  const params = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) });
  if (data.get("category")) params.set("category", String(data.get("category")));
  if (data.get("outcome")) params.set("outcome", String(data.get("outcome")));
  if (data.get("actor")) params.set("actor", String(data.get("actor")));
  return params;
}

async function refresh() {
  try {
    const page = await apiFetch(`/api/v1/audit/events?${filters().toString()}`);
    renderPage(page);
    document.getElementById("audit-prev").disabled = offset <= 0;
    document.getElementById("audit-next").disabled = offset + page.events.length >= page.total;
  } catch (error) {
    banner.textContent = `Could not load audit log: ${error.message}`;
    banner.classList.remove("d-none");
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  offset = 0;
  refresh();
});

document.getElementById("audit-prev").addEventListener("click", () => {
  offset = Math.max(0, offset - LIMIT);
  refresh();
});

document.getElementById("audit-next").addEventListener("click", () => {
  offset += LIMIT;
  refresh();
});

refresh();