import { apiFetch, getJson } from "./api.js";

const REFRESH_MS = 15000;
const root = document.getElementById("alerts-root");
const canWrite = root.dataset.canWrite === "true";
const banner = document.getElementById("error-banner");
const infoBanner = document.getElementById("info-banner");

const state = { rules: [], channels: [], editingRule: null, editingChannel: null };

const THRESHOLD_METRICS = new Set([
  "cpu_percent",
  "memory_percent",
  "swap_percent",
  "load_per_core",
  "disk_percent",
]);
const METRIC_LABELS = {
  cpu_percent: "CPU",
  memory_percent: "Memory",
  swap_percent: "Swap",
  load_per_core: "Load per core",
  disk_percent: "Disk usage",
  raid_degraded: "RAID degraded",
  raid_failed_member: "RAID member failed",
  smart_failed: "SMART failed",
  service_down: "Service down",
  helper_unavailable: "Helper unavailable",
};
const SEVERITY_KIND = { warning: "warning", critical: "danger" };
const STATE_KIND = { firing: "danger", pending: "secondary", resolved: "success" };

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

function when(value) {
  return value ? new Date(`${value}${value.endsWith("Z") ? "" : "Z"}`).toLocaleString() : "–";
}

function cell(content, className = "") {
  const td = document.createElement("td");
  if (className) td.className = className;
  if (content instanceof Node) td.append(content);
  else td.textContent = content;
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

function emptyRow(body, columns, text) {
  const tr = document.createElement("tr");
  const td = cell(text, "text-body-secondary");
  td.colSpan = columns;
  tr.append(td);
  body.append(tr);
}

function duration(seconds) {
  if (!seconds) return "immediately";
  if (seconds % 3600 === 0) return `${seconds / 3600} h`;
  if (seconds % 60 === 0) return `${seconds / 60} min`;
  return `${seconds} s`;
}

// --------------------------------------------------------------------------
// Alerts
// --------------------------------------------------------------------------

function renderSummary(summary) {
  const el = document.getElementById("summary-badge");
  const { firing, pending, critical } = summary.counts;
  const kind = critical || summary.synthetic.length ? "danger" : firing ? "warning" : "success";
  el.className = `badge text-bg-${kind}`;
  el.textContent = `${firing} firing · ${pending} pending`;
  const list = document.getElementById("synthetic-list");
  list.replaceChildren(
    ...summary.synthetic.map((item) => {
      const div = document.createElement("div");
      div.className = `alert alert-${SEVERITY_KIND[item.severity] ?? "warning"}`;
      div.setAttribute("role", "alert");
      div.textContent = item.message;
      return div;
    }),
  );
}

function renderActive(page) {
  const body = document.getElementById("active-body");
  body.replaceChildren();
  if (!page.items.length) {
    emptyRow(body, canWrite ? 6 : 5, "No active alerts.");
    return;
  }
  for (const alert of page.items) {
    const tr = document.createElement("tr");
    const message = document.createElement("div");
    message.textContent = alert.message;
    const rule = document.createElement("div");
    rule.className = "text-body-secondary small";
    rule.textContent = alert.rule_name;
    const text = document.createElement("div");
    text.append(message, rule);
    tr.append(
      cell(badge(alert.severity, SEVERITY_KIND[alert.severity] ?? "secondary")),
      cell(badge(alert.state, STATE_KIND[alert.state] ?? "secondary")),
      cell(text),
      cell(when(alert.firing_at ?? alert.started_at)),
      cell(alert.acknowledged_at ? `${alert.acknowledged_by ?? "?"}, ${when(alert.acknowledged_at)}` : "–"),
    );
    if (canWrite) {
      const actions = cell("", "text-end");
      if (alert.state === "firing" && !alert.acknowledged_at) {
        actions.append(
          button("Acknowledge", "secondary", async () => {
            await apiFetch(`/api/v1/alerts/${alert.id}/acknowledge`, { method: "POST" });
            await refresh();
          }),
        );
      }
      tr.append(actions);
    }
    body.append(tr);
  }
}

function renderHistory(page) {
  const body = document.getElementById("history-body");
  body.replaceChildren();
  if (!page.items.length) {
    emptyRow(body, 4, "Nothing resolved yet.");
    return;
  }
  for (const alert of page.items) {
    const tr = document.createElement("tr");
    tr.append(
      cell(badge(alert.severity, SEVERITY_KIND[alert.severity] ?? "secondary")),
      cell(alert.message),
      cell(when(alert.firing_at)),
      cell(when(alert.resolved_at)),
    );
    body.append(tr);
  }
}

// --------------------------------------------------------------------------
// Rules
// --------------------------------------------------------------------------

function ruleCondition(rule) {
  const label = METRIC_LABELS[rule.metric] ?? rule.metric;
  const target = rule.target ? ` (${rule.target})` : "";
  if (!THRESHOLD_METRICS.has(rule.metric)) return `${label}${target}`;
  const unit = rule.metric === "load_per_core" ? "" : " %";
  return `${label}${target} ${rule.comparison === "lt" ? "<" : ">"} ${rule.threshold}${unit}`;
}

function renderRules() {
  const body = document.getElementById("rules-body");
  body.replaceChildren();
  for (const rule of state.rules) {
    const tr = document.createElement("tr");
    const name = document.createElement("span");
    name.textContent = rule.name;
    const nameCell = cell(name);
    if (rule.builtin) nameCell.append(" ", badge("built-in", "light"));
    tr.append(
      nameCell,
      cell(ruleCondition(rule)),
      cell(duration(rule.duration_seconds)),
      cell(badge(rule.severity, SEVERITY_KIND[rule.severity] ?? "secondary")),
      cell(
        rule.enabled
          ? badge(rule.notify ? "enabled" : "enabled, silent", "success")
          : badge("disabled", "secondary"),
      ),
      cell(String(rule.open_alerts)),
    );
    if (canWrite) {
      const actions = cell("", "text-end");
      actions.append(
        button(rule.enabled ? "Disable" : "Enable", "secondary", async () => {
          await apiFetch(`/api/v1/alert-rules/${rule.id}`, {
            method: "PATCH",
            body: { enabled: !rule.enabled },
          });
          await refresh();
        }),
        button("Edit", "primary", async () => openRuleForm(rule)),
      );
      if (!rule.builtin) {
        actions.append(
          button("Delete", "danger", async () => {
            if (!window.confirm(`Delete the rule "${rule.name}" and its alert history?`)) return;
            await apiFetch(`/api/v1/alert-rules/${rule.id}`, { method: "DELETE" });
            await refresh();
          }),
        );
      }
      tr.append(actions);
    }
    body.append(tr);
  }
}

function syncRuleFields() {
  const metric = document.getElementById("rule-metric").value;
  const threshold = THRESHOLD_METRICS.has(metric);
  document.getElementById("rule-threshold").disabled = !threshold;
  document.getElementById("rule-comparison").disabled = !threshold;
}

function openRuleForm(rule) {
  state.editingRule = rule;
  const form = document.getElementById("rule-form");
  document.getElementById("rule-form-title").textContent = rule ? `Edit rule "${rule.name}"` : "New rule";
  document.getElementById("rule-name").value = rule ? rule.name : "";
  document.getElementById("rule-metric").value = rule ? rule.metric : "disk_percent";
  document.getElementById("rule-metric").disabled = Boolean(rule);
  document.getElementById("rule-target").value = rule?.target ?? "";
  document.getElementById("rule-comparison").value = rule ? rule.comparison : "gt";
  document.getElementById("rule-threshold").value = rule?.threshold ?? "";
  document.getElementById("rule-duration").value = rule ? rule.duration_seconds : 300;
  document.getElementById("rule-severity").value = rule ? rule.severity : "warning";
  document.getElementById("rule-enabled").checked = rule ? rule.enabled : true;
  document.getElementById("rule-notify").checked = rule ? rule.notify : true;
  syncRuleFields();
  form.classList.remove("d-none");
  document.getElementById("rule-name").focus();
}

async function submitRule(event) {
  event.preventDefault();
  hideError();
  const metric = document.getElementById("rule-metric").value;
  const thresholdText = document.getElementById("rule-threshold").value;
  const body = {
    name: document.getElementById("rule-name").value,
    target: document.getElementById("rule-target").value,
    duration_seconds: Number(document.getElementById("rule-duration").value || 0),
    severity: document.getElementById("rule-severity").value,
    enabled: document.getElementById("rule-enabled").checked,
    notify: document.getElementById("rule-notify").checked,
  };
  if (THRESHOLD_METRICS.has(metric)) {
    body.comparison = document.getElementById("rule-comparison").value;
    body.threshold = thresholdText === "" ? null : Number(thresholdText);
  }
  try {
    if (state.editingRule) {
      await apiFetch(`/api/v1/alert-rules/${state.editingRule.id}`, { method: "PATCH", body });
    } else {
      await apiFetch("/api/v1/alert-rules", { method: "POST", body: { ...body, metric } });
    }
    document.getElementById("rule-form").classList.add("d-none");
    await refresh();
  } catch (error) {
    showError(error.message);
  }
}

// --------------------------------------------------------------------------
// Channels
// --------------------------------------------------------------------------

function channelDestination(channel) {
  if (channel.kind === "email") {
    return `${(channel.config.to ?? []).join(", ")} via ${channel.config.host}:${channel.config.port}`;
  }
  return channel.config.url ?? "";
}

function renderChannels() {
  const body = document.getElementById("channels-body");
  body.replaceChildren();
  if (!state.channels.length) {
    emptyRow(body, canWrite ? 6 : 5, "No channels: alerts are shown here only.");
    return;
  }
  for (const channel of state.channels) {
    const tr = document.createElement("tr");
    const name = cell(channel.name);
    if (!channel.enabled) name.append(" ", badge("disabled", "secondary"));
    if (channel.allow_insecure) name.append(" ", badge("unencrypted", "warning"));
    const test = channel.last_test_at
      ? `${channel.last_test_ok ? "ok" : "failed"}: ${channel.last_test_message ?? ""} (${when(channel.last_test_at)})`
      : "never";
    tr.append(
      name,
      cell(channel.kind === "email" ? "Email" : "Webhook"),
      cell(channelDestination(channel), "text-break"),
      cell(`${channel.min_severity}+`),
      cell(test, channel.last_test_ok === false ? "text-danger" : ""),
    );
    if (canWrite) {
      const actions = cell("", "text-end text-nowrap");
      actions.append(
        button("Test", "secondary", async () => {
          const result = await apiFetch(`/api/v1/alert-channels/${channel.id}/test`, { method: "POST" });
          if (result.ok) showInfo(`${channel.name}: ${result.message}`);
          else showError(`${channel.name}: ${result.message}`);
          await refresh();
        }),
        button("Edit", "primary", async () => openChannelForm(channel)),
        button("Delete", "danger", async () => {
          if (!window.confirm(`Delete the channel "${channel.name}"?`)) return;
          await apiFetch(`/api/v1/alert-channels/${channel.id}`, { method: "DELETE" });
          await refresh();
        }),
      );
      tr.append(actions);
    }
    body.append(tr);
  }
}

function syncChannelKind() {
  const kind = document.getElementById("channel-kind").value;
  document.getElementById("email-fields").classList.toggle("d-none", kind !== "email");
  document.getElementById("webhook-fields").classList.toggle("d-none", kind !== "webhook");
}

function secretHint(channel, field) {
  return channel && channel.secrets_set.includes(field) ? "(stored)" : "";
}

function openChannelForm(channel) {
  state.editingChannel = channel;
  const config = channel ? channel.config : {};
  document.getElementById("channel-form-title").textContent = channel ? `Edit channel "${channel.name}"` : "New channel";
  document.getElementById("channel-name").value = channel ? channel.name : "";
  document.getElementById("channel-kind").value = channel ? channel.kind : "email";
  document.getElementById("channel-kind").disabled = Boolean(channel);
  document.getElementById("channel-severity").value = channel ? channel.min_severity : "warning";
  document.getElementById("channel-enabled").checked = channel ? channel.enabled : true;
  document.getElementById("channel-resolved").checked = channel ? channel.send_resolved : true;
  document.getElementById("channel-insecure").checked = channel ? channel.allow_insecure : false;
  document.getElementById("email-host").value = config.host ?? "";
  document.getElementById("email-port").value = config.port ?? 587;
  document.getElementById("email-security").value = config.security ?? "starttls";
  document.getElementById("email-username").value = config.username ?? "";
  document.getElementById("email-from").value = config.from_address ?? "";
  document.getElementById("email-to").value = (config.to ?? []).join(", ");
  document.getElementById("webhook-url").value = config.url ?? "";
  for (const id of ["email-password", "webhook-secret", "webhook-authorization"]) {
    document.getElementById(id).value = "";
  }
  document.getElementById("email-password-hint").textContent = secretHint(channel, "password");
  document.getElementById("webhook-secret-hint").textContent = secretHint(channel, "signing_secret");
  document.getElementById("webhook-authorization-hint").textContent = secretHint(channel, "authorization");
  syncChannelKind();
  document.getElementById("channel-form").classList.remove("d-none");
  document.getElementById("channel-name").focus();
}

async function submitChannel(event) {
  event.preventDefault();
  hideError();
  const kind = document.getElementById("channel-kind").value;
  const secrets = {};
  let config;
  if (kind === "email") {
    config = {
      host: document.getElementById("email-host").value,
      port: Number(document.getElementById("email-port").value || 587),
      security: document.getElementById("email-security").value,
      username: document.getElementById("email-username").value,
      from_address: document.getElementById("email-from").value,
      to: document.getElementById("email-to").value,
    };
    const password = document.getElementById("email-password").value;
    if (password) secrets.password = password;
  } else {
    config = { url: document.getElementById("webhook-url").value };
    const secret = document.getElementById("webhook-secret").value;
    const authorization = document.getElementById("webhook-authorization").value;
    if (secret) secrets.signing_secret = secret;
    if (authorization) secrets.authorization = authorization;
  }
  const body = {
    name: document.getElementById("channel-name").value,
    config,
    secrets,
    min_severity: document.getElementById("channel-severity").value,
    enabled: document.getElementById("channel-enabled").checked,
    send_resolved: document.getElementById("channel-resolved").checked,
    allow_insecure: document.getElementById("channel-insecure").checked,
  };
  try {
    if (state.editingChannel) {
      await apiFetch(`/api/v1/alert-channels/${state.editingChannel.id}`, { method: "PATCH", body });
    } else {
      await apiFetch("/api/v1/alert-channels", { method: "POST", body: { ...body, kind } });
    }
    document.getElementById("channel-form").classList.add("d-none");
    await refresh();
  } catch (error) {
    showError(error.message);
  }
}

// --------------------------------------------------------------------------

async function refresh() {
  try {
    const [summary, active, resolved, rules, channels] = await Promise.all([
      getJson("/api/v1/alerts/summary"),
      getJson("/api/v1/alerts?state=open&limit=200"),
      getJson("/api/v1/alerts?state=resolved&limit=25"),
      getJson("/api/v1/alert-rules"),
      getJson("/api/v1/alert-channels"),
    ]);
    state.rules = rules;
    state.channels = channels;
    renderSummary(summary);
    renderActive(active);
    renderHistory(resolved);
    renderRules();
    renderChannels();
  } catch (error) {
    showError(error.message);
  }
}

if (canWrite) {
  document.getElementById("add-rule").addEventListener("click", () => openRuleForm(null));
  document.getElementById("rule-cancel").addEventListener("click", () => {
    document.getElementById("rule-form").classList.add("d-none");
  });
  document.getElementById("rule-metric").addEventListener("change", syncRuleFields);
  document.getElementById("rule-form").addEventListener("submit", submitRule);
  document.getElementById("add-channel").addEventListener("click", () => openChannelForm(null));
  document.getElementById("channel-cancel").addEventListener("click", () => {
    document.getElementById("channel-form").classList.add("d-none");
  });
  document.getElementById("channel-kind").addEventListener("change", syncChannelKind);
  document.getElementById("channel-form").addEventListener("submit", submitChannel);
}

refresh();
setInterval(refresh, REFRESH_MS);
