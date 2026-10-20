import { getJson } from "./api.js";
import { formatBytes, formatDuration, formatPercent } from "./format.js";

const REFRESH_MS = 5000;

function setText(id, text) {
  document.getElementById(id).textContent = text;
}

function setBar(id, percent) {
  const clamped = Math.min(100, Math.max(0, Number(percent) || 0));
  document.getElementById(`${id}-bar`).style.width = `${clamped}%`;
  document.getElementById(`${id}-progress`).setAttribute("aria-valuenow", String(clamped));
}

function renderDisks(disks) {
  const list = document.getElementById("disk-list");
  list.replaceChildren();
  for (const disk of disks) {
    const item = document.createElement("li");
    item.className = "list-group-item";

    const head = document.createElement("div");
    head.className = "d-flex justify-content-between mb-1";
    const path = document.createElement("code");
    path.textContent = disk.path;
    const usage = document.createElement("span");
    usage.textContent = `${formatBytes(disk.used)} of ${formatBytes(disk.total)} (${formatPercent(disk.percent)})`;
    head.append(path, usage);

    const progress = document.createElement("div");
    progress.className = "progress";
    progress.setAttribute("role", "progressbar");
    progress.setAttribute("aria-label", `Disk usage for ${disk.path}`);
    progress.setAttribute("aria-valuemin", "0");
    progress.setAttribute("aria-valuemax", "100");
    progress.setAttribute("aria-valuenow", String(disk.percent));
    const bar = document.createElement("div");
    bar.className = "progress-bar";
    bar.style.width = `${Math.min(100, Math.max(0, disk.percent))}%`;
    progress.append(bar);

    item.append(head, progress);
    list.append(item);
  }
}

function renderSummary(summary) {
  setText("host-name", summary.host.hostname);
  setText("host-os", summary.host.os);
  setText("host-kernel", summary.host.kernel);
  setText("host-uptime", formatDuration(summary.host.uptime_seconds));

  setText("cpu-percent", formatPercent(summary.cpu.percent));
  setBar("cpu", summary.cpu.percent);
  setText("cpu-load", summary.cpu.load_average ? summary.cpu.load_average.join(" / ") : "n/a");
  setText("cpu-cores", summary.cpu.logical_cores ?? "?");

  const mem = summary.memory;
  setText("mem-text", `${formatBytes(mem.used)} of ${formatBytes(mem.total)} (${formatPercent(mem.percent)})`);
  setBar("mem", mem.percent);
  setText(
    "swap-text",
    mem.swap_total ? `${formatBytes(mem.swap_used)} of ${formatBytes(mem.swap_total)} (${formatPercent(mem.swap_percent)})` : "none",
  );
  setBar("swap", mem.swap_percent);

  setText("net-sent", formatBytes(summary.network.bytes_sent));
  setText("net-recv", formatBytes(summary.network.bytes_received));
  renderDisks(summary.disks);
}

function renderHealth(health) {
  const badge = document.getElementById("health-badge");
  const styles = { ok: "text-bg-success", degraded: "text-bg-warning", unhealthy: "text-bg-danger" };
  badge.className = `badge ${styles[health.status] ?? "text-bg-secondary"}`;
  badge.textContent = health.status;
  if (health.status === "degraded") {
    badge.title = "Database schema is not up to date. Run: alembic upgrade head";
  } else {
    badge.removeAttribute("title");
  }
}

async function refresh() {
  const banner = document.getElementById("error-banner");
  try {
    const [summary, health] = await Promise.all([
      getJson("/api/v1/system/summary"),
      getJson("/api/v1/health", { allowStatuses: [503] }),
    ]);
    renderSummary(summary);
    renderHealth(health);
    banner.classList.add("d-none");
    setText("updated-at", `Updated ${new Date().toLocaleTimeString()}`);
  } catch (error) {
    banner.textContent = `Could not refresh: ${error.message}`;
    banner.classList.remove("d-none");
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
