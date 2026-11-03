import { getJson } from "./api.js";
import { formatBytes, formatPercent } from "./format.js";

const REFRESH_MS = 15000;
const SVG_NS = "http://www.w3.org/2000/svg";
const banner = document.getElementById("error-banner");
const workerBanner = document.getElementById("worker-banner");
const rangeSelect = document.getElementById("history-range");
const sortCpu = document.getElementById("sort-cpu");
const sortMemory = document.getElementById("sort-memory");

const state = { sort: "cpu" };

const STATUS_KIND = { ok: "success", warning: "warning", critical: "danger", unknown: "secondary" };

function showError(message) {
  banner.textContent = message;
  banner.classList.remove("d-none");
}

function hideError() {
  banner.classList.add("d-none");
}

function when(value) {
  return value ? new Date(`${value}${value.endsWith("Z") ? "" : "Z"}`).toLocaleString() : "–";
}

function badge(text, kind) {
  const span = document.createElement("span");
  span.className = `badge text-bg-${kind}`;
  span.textContent = text;
  return span;
}

function emptyItem(text) {
  const li = document.createElement("li");
  li.className = "list-group-item text-body-secondary";
  li.textContent = text;
  return li;
}

function checkItem(title, status, summary, checkedAt) {
  const li = document.createElement("li");
  li.className = "list-group-item d-flex flex-wrap justify-content-between align-items-center gap-2";
  const left = document.createElement("div");
  const name = document.createElement("code");
  name.textContent = title;
  const detail = document.createElement("div");
  detail.className = "text-body-secondary small";
  detail.textContent = summary;
  left.append(name, detail);
  const right = document.createElement("div");
  right.className = "text-end";
  const time = document.createElement("div");
  time.className = "text-body-secondary small";
  time.textContent = checkedAt ? `checked ${when(checkedAt)}` : "";
  right.append(badge(status, STATUS_KIND[status] ?? "secondary"), time);
  li.append(left, right);
  return li;
}

// --------------------------------------------------------------------------
// Charts
// --------------------------------------------------------------------------

function sparkline(values, { max = null } = {}) {
  const width = 300;
  const height = 60;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "monitor-sparkline");
  svg.setAttribute("role", "img");
  const numbers = values.filter((v) => v !== null && Number.isFinite(v));
  if (numbers.length < 2) {
    return svg;
  }
  const top = max ?? Math.max(...numbers, 1e-9);
  const step = width / (values.length - 1);
  const points = [];
  values.forEach((value, index) => {
    if (value === null || !Number.isFinite(value)) return;
    const y = height - Math.min(1, Math.max(0, value / top)) * (height - 4) - 2;
    points.push(`${(index * step).toFixed(1)},${y.toFixed(1)}`);
  });
  const line = document.createElementNS(SVG_NS, "polyline");
  line.setAttribute("points", points.join(" "));
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "currentColor");
  line.setAttribute("stroke-width", "1.5");
  line.setAttribute("vector-effect", "non-scaling-stroke");
  svg.append(line);
  return svg;
}

function chartCard(title, values, latestText, options = {}) {
  const col = document.createElement("div");
  col.className = "col-12 col-md-6 col-xl-4";
  const head = document.createElement("div");
  head.className = "d-flex justify-content-between small mb-1";
  const label = document.createElement("span");
  label.textContent = title;
  const latest = document.createElement("strong");
  latest.textContent = latestText;
  head.append(label, latest);
  const chart = sparkline(values, options);
  chart.setAttribute("aria-label", `${title} history`);
  const wrap = document.createElement("div");
  wrap.className = "monitor-chart text-primary";
  wrap.append(chart);
  col.append(head, wrap);
  return col;
}

function last(values) {
  for (let i = values.length - 1; i >= 0; i -= 1) {
    if (values[i] !== null && Number.isFinite(values[i])) return values[i];
  }
  return null;
}

function rate(value) {
  return value === null ? "–" : `${formatBytes(value)}/s`;
}

