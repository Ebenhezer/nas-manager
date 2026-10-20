import { apiFetch } from "./api.js";

const container = document.getElementById("groups-container");
const banner = document.getElementById("error-banner");
const count = document.getElementById("group-count");
const canWrite = document.getElementById("create-group-form") !== null;

function showError(message) {
  banner.textContent = message;
  banner.classList.remove("d-none");
}

function hideError() {
  banner.classList.add("d-none");
}

function badge(text, kind) {
  const span = document.createElement("span");
  span.className = `badge ${kind} me-1`;
  span.textContent = text;
  return span;
}

function button(text, kind, onClick) {
  const el = document.createElement("button");
  el.type = "button";
  el.className = `btn btn-sm ${kind} ms-1`;
  el.textContent = text;
  el.addEventListener("click", onClick);
  return el;
}

async function editGroup(group) {
  const members = await apiFetch("/api/v1/users");
  const names = window.prompt(
    `Members of ${group.name}, comma-separated (enter user ids):`,
    group.members.map((m) => m.id).join(","),
  );
  if (!names) return;
  const user_ids = names.split(",").map((id) => parseInt(id, 10)).filter(Number.isFinite);
  try {
    const updated = await apiFetch(`/api/v1/groups/${group.id}/members`, {
      method: "PATCH",
      body: { user_ids },
    });
    if (updated) await refresh();
  } catch (error) {
    showError(error.message);
  }
}

async function editRoles(group) {
  const roles = window.prompt(
    `Roles for ${group.name}, comma-separated:`,
    group.roles.map((r) => r.name).join(","),
  );
  if (!roles) return;
  const names = roles.split(",").map((role) => role.trim().toLowerCase()).filter(Boolean);
  try {
    const updated = await apiFetch(`/api/v1/groups/${group.id}/roles`, {
      method: "PATCH",
      body: { roles: names },
    });
    if (updated) await refresh();
  } catch (error) {
    showError(error.message);
  }
}

async function remove(group) {
  if (!window.confirm(`Delete group ${group.name}?`)) return;
  try {
    const response = await apiFetch(`/api/v1/groups/${group.id}`, { method: "DELETE" });
    if (response === null || response === undefined) await refresh();
  } catch (error) {
    showError(error.message);
  }
}

function renderGroups(groups) {
  container.replaceChildren();
  count.textContent = `${groups.length} group${groups.length === 1 ? "" : "s"}`;
  for (const group of groups) {
    const col = document.createElement("div");
    col.className = "col-12 col-lg-6";

    const card = document.createElement("div");
    card.className = "card h-100";

    const header = document.createElement("div");
    header.className = "card-header d-flex justify-content-between align-items-center";
    const name = document.createElement("span");
    name.textContent = group.name;
    const roles = document.createElement("span");
    roles.className = "text-body-secondary small";
    roles.textContent = group.roles.map((r) => r.name).join(", ") || "no roles";
    header.append(name, roles);

    const bodyEl = document.createElement("div");
    bodyEl.className = "card-body";
    const desc = document.createElement("p");
    desc.className = "text-body-secondary small mb-2";
    desc.textContent = group.description || "–";
    const members = document.createElement("div");
    for (const member of group.members) {
      members.append(badge(member.username, "text-bg-light border"));
    }
    if (!group.members.length) {
      const none = document.createElement("span");
      none.className = "text-body-secondary small";
      none.textContent = "no members";
      members.append(none);
    }
    bodyEl.append(desc, members);

    const footer = document.createElement("div");
    footer.className = "card-footer";
    if (canWrite) {
      footer.append(
        button("Members", "btn-outline-secondary", () => editGroup(group)),
        button("Roles", "btn-outline-secondary", () => editRoles(group)),
        button("Delete", "btn-outline-danger", () => remove(group)),
      );
    }
    card.append(header, bodyEl, footer);
    col.append(card);
    container.append(col);
  }
}

async function refresh() {
  hideError();
  try {
    const groups = await apiFetch("/api/v1/groups");
    renderGroups(groups);
  } catch (error) {
    showError(`Could not load groups: ${error.message}`);
  }
}

document.getElementById("create-group-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const data = new FormData(form);
  const payload = {
    name: String(data.get("name")),
    description: String(data.get("description") || ""),
  };
  try {
    const created = await apiFetch("/api/v1/groups", { method: "POST", body: payload });
    if (created) {
      form.reset();
      await refresh();
    }
  } catch (error) {
    showError(error.message);
  }
});

refresh();