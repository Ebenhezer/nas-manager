import { apiFetch, getCsrfToken } from "./api.js";
import { formatBytes } from "./format.js";

const body = document.getElementById("files-body");
const banner = document.getElementById("error-banner");
const rootSelect = document.getElementById("root-select");
const searchInput = document.getElementById("search-input");
const breadcrumb = document.getElementById("breadcrumb");
const mkdirBtn = document.getElementById("mkdir-btn");
const uploadForm = document.getElementById("upload-form");
const canWrite = uploadForm !== null;

let roots = [];
let currentRoot = "";
let currentPath = "";
let searching = false;

function showError(message) {
  banner.textContent = message;
  banner.classList.remove("d-none");
  banner.classList.add("alert-danger");
}

function hideError() {
  banner.classList.add("d-none");
}

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text;
  return td;
}

function fmtTime(value) {
  if (!value) return "–";
  return String(value).replace("T", " ").replace("Z", "").slice(0, 19);
}

function actionButton(text, kind, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `btn btn-sm ${kind} ms-1`;
  button.textContent = text;
  button.addEventListener("click", onClick);
  return button;
}

function navigateTo(path) {
  currentPath = path;
  searching = false;
  searchInput.value = "";
  refresh();
}

function renderBreadcrumb() {
  breadcrumb.replaceChildren();
  const segments = currentPath ? currentPath.split("/").filter(Boolean) : [];
  const rootItem = document.createElement("li");
  rootItem.className = "breadcrumb-item";
  const rootLink = document.createElement("a");
  rootLink.href = "#";
  rootLink.textContent = "Root";
  rootLink.addEventListener("click", (event) => {
    event.preventDefault();
    navigateTo("");
  });
  rootItem.append(rootLink);
  breadcrumb.append(rootItem);
  let prefix = "";
  for (const [index, segment] of segments.entries()) {
    prefix = prefix ? `${prefix}/${segment}` : segment;
    const item = document.createElement("li");
    item.className = "breadcrumb-item";
    if (index === segments.length - 1) {
      item.classList.add("active");
      item.textContent = segment;
    } else {
      const link = document.createElement("a");
      link.href = "#";
      link.textContent = segment;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        navigateTo(prefix);
      });
      item.append(link);
    }
    breadcrumb.append(item);
  }
}

function renderEntries(entries) {
  body.replaceChildren();
  for (const entry of entries) {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const icon = document.createElement("span");
    icon.className = "me-2";
    icon.textContent = entry.is_dir ? "📁" : "📄";
    nameCell.append(icon);
    if (entry.is_dir) {
      const link = document.createElement("a");
      link.href = "#";
      link.textContent = entry.name;
      link.addEventListener("click", (event) => {
        event.preventDefault();
        navigateTo(entry.path);
      });
      nameCell.append(link);
    } else {
      const download = document.createElement("a");
      const params = new URLSearchParams({ root: currentRoot, path: entry.path });
      download.href = `/api/v1/files/content?${params.toString()}`;
      download.textContent = entry.name;
      nameCell.append(download);
    }
    row.append(nameCell, cell(formatBytes(entry.size)), cell(fmtTime(entry.modified)));
    if (canWrite) {
      const actions = document.createElement("td");
      actions.className = "text-end";
      if (!entry.is_dir) {
        actions.append(actionButton("Download", "btn-outline-secondary", () => {
          const params = new URLSearchParams({ root: currentRoot, path: entry.path });
          window.location.href = `/api/v1/files/content?${params.toString()}`;
        }));
      }
      actions.append(
        actionButton("Rename", "btn-outline-secondary", () => renameEntry(entry)),
        actionButton("Delete", "btn-outline-danger", () => deleteEntry(entry)),
      );
      row.append(actions);
    }
    body.append(row);
  }
  if (!entries.length) {
    const row = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = canWrite ? 4 : 3;
    td.className = "text-body-secondary text-center";
    td.textContent = searching ? "No matches." : "This folder is empty.";
    row.append(td);
    body.append(row);
  }
}

async function refresh() {
  hideError();
  try {
    const params = new URLSearchParams({ root: currentRoot, path: currentPath });
    const listing = await apiFetch(`/api/v1/files/list?${params.toString()}`);
    renderBreadcrumb();
    renderEntries(listing.entries);
  } catch (error) {
    showError(`Could not load folder: ${error.message}`);
  }
}

async function runSearch() {
  const query = searchInput.value.trim();
  if (!query) {
    navigateTo(currentPath);
    return;
  }
  hideError();
  searching = true;
  try {
    const params = new URLSearchParams({ root: currentRoot, q: query });
    const result = await apiFetch(`/api/v1/files/search?${params.toString()}`);
    renderBreadcrumb();
    renderEntries(result.matches);
  } catch (error) {
    showError(`Search failed: ${error.message}`);
  }
}

async function uploadFile() {
  const input = document.getElementById("upload-input");
  const file = input.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  form.append("root", currentRoot);
  form.append("path", currentPath);
  form.append("overwrite", "false");
  try {
    const response = await fetch("/api/v1/files/upload", {
      method: "POST",
      headers: { "X-CSRF-Token": getCsrfToken() },
      body: form,
    });
    const text = await response.text();
    let parsed = null;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch (_ignored) {
      parsed = null;
    }
    if (!response.ok) {
      throw new Error(parsed && parsed.error ? parsed.error.message : `Upload failed (${response.status})`);
    }
    input.value = "";
    await refresh();
  } catch (error) {
    showError(`Upload failed: ${error.message}`);
  }
}

async function renameEntry(entry) {
  const newName = window.prompt(`Rename "${entry.name}" to:`, entry.name);
  if (!newName) return;
  try {
    await apiFetch("/api/v1/files/rename", {
      method: "PATCH",
      body: { root: currentRoot, path: entry.path, new_name: newName },
    });
    await refresh();
  } catch (error) {
    showError(error.message);
  }
}

async function deleteEntry(entry) {
  const recursive = entry.is_dir;
  const question = entry.is_dir
    ? `Delete the folder "${entry.name}" and everything inside it? This cannot be undone.`
    : `Delete the file "${entry.name}"? This cannot be undone.`;
  if (!window.confirm(question)) return;
  try {
    await apiFetch("/api/v1/files", {
      method: "DELETE",
      body: { root: currentRoot, path: entry.path, recursive },
    });
    await refresh();
  } catch (error) {
    showError(error.message);
  }
}

async function newFolder() {
  const name = window.prompt("New folder name:");
  if (!name) return;
  try {
    await apiFetch("/api/v1/files/mkdir", {
      method: "POST",
      body: { root: currentRoot, path: currentPath, name },
    });
    await refresh();
  } catch (error) {
    showError(error.message);
  }
}

async function loadRoots() {
  try {
    roots = await apiFetch("/api/v1/files/roots");
  } catch (error) {
    showError(`Could not load roots: ${error.message}`);
    return;
  }
  rootSelect.replaceChildren();
  for (const root of roots) {
    const option = document.createElement("option");
    option.value = root;
    option.textContent = root;
    rootSelect.append(option);
  }
  if (roots.length) {
    currentRoot = roots[0];
    await refresh();
  }
}

rootSelect.addEventListener("change", () => {
  currentRoot = rootSelect.value;
  navigateTo("");
});

searchInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    runSearch();
  } else if (event.key === "Escape") {
    navigateTo(currentPath);
  }
});

uploadForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  uploadFile();
});

mkdirBtn?.addEventListener("click", newFolder);

loadRoots();

