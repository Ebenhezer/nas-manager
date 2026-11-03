import { apiFetch } from "./api.js";

const banner = document.getElementById("token-error");
const created = document.getElementById("token-created");
const valueEl = document.getElementById("token-value");
const copyBtn = document.getElementById("token-copy");
const form = document.getElementById("token-form");
const nameInput = document.getElementById("token-name");
const daysInput = document.getElementById("token-days");
const scopesBox = document.getElementById("token-scopes");
const body = document.getElementById("token-body");

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

function renderScopes(info) {
  scopesBox.replaceChildren();
  daysInput.value = String(info.default_lifetime_days);
  if (info.max_lifetime_days > 0) daysInput.max = String(info.max_lifetime_days);
  for (const scope of info.scopes) {
    const wrapper = document.createElement("div");
    wrapper.className = "form-check";
    const input = document.createElement("input");
    input.className = "form-check-input";
    input.type = "checkbox";
    input.value = scope;
    input.id = `scope-${scope.replace(":", "-")}`;
    const label = document.createElement("label");
    label.className = "form-check-label";
    label.htmlFor = input.id;
    label.textContent = scope;
    wrapper.append(input, label);
    scopesBox.append(wrapper);
  }
}

function renderTokens(tokens) {
  body.replaceChildren();
  if (tokens.length === 0) {
    const row = document.createElement("tr");
    const td = cell("No tokens yet.", "text-body-secondary");
    td.colSpan = 6;
    row.append(td);
    body.append(row);
    return;
  }
  for (const token of tokens) {
    const row = document.createElement("tr");
    row.append(cell(token.name));
    row.append(cell(`${token.token_prefix}…`, "font-monospace small"));
    row.append(cell(token.scopes.join(", "), "small"));
    row.append(cell(token.expires_at ? formatDate(token.expires_at) : "never"));
    row.append(cell(formatDate(token.last_used_at)));
    const actions = document.createElement("td");
    actions.className = "text-end";
    if (token.active) {
      const revoke = document.createElement("button");
      revoke.type = "button";
      revoke.className = "btn btn-sm btn-outline-danger";
      revoke.textContent = "Revoke";
      revoke.addEventListener("click", async () => {
        if (!window.confirm(`Revoke token "${token.name}"? Clients using it stop working.`)) return;
        try {
          await apiFetch(`/api/v1/tokens/${token.id}`, { method: "DELETE" });
          await loadTokens();
        } catch (error) {
          showError(error.message);
        }
      });
      actions.append(revoke);
    } else {
      const badge = document.createElement("span");
      badge.className = "badge text-bg-secondary";
      badge.textContent = token.revoked_at ? "revoked" : "expired";
      actions.append(badge);
    }
    row.append(actions);
    body.append(row);
  }
}

async function loadTokens() {
  renderTokens(await apiFetch("/api/v1/tokens"));
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  banner.classList.add("d-none");
  const scopes = [...scopesBox.querySelectorAll("input:checked")].map((input) => input.value);
  if (scopes.length === 0) {
    showError("Select at least one scope.");
    return;
  }
  const payload = { name: nameInput.value, scopes };
  if (daysInput.value !== "") payload.expires_in_days = Number(daysInput.value);
  try {
    const result = await apiFetch("/api/v1/tokens", { method: "POST", body: payload });
    valueEl.textContent = result.token;
    created.classList.remove("d-none");
    nameInput.value = "";
    for (const input of scopesBox.querySelectorAll("input:checked")) input.checked = false;
    await loadTokens();
  } catch (error) {
    showError(error.message);
  }
});

copyBtn.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(valueEl.textContent);
    copyBtn.textContent = "Copied";
  } catch (_ignored) {
    showError("Copy failed; select the token and copy it manually.");
  }
});

Promise.all([apiFetch("/api/v1/tokens/scopes"), loadTokens()])
  .then(([info]) => renderScopes(info))
  .catch((error) => showError(error.message));