function renderHistory(history) {
  const grid = document.getElementById("chart-grid");
  const empty = document.getElementById("history-empty");
  document.getElementById("sample-interval").textContent = String(history.sample_seconds);
  document.getElementById("history-note").textContent =
    `${history.points.length} points, ${Math.round(history.bucket_seconds / 60)} min buckets`;
  grid.replaceChildren();
  if (!history.points.length) {
    empty.classList.remove("d-none");
    return;
  }
  empty.classList.add("d-none");
  const pick = (key) => history.points.map((p) => p[key]);
  const cpu = pick("cpu_percent");
  const memory = pick("memory_percent");
  const swap = pick("swap_percent");
  const load = pick("load_per_core");
  const sent = pick("net_sent_bps");
  const recv = pick("net_recv_bps");
  grid.append(
    chartCard("CPU", cpu, formatPercent(last(cpu) ?? 0), { max: 100 }),
    chartCard("Memory", memory, formatPercent(last(memory) ?? 0), { max: 100 }),
    chartCard("Swap", swap, formatPercent(last(swap) ?? 0), { max: 100 }),
    chartCard("Load per core", load, last(load) === null ? "–" : last(load).toFixed(2)),
    chartCard("Network sent", sent, rate(last(sent))),
    chartCard("Network received", recv, rate(last(recv))),
  );
  const paths = new Set();
  history.points.forEach((p) => Object.keys(p.disks).forEach((path) => paths.add(path)));
  for (const path of [...paths].sort()) {
    const values = history.points.map((p) => (path in p.disks ? p.disks[path] : null));
    grid.append(chartCard(`Disk ${path}`, values, formatPercent(last(values) ?? 0), { max: 100 }));
  }
}

// --------------------------------------------------------------------------
// Checks, alerts and processes
// --------------------------------------------------------------------------

function renderAlertBadge(summary) {
  const el = document.getElementById("alert-badge");
  const { firing, critical } = summary.counts;
  const total = firing + summary.synthetic.length;
  const kind = critical || summary.synthetic.length ? "danger" : firing ? "warning" : "success";
  el.className = `badge text-bg-${kind} text-decoration-none`;
  el.textContent = total ? `Alerts: ${total} firing` : "Alerts: none";
  if (summary.worker_online) {
    workerBanner.classList.add("d-none");
  } else {
    workerBanner.textContent = summary.synthetic.map((s) => s.message).join(" ");
    workerBanner.classList.remove("d-none");
  }
}

function renderChecks(checks) {
  const services = document.getElementById("service-list");
  const raid = document.getElementById("raid-list");
  const smart = document.getElementById("smart-list");
  const byKind = (kind) => checks.filter((c) => c.kind === kind);
  services.replaceChildren(
    ...byKind("service").map((c) => checkItem(c.target, c.status, c.summary, c.checked_at)),
  );
  if (!services.children.length) services.append(emptyItem("No service checks yet."));
  raid.replaceChildren(
    ...byKind("raid").map((c) => checkItem(c.target, c.status, c.summary, c.checked_at)),
  );
  if (!raid.children.length) raid.append(emptyItem("No software RAID arrays found."));
  smart.replaceChildren(
    ...byKind("helper").map((c) => checkItem("privileged helper", c.status, c.summary, c.checked_at)),
    ...byKind("smart").map((c) => {
      const extra = [c.details.model, c.details.temperature != null ? `${c.details.temperature} °C` : null]
        .filter(Boolean)
        .join(" · ");
      return checkItem(`/dev/${c.target}`, c.status, extra ? `${c.summary} · ${extra}` : c.summary, c.checked_at);
    }),
  );
  if (!smart.children.length) {
    smart.append(emptyItem("SMART needs the privileged helper (NAS_HELPER_ENABLED=true)."));
  }
}

function renderProcesses(rows) {
  const body = document.getElementById("process-body");
  body.replaceChildren(
    ...rows.map((row) => {
      const tr = document.createElement("tr");
      const values = [
        String(row.pid),
        row.name,
        row.username ?? "–",
        row.status,
        formatPercent(row.cpu_percent),
        `${formatBytes(row.memory_rss)} (${formatPercent(row.memory_percent)})`,
      ];
      values.forEach((value, index) => {
        const td = document.createElement("td");
        if (index >= 4) td.className = "text-end";
        td.textContent = value;
        tr.append(td);
      });
      return tr;
    }),
  );
}

async function refresh() {
  try {
    const [overview, history, processes] = await Promise.all([
      getJson("/api/v1/monitoring/overview"),
      getJson(`/api/v1/monitoring/history?range=${encodeURIComponent(rangeSelect.value)}`),
      getJson(`/api/v1/monitoring/processes?sort=${state.sort}`),
    ]);
    hideError();
    renderAlertBadge(overview.alerts);
    renderChecks(overview.checks);
    renderHistory(history);
    renderProcesses(processes);
  } catch (error) {
    showError(error.message);
  }
}

function setSort(sort) {
  state.sort = sort;
  sortCpu.classList.toggle("active", sort === "cpu");
  sortMemory.classList.toggle("active", sort === "memory");
  refresh();
}

sortCpu.addEventListener("click", () => setSort("cpu"));
sortMemory.addEventListener("click", () => setSort("memory"));
rangeSelect.addEventListener("change", refresh);
refresh();
setInterval(refresh, REFRESH_MS);
