import { apiFetch } from "./api.js";

const banner = document.getElementById("error-banner");
const table = document.getElementById("tokens-table");
const body = document.getElementById("tokens-body");
const canWrite = table.dataset.canWrite === "true";

function showError(message) {
  banner.textContent = message;
  banner.classList.remove("d-none");
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function formatDate(value) {
  return value ? new Date(value).toLocaleString() : "—";
}

function render(tokens) {
  body.replaceChildren();
  if (tokens.length === 0) {
    const row = document.createElement("tr");
    const td = cell("No API tokens have been created.", "text-body-secondary");
    td.colSpan = 8;
    row.append(td);
    body.append(row);
    return;
  }
  for (const token of tokens) {
    const row = document.createElement("tr");
    row.append(cell(token.username));
    row.append(cell(token.name));
    row.append(cell(`${token.token_prefix}…`, "font-monospace small"));
    row.append(cell(token.scopes.join(", "), "small"));
    row.append(cell(formatDate(token.created_at)));
    row.append(cell(token.expires_at ? formatDate(token.expires_at) : "never"));
    row.append(cell(`${formatDate(token.last_used_at)}${token.last_used_ip ? ` (${token.last_used_ip})` : ""}`));
    const status = document.createElement("td");
    status.className = "text-end";
    if (token.active && canWrite) {
      const revoke = document.createElement("button");
      revoke.type = "button";
      revoke.className = "btn btn-sm btn-outline-danger";
      revoke.textContent = "Revoke";
      revoke.addEventListener("click", async () => {
        if (!window.confirm(`Revoke ${token.username}'s token "${token.name}"?`)) return;
        try {
          await apiFetch(`/api/v1/admin/tokens/${token.id}`, { method: "DELETE" });
          await load();
        } catch (error) {
          showError(error.message);
        }
      });
      status.append(revoke);
    } else {
      const badge = document.createElement("span");
      badge.className = `badge ${token.active ? "text-bg-success" : "text-bg-secondary"}`;
      badge.textContent = token.active ? "active" : token.revoked_at ? "revoked" : "expired";
      status.append(badge);
    }
    row.append(status);
    body.append(row);
  }
}

async function load() {
  render(await apiFetch("/api/v1/admin/tokens"));
}

load().catch((error) => showError(error.message));
