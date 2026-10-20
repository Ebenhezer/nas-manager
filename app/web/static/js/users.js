import { apiFetch } from "./api.js";

const body = document.getElementById("users-body");
const banner = document.getElementById("error-banner");
const count = document.getElementById("user-count");
const canWrite = document.getElementById("create-user-form") !== null;
let currentUserId = null;

function showError(message) {
  banner.textContent = message;
  banner.classList.remove("d-none");
  banner.classList.remove("alert-success");
  banner.classList.add("alert-danger");
}

function hideError() {
  banner.classList.add("d-none");
}

function badge(text, kind) {
  const span = document.createElement("span");
  span.className = `badge ${kind}`;
  span.textContent = text;
  return span;
}

function statusBadge(user) {
  if (!user.is_active) return badge("disabled", "text-bg-secondary");
  if (user.is_locked) return badge("locked", "text-bg-warning");
  return badge("active", "text-bg-success");
}

function roleBadge(name) {
  const span = document.createElement("span");
  span.className = "badge text-bg-light border me-1";
  span.textContent = name;
  return span;
}

function actionButton(text, kind, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `btn btn-sm ${kind} ms-1`;
  button.textContent = text;
  button.addEventListener("click", onClick);
  return button;
}

function renderUsers(users) {
  body.replaceChildren();
  count.textContent = `${users.length} account${users.length === 1 ? "" : "s"}`;
  for (const user of users) {
    const row = document.createElement("tr");
    const username = document.createElement("td");
    username.textContent = user.username;
    const display = document.createElement("td");
    display.textContent = user.display_name || "–";
    const email = document.createElement("td");
    email.textContent = user.email || "–";
    const roles = document.createElement("td");
    for (const role of user.roles) roles.append(roleBadge(role.name));
    const status = document.createElement("td");
    status.append(statusBadge(user));
    row.append(username, display, email, roles, status);
    if (canWrite) {
      const actions = document.createElement("td");
      actions.className = "text-end";
      actions.append(
        actionButton("Reset password", "btn-outline-secondary", () => resetPassword(user)),
        actionButton("Roles", "btn-outline-secondary", () => editRoles(user)),
        actionButton(user.is_active ? "Disable" : "Enable", "btn-outline-warning",
          () => toggleActive(user)),
      );
      if (user.is_locked) actions.append(actionButton("Unlock", "btn-outline-secondary", () => unlock(user)));
      // The API refuses self-deletion; the UI does not offer it either.
      if (user.id !== currentUserId) {
        actions.append(actionButton("Delete", "btn-outline-danger", () => remove(user)));
      }
      row.append(actions);
    }
    body.append(row);
  }
}

async function loadRoles() {
  const roles = await apiFetch("/api/v1/roles");
  const container = document.getElementById("cu-roles");
  if (!container) return;
  container.replaceChildren();
  for (const role of roles) {
    const label = document.createElement("label");
    label.className = "form-check form-check-inline";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.className = "form-check-input";
    input.name = "role";
    input.value = role.name;
    input.checked = role.name === "viewer";
    const span = document.createElement("span");
    span.className = "form-check-label";
    span.textContent = role.name;
    label.append(input, span);
    container.append(label);
  }
}

async function refresh() {
  hideError();
  try {
    const me = await apiFetch("/api/v1/auth/me");
    currentUserId = me.user.id;
    const users = await apiFetch("/api/v1/users");
    renderUsers(users);
    if (canWrite) await loadRoles();
  } catch (error) {
    showError(`Could not load users: ${error.message}`);
  }
}

async function postJson(path, body) {
  try {
    return await apiFetch(path, { method: "POST", body });
  } catch (error) {
    showError(error.message);
    return null;
  }
}

async function patchJson(path, body) {
  try {
    return await apiFetch(path, { method: "PATCH", body });
  } catch (error) {
    showError(error.message);
    return null;
  }
}

document.getElementById("create-user-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const data = new FormData(form);
  const roles = data.getAll("role").map(String);
  const payload = {
    username: String(data.get("username")),
    password: String(data.get("password")),
    display_name: String(data.get("display_name") || ""),
    email: String(data.get("email") || ""),
    roles,
  };
  const created = await postJson("/api/v1/users", payload);
  if (created) {
    form.reset();
    await refresh();
  }
});

async function resetPassword(user) {
  const password = window.prompt(`New password for ${user.username} (min 12 characters):`);
  if (!password) return;
  const updated = await postJson(`/api/v1/users/${user.id}/reset-password`, { new_password: password });
  if (updated) {
    banner.textContent = `Password for ${user.username} was reset; their sessions were revoked.`;
    banner.classList.remove("d-none");
    await refresh();
  }
}

async function toggleActive(user) {
  const updated = await patchJson(`/api/v1/users/${user.id}/active`, { active: !user.is_active });
  if (updated) await refresh();
}

async function unlock(user) {
  const updated = await postJson(`/api/v1/users/${user.id}/unlock`, {});
  if (updated) await refresh();
}

async function editRoles(user) {
  const roles = window.prompt(
    `Roles for ${user.username}, comma-separated (e.g. viewer, operator):`,
    user.roles.map((r) => r.name).join(","),
  );
  if (!roles) return;
  const names = roles.split(",").map((role) => role.trim().toLowerCase()).filter(Boolean);
  const updated = await postJson(`/api/v1/users/${user.id}/roles`, { roles: names });
  if (updated) await refresh();
}

async function remove(user) {
  if (!window.confirm(`Delete user ${user.username}? This cannot be undone.`)) return;
  try {
    const response = await apiFetch(`/api/v1/users/${user.id}`, { method: "DELETE" });
    if (response === null || response === undefined) await refresh();
  } catch (error) {
    showError(error.message);
  }
}

refresh();