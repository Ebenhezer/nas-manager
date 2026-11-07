// Files page: NAS folders and plugged-in USB drives side by side, with
// Windows-style selection, copy / cut / paste, drag & drop and background
// copy/move transfers with progress (Phase 16).

import { apiFetch, getCsrfToken } from "./api.js";
import { formatBytes } from "./format.js";
import { confirmDialog, promptDialog, showDialog } from "./gallery/dialogs.js";
import { hideMenu, onLongPress, showMenu } from "./gallery/menu.js";
import { el, hideError, hydrateIcons, icon, plural, showError, showInfo, storageGet, storageSet } from "./gallery/util.js";

const API = "/api/v1/files";
const DRAG_TYPE = "application/x-nas-files";
const CLIPBOARD_KEY = "nas.files.clipboard";
const LOCATION_KEY = "nas.files.location";

const body = document.getElementById("fx-body");
const content = document.getElementById("fx-content");
const emptyBox = document.getElementById("fx-empty");
const breadcrumb = document.getElementById("fx-breadcrumb");
const searchInput = document.getElementById("fx-search");
const statusText = document.getElementById("fx-status");

const state = {
  locations: [],
  root: "",
  path: "",
  entries: [],
  searching: false,
  selected: new Set(),
  anchor: -1,
  transfers: [],
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function location(root = state.root) {
  return state.locations.find((loc) => loc.root === root) || null;
}

function writable() {
  const loc = location();
  return Boolean(loc && loc.writable);
}

function locationName(loc) {
  return loc.kind === "removable" ? `USB: ${loc.label}` : loc.label;
}

function parentOf(path) {
  return path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
}

function fmtTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function selectedEntries() {
  return state.entries.filter((entry) => state.selected.has(entry.path));
}

function downloadUrl(entry) {
  return `${API}/content?${new URLSearchParams({ root: state.root, path: entry.path })}`;
}

async function attempt(work) {
  try {
    return (await work()) ?? true;
  } catch (error) {
    showError(error.message);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Locations (polled: USB drives come and go)
// ---------------------------------------------------------------------------

function usage(loc) {
  if (loc.total_bytes === null || loc.total_bytes === undefined) return null;
  const used = loc.total_bytes - loc.free_bytes;
  const bar = el("div", { className: "progress fx-usage", attrs: { role: "progressbar", "aria-label": `${locationName(loc)} used space`, "aria-valuenow": Math.round((used / Math.max(1, loc.total_bytes)) * 100), "aria-valuemin": 0, "aria-valuemax": 100 } });
  const fill = el("div", { className: "progress-bar" });
  fill.style.width = `${(used / Math.max(1, loc.total_bytes)) * 100}%`;
  bar.append(fill);
  return el("div", { className: "fx-usage-wrap" }, bar, el("span", { className: "fx-usage-text", text: `${formatBytes(loc.free_bytes)} free of ${formatBytes(loc.total_bytes)}` }));
}

function renderLocations() {
  const pane = document.getElementById("fx-locations");
  const tree = el("div", { className: "mx-tree", attrs: { role: "tree", "aria-label": "Locations" } });
  const groups = [
    ["NAS", state.locations.filter((loc) => loc.kind === "nas")],
    ["USB drives", state.locations.filter((loc) => loc.kind === "removable")],
  ];
  for (const [title, list] of groups) {
    if (title === "USB drives" && document.getElementById("files-root").dataset.canReadUsb !== "true") continue;
    if (title === "NAS" && !list.length) continue;
    tree.append(el("div", { className: "mx-tree-heading" }, el("span", { text: title })));
    for (const loc of list) {
      const active = loc.root === state.root;
      const row = el("div", { className: `mx-tree-row fx-location${active ? " is-active" : ""}`, attrs: { role: "treeitem", "aria-selected": active ? "true" : "false" } });
      const link = el(
        "button",
        { className: "mx-tree-link", attrs: { type: "button", title: loc.root }, on: { click: () => openLocation(loc.root) } },
        icon(loc.kind === "removable" ? "usb" : "drive"),
        el("span", { className: "text-truncate", text: loc.label }),
      );
      if (!loc.writable) link.append(el("span", { className: "badge text-bg-secondary ms-auto", text: "read-only" }));
      row.append(el("span", { className: "mx-tree-caret-space" }), link);
      const meter = usage(loc);
      const wrap = el("div", { className: "fx-location-wrap" }, row);
      if (meter) wrap.append(meter);
      acceptDrop(row, () => ({ root: loc.root, path: "" }), loc);
      tree.append(wrap);
    }
    if (title === "USB drives" && !list.length) {
      tree.append(el("p", { className: "mx-tree-empty", text: "No USB drive connected. Plug one in and it appears here." }));
    }
  }
  pane.replaceChildren(tree);
}

async function loadLocations({ announce = false } = {}) {
  let next;
  try {
    next = await apiFetch(`${API}/locations`);
  } catch (error) {
    showError(`Could not load locations: ${error.message}`);
    return;
  }
  const before = new Map(state.locations.filter((loc) => loc.kind === "removable").map((loc) => [loc.root, loc]));
  const after = new Map(next.filter((loc) => loc.kind === "removable").map((loc) => [loc.root, loc]));
  state.locations = next;
  if (announce) {
    for (const [root, loc] of after) if (!before.has(root)) showInfo(`USB drive “${loc.label}” connected.`);
    for (const [root, loc] of before) if (!after.has(root)) showInfo(`USB drive “${loc.label}” was removed.`);
  }
  renderLocations();
  if (!location() && state.locations.length) {
    const wasUsb = before.has(state.root);
    await openLocation(state.locations[0].root);
    if (wasUsb) showInfo("The USB drive you had open was removed.");
  } else if (!state.locations.length) {
    state.root = "";
    state.entries = [];
    render();
  }
}

async function openLocation(root, path = "") {
  state.root = root;
  state.path = path;
  state.searching = false;
  searchInput.value = "";
  storageSet(LOCATION_KEY, root);
  const pane = document.getElementById("fx-nav");
  const offcanvas = window.bootstrap && bootstrap.Offcanvas.getInstance(pane);
  if (offcanvas) offcanvas.hide();
  renderLocations();
  await refresh();
}

// ---------------------------------------------------------------------------
// Listing
// ---------------------------------------------------------------------------

async function refresh() {
  if (!state.root) return;
  hideError();
  try {
    const listing = await apiFetch(`${API}/list?${new URLSearchParams({ root: state.root, path: state.path })}`);
    state.entries = listing.entries;
  } catch (error) {
    state.entries = [];
    showError(`Could not open the folder: ${error.message}`);
  }
  const visible = new Set(state.entries.map((entry) => entry.path));
  for (const path of Array.from(state.selected)) if (!visible.has(path)) state.selected.delete(path);
  render();
}

async function runSearch() {
  const query = searchInput.value.trim();
  if (!query) {
    state.searching = false;
    await refresh();
    return;
  }
  hideError();
  try {
    const result = await apiFetch(`${API}/search?${new URLSearchParams({ root: state.root, q: query })}`);
    state.entries = result.matches;
    state.searching = true;
    state.selected.clear();
    render();
  } catch (error) {
    showError(`Search failed: ${error.message}`);
  }
}

function openFolder(path) {
  state.path = path;
  state.searching = false;
  searchInput.value = "";
  state.selected.clear();
  refresh();
}

function renderBreadcrumb() {
  breadcrumb.replaceChildren();
  const loc = location();
  if (!loc) return;
  breadcrumb.append(icon(loc.kind === "removable" ? "usb" : "drive"));
  const parts = state.path ? state.path.split("/") : [];
  const crumb = (label, target, current) =>
    current
      ? el("span", { className: "mx-crumb is-current", text: label, attrs: { "aria-current": "location" } })
      : el("button", { className: "mx-crumb", text: label, attrs: { type: "button" }, on: { click: () => openFolder(target) } });
  breadcrumb.append(crumb(locationName(loc), "", !parts.length && !state.searching));
  parts.forEach((part, index) => {
    breadcrumb.append(el("span", { className: "mx-crumb-sep", text: "›", attrs: { "aria-hidden": "true" } }));
    breadcrumb.append(crumb(part, parts.slice(0, index + 1).join("/"), index === parts.length - 1 && !state.searching));
  });
  if (state.searching) {
    breadcrumb.append(el("span", { className: "mx-crumb-sep", text: "›" }), el("span", { className: "mx-crumb is-current", text: `Search: ${searchInput.value.trim()}` }));
  }
  breadcrumb.title = loc.root + (state.path ? `/${state.path}` : "");
}

function row(entry, index) {
  const selected = state.selected.has(entry.path);
  const check = el("input", { className: "form-check-input", attrs: { type: "checkbox", "aria-label": `Select ${entry.name}`, tabindex: "-1" } });
  check.checked = selected;
  const name = el(
    "span",
    { className: "fx-name" },
    icon(entry.is_dir ? "folder" : "file", { size: 18, className: entry.is_dir ? "mx-folder-icon" : "fx-file-icon" }),
    el("span", { className: "text-truncate", text: state.searching ? entry.path : entry.name }),
  );
  const tr = el(
    "tr",
    {
      className: `mx-entry fx-row${selected ? " is-selected" : ""}`,
      attrs: { tabindex: "-1", draggable: "true", "aria-selected": selected ? "true" : "false" },
      dataset: { index },
    },
    el("td", { className: "fx-col-check" }, check),
    el("td", { className: "mx-col-name" }, name),
    el("td", { text: fmtTime(entry.modified) }),
    el("td", { className: "text-end", text: entry.is_dir ? "" : formatBytes(entry.size) }),
  );
  return tr;
}

function render() {
  hideMenu();
  renderBreadcrumb();
  body.replaceChildren(...state.entries.map(row));
  emptyBox.classList.toggle("d-none", state.entries.length > 0);
  emptyBox.textContent = state.searching ? "No matches." : writable() ? "This folder is empty. Drop files here to upload them." : "This folder is empty.";
  const first = body.querySelector(".fx-row");
  if (first) first.tabIndex = 0;
  renderSelection();
}

function renderSelection() {
  for (const tr of body.querySelectorAll(".fx-row")) {
    const entry = state.entries[Number(tr.dataset.index)];
    const on = state.selected.has(entry.path);
    tr.classList.toggle("is-selected", on);
    tr.setAttribute("aria-selected", on ? "true" : "false");
    tr.querySelector("input[type=checkbox]").checked = on;
  }
  const selected = selectedEntries();
  const all = document.getElementById("fx-select-all");
  all.checked = selected.length > 0 && selected.length === state.entries.length;
  all.indeterminate = selected.length > 0 && selected.length < state.entries.length;
  const canWrite = writable();
  const single = selected.length === 1 ? selected[0] : null;
  const toggle = (id, enabled) => {
    document.getElementById(id).disabled = !enabled;
  };
  toggle("fx-upload", canWrite);
  toggle("fx-new-folder", canWrite && !state.searching);
  toggle("fx-copy", selected.length > 0);
  toggle("fx-cut", selected.length > 0 && canWrite);
  toggle("fx-paste", Boolean(readClipboard()) && canWrite && !state.searching);
  toggle("fx-copy-to", selected.length > 0);
  toggle("fx-move-to", selected.length > 0 && canWrite);
  toggle("fx-download", Boolean(single && !single.is_dir));
  toggle("fx-rename", Boolean(single) && canWrite);
  toggle("fx-delete", selected.length > 0 && canWrite);
  document.getElementById("fx-up").disabled = !state.path && !state.searching;
  const files = state.entries.filter((entry) => !entry.is_dir);
  const parts = [plural(state.entries.length, "item")];
  if (selected.length) {
    const bytes = selected.reduce((sum, entry) => sum + (entry.is_dir ? 0 : entry.size), 0);
    parts.push(`${selected.length} selected${bytes ? ` (${formatBytes(bytes)})` : ""}`);
  } else if (files.length) {
    parts.push(formatBytes(files.reduce((sum, entry) => sum + entry.size, 0)));
  }
  const loc = location();
  if (loc && loc.kind === "removable") parts.push(`${formatBytes(loc.free_bytes)} free on the drive`);
  statusText.textContent = parts.join(" · ");
  renderClipboard();
}

// ---------------------------------------------------------------------------
// Selection and keyboard
// ---------------------------------------------------------------------------

function select(index, { toggle = false, range = false } = {}) {
  const entry = state.entries[index];
  if (!entry) return;
  if (range && state.anchor >= 0) {
    if (!toggle) state.selected.clear();
    const [from, to] = [Math.min(state.anchor, index), Math.max(state.anchor, index)];
    for (let i = from; i <= to; i += 1) state.selected.add(state.entries[i].path);
  } else if (toggle) {
    if (state.selected.has(entry.path)) state.selected.delete(entry.path);
    else state.selected.add(entry.path);
    state.anchor = index;
  } else {
    state.selected.clear();
    state.selected.add(entry.path);
    state.anchor = index;
  }
  renderSelection();
}

function focusRow(tr) {
  for (const other of body.querySelectorAll('.fx-row[tabindex="0"]')) other.tabIndex = -1;
  tr.tabIndex = 0;
  tr.focus({ preventScroll: true });
  tr.scrollIntoView({ block: "nearest" });
}

function activate(entry) {
  if (entry.is_dir) openFolder(entry.path);
  else window.location.href = downloadUrl(entry);
}

let lastPointer = "mouse";

body.addEventListener("pointerdown", (event) => {
  lastPointer = event.pointerType;
});

body.addEventListener("click", (event) => {
  const tr = event.target.closest(".fx-row");
  if (!tr) return;
  const index = Number(tr.dataset.index);
  focusRow(tr);
  const onCheck = Boolean(event.target.closest("input[type=checkbox]"));
  if (lastPointer !== "mouse" && !onCheck && !state.selected.size) {
    activate(state.entries[index]);
    return;
  }
  select(index, { toggle: onCheck || event.ctrlKey || event.metaKey || (lastPointer !== "mouse" && state.selected.size > 0), range: event.shiftKey });
});

body.addEventListener("dblclick", (event) => {
  const tr = event.target.closest(".fx-row");
  if (tr && !event.target.closest("input")) activate(state.entries[Number(tr.dataset.index)]);
});

content.addEventListener("click", (event) => {
  if (!event.target.closest(".fx-row, thead") && state.selected.size) {
    state.selected.clear();
    renderSelection();
  }
});

document.getElementById("fx-select-all").addEventListener("change", (event) => {
  state.selected = event.target.checked ? new Set(state.entries.map((entry) => entry.path)) : new Set();
  renderSelection();
});

body.addEventListener("keydown", (event) => {
  const tr = event.target.closest(".fx-row");
  if (!tr) return;
  const index = Number(tr.dataset.index);
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const next = body.querySelectorAll(".fx-row")[index + (event.key === "ArrowDown" ? 1 : -1)];
    if (!next) return;
    focusRow(next);
    if (!(event.ctrlKey || event.metaKey) || event.shiftKey) select(Number(next.dataset.index), { range: event.shiftKey });
  } else if (event.key === "Enter") {
    event.preventDefault();
    activate(state.entries[index]);
  } else if (event.key === " ") {
    event.preventDefault();
    select(index, { toggle: true });
  } else if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
    event.preventDefault();
    const rect = tr.getBoundingClientRect();
    contextMenu({ preventDefault() {}, clientX: rect.left + 40, clientY: rect.bottom, target: tr });
  }
});

document.addEventListener("keydown", (event) => {
  if (event.target.closest("input, textarea, select, .modal, .mx-context-menu")) return;
  const ctrl = event.ctrlKey || event.metaKey;
  const key = event.key.toLowerCase();
  if (ctrl && key === "a") {
    event.preventDefault();
    state.selected = new Set(state.entries.map((entry) => entry.path));
    renderSelection();
  } else if (ctrl && key === "c" && state.selected.size) {
    event.preventDefault();
    toClipboard("copy");
  } else if (ctrl && key === "x" && state.selected.size && writable()) {
    event.preventDefault();
    toClipboard("move");
  } else if (ctrl && key === "v" && readClipboard() && writable()) {
    event.preventDefault();
    paste();
  } else if (event.key === "Delete" && state.selected.size && writable()) {
    event.preventDefault();
    deleteSelected();
  } else if (event.key === "F2" && state.selected.size === 1 && writable()) {
    event.preventDefault();
    renameEntry(selectedEntries()[0]);
  } else if (event.key === "Backspace" || (event.altKey && event.key === "ArrowUp")) {
    event.preventDefault();
    goUp();
  } else if (event.key === "Escape" && state.selected.size) {
    state.selected.clear();
    renderSelection();
  }
});

function goUp() {
  if (state.searching) {
    searchInput.value = "";
    state.searching = false;
    refresh();
  } else if (state.path) {
    openFolder(parentOf(state.path));
  }
}

// ---------------------------------------------------------------------------
// Clipboard (kept per browser tab)
// ---------------------------------------------------------------------------

function readClipboard() {
  try {
    const raw = window.sessionStorage.getItem(CLIPBOARD_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (_ignored) {
    return null;
  }
}

function writeClipboard(value) {
  try {
    if (value) window.sessionStorage.setItem(CLIPBOARD_KEY, JSON.stringify(value));
    else window.sessionStorage.removeItem(CLIPBOARD_KEY);
  } catch (_ignored) {
    // storage blocked: the clipboard just does not survive navigation
  }
}

function toClipboard(mode) {
  const items = selectedEntries();
  if (!items.length) return;
  writeClipboard({ mode, root: state.root, paths: items.map((entry) => entry.path), label: location()?.label || "" });
  showInfo(`${plural(items.length, "item")} ${mode === "move" ? "cut" : "copied"}. Open a folder or USB drive and choose Paste.`);
  renderSelection();
}

function renderClipboard() {
  const clip = readClipboard();
  const box = document.getElementById("fx-clipboard");
  box.replaceChildren();
  if (!clip) return;
  box.append(
    icon("clipboard", { size: 14 }),
    ` ${plural(clip.paths.length, "item")} ${clip.mode === "move" ? "cut" : "copied"} from ${clip.label} `,
    el("button", { className: "btn btn-link btn-sm p-0 align-baseline", text: "Clear", attrs: { type: "button" }, on: { click: () => { writeClipboard(null); renderSelection(); } } }),
  );
}

async function paste(target = { root: state.root, path: state.path }) {
  const clip = readClipboard();
  if (!clip) return;
  const started = await startTransfer({
    mode: clip.mode,
    sources: clip.paths.map((path) => ({ root: clip.root, path })),
    destRoot: target.root,
    destPath: target.path,
  });
  if (started && clip.mode === "move") writeClipboard(null);
  renderSelection();
}

// ---------------------------------------------------------------------------
// Transfers
// ---------------------------------------------------------------------------

async function existingNames(root, path) {
  if (root === state.root && path === state.path && !state.searching) {
    return new Set(state.entries.map((entry) => entry.name));
  }
  try {
    const listing = await apiFetch(`${API}/list?${new URLSearchParams({ root, path })}`);
    return new Set(listing.entries.map((entry) => entry.name));
  } catch (_ignored) {
    return new Set();
  }
}

async function askConflict(count) {
  const choice = { value: "keep_both" };
  const options = [
    ["keep_both", "Keep both", "Copies get a number, e.g. “photo (1).jpg”."],
    ["replace", "Replace the files in the destination", "Folders are merged; files with the same name are overwritten."],
    ["skip", "Skip these files", "Folders are merged; files that already exist are left alone."],
  ];
  const list = el("div", { className: "d-flex flex-column gap-2" });
  for (const [value, label, help] of options) {
    const id = `fx-conflict-${value}`;
    const input = el("input", { className: "form-check-input", attrs: { type: "radio", name: "fx-conflict", id, value } });
    input.checked = value === "keep_both";
    input.addEventListener("change", () => {
      choice.value = value;
    });
    list.append(el("div", { className: "form-check" }, input, el("label", { className: "form-check-label", attrs: { for: id } }, el("span", { className: "d-block", text: label }), el("small", { className: "text-body-secondary", text: help }))));
  }
  const ok = await showDialog({
    title: "Some items already exist",
    okLabel: "Continue",
    body: [el("p", { text: `${plural(count, "item")} with the same name already exist in the destination.` }), list],
  });
  return ok ? choice.value : null;
}

async function startTransfer({ mode, sources, destRoot, destPath }) {
  if (!sources.length) return false;
  const names = await existingNames(destRoot, destPath);
  const clashes = sources.filter((source) => names.has(source.path.split("/").pop()));
  const sameFolder = sources.every((source) => source.root === destRoot && parentOf(source.path) === destPath);
  if (mode === "move" && sameFolder) {
    showInfo("The items are already in that folder.");
    return false;
  }
  let conflict = "keep_both";
  if (clashes.length && !sameFolder) {
    conflict = await askConflict(clashes.length);
    if (!conflict) return false;
  }
  const created = await attempt(() =>
    apiFetch(`${API}/transfers`, { method: "POST", body: { mode, sources, dest_root: destRoot, dest_path: destPath, conflict } }),
  );
  if (!created) return false;
  state.transfers = [created, ...state.transfers.filter((t) => t.id !== created.id)];
  renderTransfers();
  pollTransfers();
  return true;
}

const ACTIVE = new Set(["queued", "running"]);
let pollTimer = null;
const finishedSeen = new Set();
// The first poll only learns which transfers already finished (no toasts).
let initialPoll = true;

function transferLabel(transfer) {
  const dest = location(transfer.dest_root);
  const where = `${dest ? locationName(dest) : transfer.dest_root.split("/").pop()}${transfer.dest_path ? ` / ${transfer.dest_path}` : ""}`;
  const what = transfer.sources.length === 1 ? `“${transfer.sources[0].path.split("/").pop()}”` : plural(transfer.sources.length, "item");
  return `${transfer.mode === "move" ? "Moving" : "Copying"} ${what} to ${where}`;
}

function transferRow(transfer) {
  const percent = transfer.total_bytes ? Math.min(100, Math.round((transfer.done_bytes / transfer.total_bytes) * 100)) : ACTIVE.has(transfer.state) ? 0 : 100;
  const stateText = {
    queued: "Waiting…",
    running: `${percent}% · ${formatBytes(transfer.done_bytes)} of ${formatBytes(transfer.total_bytes)}`,
    done: transfer.message || `Done · ${plural(transfer.done_files, "file")}`,
    failed: transfer.message || "Failed",
    cancelled: "Cancelled",
    interrupted: transfer.message || "Interrupted",
  }[transfer.state] || transfer.state;
  const bar = el("div", { className: "progress fx-transfer-bar", attrs: { role: "progressbar", "aria-valuenow": percent, "aria-valuemin": 0, "aria-valuemax": 100, "aria-label": transferLabel(transfer) } });
  const fill = el("div", { className: `progress-bar${transfer.state === "failed" ? " bg-danger" : transfer.state === "done" ? (transfer.errors.length ? " bg-warning" : " bg-success") : ACTIVE.has(transfer.state) ? " progress-bar-striped progress-bar-animated" : " bg-secondary"}` });
  fill.style.width = `${percent}%`;
  bar.append(fill);
  const rowBox = el("div", { className: `fx-transfer is-${transfer.state}` }, el("div", { className: "fx-transfer-title text-truncate", text: transferLabel(transfer), title: transferLabel(transfer) }), bar);
  const meta = el("div", { className: "fx-transfer-meta" }, el("span", { className: "text-truncate", text: transfer.state === "running" && transfer.current_name ? `${stateText} · ${transfer.current_name}` : stateText }));
  if (ACTIVE.has(transfer.state)) {
    meta.append(el("button", { className: "btn btn-link btn-sm p-0 ms-auto", text: "Cancel", attrs: { type: "button" }, on: { click: () => cancelTransfer(transfer) } }));
  } else {
    meta.append(el("button", { className: "btn btn-link btn-sm p-0 ms-auto text-body-secondary", text: "Dismiss", attrs: { type: "button" }, on: { click: () => dismiss(transfer) } }));
  }
  rowBox.append(meta);
  if (transfer.errors.length && !ACTIVE.has(transfer.state)) {
    const details = el("details", { className: "fx-transfer-errors" }, el("summary", { text: `${plural(transfer.errors.length, "problem")}` }));
    const list = el("ul", { className: "mb-0" });
    for (const problem of transfer.errors) list.append(el("li", {}, el("code", { text: problem.path }), ` — ${problem.message}`));
    details.append(list);
    rowBox.append(details);
  }
  return rowBox;
}

const dismissed = new Set((storageGet("nas.files.dismissed", "") || "").split(",").filter(Boolean).map(Number));

function dismiss(transfer) {
  dismissed.add(transfer.id);
  storageSet("nas.files.dismissed", Array.from(dismissed).slice(-100).join(","));
  renderTransfers();
}

function renderTransfers() {
  const panel = document.getElementById("fx-transfers");
  const shown = state.transfers.filter((t) => ACTIVE.has(t.state) || !dismissed.has(t.id)).slice(0, 8);
  panel.classList.toggle("d-none", !shown.length);
  const active = shown.filter((t) => ACTIVE.has(t.state)).length;
  document.getElementById("fx-transfers-title").textContent = active ? `${plural(active, "transfer")} in progress` : "Transfers";
  document.getElementById("fx-transfers-list").replaceChildren(...shown.map(transferRow));
}

async function cancelTransfer(transfer) {
  const updated = await attempt(() => apiFetch(`${API}/transfers/${transfer.id}/cancel`, { method: "POST" }));
  if (updated && updated !== true) {
    state.transfers = state.transfers.map((t) => (t.id === updated.id ? updated : t));
    renderTransfers();
  }
}

async function pollTransfers() {
  window.clearTimeout(pollTimer);
  try {
    state.transfers = await apiFetch(`${API}/transfers?limit=20`);
  } catch (_ignored) {
    pollTimer = window.setTimeout(pollTransfers, 5000);
    return;
  }
  let reload = false;
  for (const transfer of state.transfers) {
    if (ACTIVE.has(transfer.state) || finishedSeen.has(transfer.id)) continue;
    finishedSeen.add(transfer.id);
    if (!initialPoll) {
      const into = transfer.dest_root === state.root;
      const from = transfer.mode === "move" && transfer.sources.some((s) => s.root === state.root);
      if (into || from) reload = true;
      if (transfer.state === "done") showInfo(`${transferLabel(transfer).replace(/^Copying/, "Copied").replace(/^Moving/, "Moved")}${transfer.errors.length ? ` (${plural(transfer.errors.length, "problem")})` : ""}.`);
      else if (transfer.state === "failed") showError(`${transferLabel(transfer)} failed: ${transfer.message || "unknown error"}`);
    }
  }
  initialPoll = false;
  renderTransfers();
  if (reload) {
    await Promise.all([refresh(), loadLocations()]);
  }
  if (state.transfers.some((t) => ACTIVE.has(t.state))) pollTimer = window.setTimeout(pollTransfers, 1000);
}

document.getElementById("fx-transfers-toggle").addEventListener("click", (event) => {
  const list = document.getElementById("fx-transfers-list");
  const open = list.classList.toggle("d-none") === false;
  event.currentTarget.setAttribute("aria-expanded", open ? "true" : "false");
});

// ---------------------------------------------------------------------------
// Copy to… / Move to… (location and folder picker)
// ---------------------------------------------------------------------------

async function pickDestination(title, okLabel) {
  const targets = state.locations.filter((loc) => loc.writable);
  if (!targets.length) {
    showError("There is no location you can copy to. Plug in a USB drive or ask an administrator for access.");
    return null;
  }
  let current = { root: (targets.find((loc) => loc.kind === "removable" && loc.root !== state.root) || targets[0]).root, path: "" };
  const select = el("select", { className: "form-select form-select-sm mb-2", attrs: { "aria-label": "Location" } });
  for (const loc of targets) select.append(el("option", { text: locationName(loc), attrs: { value: loc.root } }));
  select.value = current.root;
  const crumbs = el("nav", { className: "mx-picker-crumbs mb-2", attrs: { "aria-label": "Folder" } });
  const list = el("div", { className: "list-group mx-picker-list" });
  const newFolder = el("button", { className: "btn btn-sm btn-outline-secondary mt-2", attrs: { type: "button" } }, icon("folderPlus"), " New folder");

  async function load(root, path) {
    current = { root, path };
    const loc = location(root);
    crumbs.replaceChildren();
    const parts = path ? path.split("/") : [];
    [[locationName(loc), ""], ...parts.map((part, i) => [part, parts.slice(0, i + 1).join("/")])].forEach(([name, target], index) => {
      if (index) crumbs.append(el("span", { className: "text-body-secondary mx-1", text: "›" }));
      crumbs.append(el("button", { className: "btn btn-link btn-sm p-0", text: name, attrs: { type: "button" }, on: { click: () => load(root, target) } }));
    });
    list.replaceChildren(el("div", { className: "list-group-item small text-body-secondary", text: "Loading…" }));
    try {
      const listing = await apiFetch(`${API}/list?${new URLSearchParams({ root, path })}`);
      const folders = listing.entries.filter((entry) => entry.is_dir);
      list.replaceChildren(...folders.map((folder) => el("button", { className: "list-group-item list-group-item-action d-flex align-items-center gap-2", attrs: { type: "button" }, on: { click: () => load(root, folder.path) } }, icon("folder", { className: "mx-folder-icon" }), el("span", { className: "text-truncate", text: folder.name }))));
      if (!folders.length) list.append(el("div", { className: "list-group-item small text-body-secondary", text: "No subfolders." }));
    } catch (error) {
      list.replaceChildren(el("div", { className: "list-group-item small text-danger", text: error.message }));
    }
  }

  select.addEventListener("change", () => load(select.value, ""));
  newFolder.addEventListener("click", () => {
    const input = el("input", { className: "form-control form-control-sm", attrs: { type: "text", maxlength: 255, placeholder: "Folder name", "aria-label": "New folder name" } });
    const create = el("button", { className: "btn btn-sm btn-primary", text: "Create", attrs: { type: "button" } });
    const rowBox = el("div", { className: "input-group input-group-sm mt-2" }, input, create);
    newFolder.replaceWith(rowBox);
    input.focus();
    const submit = async () => {
      const name = input.value.trim();
      if (!name) return;
      try {
        const made = await apiFetch(`${API}/mkdir`, { method: "POST", body: { root: current.root, path: current.path, name } });
        rowBox.replaceWith(newFolder);
        await load(current.root, made.path);
      } catch (error) {
        input.classList.add("is-invalid");
        input.title = error.message;
      }
    };
    create.addEventListener("click", submit);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submit();
      }
    });
  });
  const shown = showDialog({ title, okLabel, body: [select, crumbs, list, newFolder] });
  await load(current.root, current.path);
  return (await shown) ? current : null;
}

async function copyOrMoveTo(mode) {
  const items = selectedEntries();
  if (!items.length) return;
  const target = await pickDestination(`${mode === "move" ? "Move" : "Copy"} ${plural(items.length, "item")} to…`, mode === "move" ? "Move here" : "Copy here");
  if (!target) return;
  await startTransfer({ mode, sources: items.map((entry) => ({ root: state.root, path: entry.path })), destRoot: target.root, destPath: target.path });
}

// ---------------------------------------------------------------------------
// Drag & drop
// ---------------------------------------------------------------------------

body.addEventListener("dragstart", (event) => {
  const tr = event.target.closest(".fx-row");
  if (!tr) return;
  const index = Number(tr.dataset.index);
  if (!state.selected.has(state.entries[index].path)) select(index);
  const paths = selectedEntries().map((entry) => entry.path);
  event.dataTransfer.setData(DRAG_TYPE, JSON.stringify({ root: state.root, paths }));
  event.dataTransfer.effectAllowed = "copyMove";
  const label = el("div", { className: "mx-drag-label", text: plural(paths.length, "item") });
  document.body.append(label);
  event.dataTransfer.setDragImage(label, 12, 12);
  window.setTimeout(() => label.remove(), 0);
});

// Same location: move; another location: copy. Shift forces move, Ctrl copy.
function dropMode(event, fromRoot, toRoot) {
  if (event.shiftKey) return "move";
  if (event.ctrlKey || event.metaKey) return "copy";
  return fromRoot === toRoot ? "move" : "copy";
}

function acceptDrop(element, targetOf, loc) {
  element.addEventListener("dragover", (event) => {
    if (!event.dataTransfer.types.includes(DRAG_TYPE) || !loc.writable) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = event.shiftKey ? "move" : loc.root === state.root && !(event.ctrlKey || event.metaKey) ? "move" : "copy";
    element.classList.add("is-drop-target");
  });
  element.addEventListener("dragleave", () => element.classList.remove("is-drop-target"));
  element.addEventListener("drop", (event) => {
    element.classList.remove("is-drop-target");
    const raw = event.dataTransfer.getData(DRAG_TYPE);
    if (!raw) return;
    event.preventDefault();
    event.stopPropagation();
    const data = JSON.parse(raw);
    const target = targetOf();
    const sources = data.paths.filter((path) => !(data.root === target.root && (target.path === path || target.path.startsWith(`${path}/`)))).map((path) => ({ root: data.root, path }));
    startTransfer({ mode: dropMode(event, data.root, target.root), sources, destRoot: target.root, destPath: target.path });
  });
}

body.addEventListener("dragover", (event) => {
  const tr = event.target.closest(".fx-row");
  if (!tr || !event.dataTransfer.types.includes(DRAG_TYPE)) return;
  const entry = state.entries[Number(tr.dataset.index)];
  if (!entry.is_dir || !writable() || state.selected.has(entry.path)) return;
  event.preventDefault();
  tr.classList.add("is-drop-target");
});

body.addEventListener("dragleave", (event) => {
  const tr = event.target.closest(".fx-row");
  if (tr) tr.classList.remove("is-drop-target");
});

body.addEventListener("drop", (event) => {
  const tr = event.target.closest(".fx-row");
  if (!tr) return;
  tr.classList.remove("is-drop-target");
  const raw = event.dataTransfer.getData(DRAG_TYPE);
  const entry = state.entries[Number(tr.dataset.index)];
  if (!raw || !entry.is_dir) return;
  event.preventDefault();
  event.stopPropagation();
  const data = JSON.parse(raw);
  const sources = data.paths.filter((path) => path !== entry.path).map((path) => ({ root: data.root, path }));
  startTransfer({ mode: dropMode(event, data.root, state.root), sources, destRoot: state.root, destPath: entry.path });
});

// Files from the desktop: upload into the open folder.
const overlay = document.getElementById("fx-drop-overlay");
let dragDepth = 0;
const hasFiles = (event) => event.dataTransfer && Array.from(event.dataTransfer.types).includes("Files") && writable();

content.addEventListener("dragenter", (event) => {
  if (!hasFiles(event)) return;
  dragDepth += 1;
  overlay.classList.remove("d-none");
});
content.addEventListener("dragover", (event) => {
  if (hasFiles(event)) event.preventDefault();
});
content.addEventListener("dragleave", (event) => {
  if (!hasFiles(event)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) overlay.classList.add("d-none");
});
content.addEventListener("drop", (event) => {
  if (!hasFiles(event)) return;
  event.preventDefault();
  dragDepth = 0;
  overlay.classList.add("d-none");
  uploadFiles(event.dataTransfer.files);
});

// ---------------------------------------------------------------------------
// Uploads, folders, rename, delete
// ---------------------------------------------------------------------------

function uploadOne(file, root, path) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("root", root);
    form.append("path", path);
    form.append("overwrite", "false");
    const request = new XMLHttpRequest();
    request.open("POST", `${API}/upload`);
    request.setRequestHeader("X-CSRF-Token", getCsrfToken());
    request.addEventListener("load", () => {
      if (request.status >= 200 && request.status < 300) return resolve();
      let message = `${request.status}`;
      try {
        message = JSON.parse(request.responseText).error.message;
      } catch (_ignored) {
        // keep the status code
      }
      reject(new Error(`${file.name}: ${message}`));
    });
    request.addEventListener("error", () => reject(new Error(`${file.name}: network error`)));
    request.send(form);
  });
}

async function uploadFiles(fileList) {
  const files = Array.from(fileList);
  if (!files.length || !writable()) return;
  const root = state.root;
  const path = state.path;
  const failures = [];
  for (let index = 0; index < files.length; index += 1) {
    statusText.textContent = `Uploading ${index + 1} of ${files.length}: ${files[index].name}…`;
    try {
      await uploadOne(files[index], root, path);
    } catch (error) {
      failures.push(error.message);
    }
  }
  if (failures.length) showError(failures.join("; "));
  const sent = files.length - failures.length;
  if (sent) showInfo(`Uploaded ${plural(sent, "file")}.`);
  await Promise.all([refresh(), loadLocations()]);
}

async function newFolder() {
  const name = await promptDialog({ title: "New folder", label: "Folder name", value: "New folder", okLabel: "Create" });
  if (!name) return;
  if (await attempt(() => apiFetch(`${API}/mkdir`, { method: "POST", body: { root: state.root, path: state.path, name } }))) await refresh();
}

async function renameEntry(entry) {
  const name = await promptDialog({ title: "Rename", label: "New name", value: entry.name, okLabel: "Rename", selectStem: !entry.is_dir });
  if (!name || name === entry.name) return;
  if (await attempt(() => apiFetch(`${API}/rename`, { method: "PATCH", body: { root: state.root, path: entry.path, new_name: name } }))) await refresh();
}

async function deleteSelected() {
  const items = selectedEntries();
  if (!items.length) return;
  const onUsb = location()?.kind === "removable";
  const message =
    items.length === 1
      ? `Permanently delete ${items[0].is_dir ? "the folder" : ""} “${items[0].name}”${items[0].is_dir ? " and everything in it" : ""}? This cannot be undone${onUsb ? " (USB drives have no recycle bin)" : ""}.`
      : `Permanently delete ${plural(items.length, "item")}? Folders are deleted with everything in them. This cannot be undone.`;
  if (!(await confirmDialog({ title: "Delete", message, okLabel: "Delete", danger: true }))) return;
  for (const entry of items) {
    await attempt(() => apiFetch(API, { method: "DELETE", body: { root: state.root, path: entry.path, recursive: entry.is_dir } }));
  }
  state.selected.clear();
  await Promise.all([refresh(), loadLocations()]);
}

// ---------------------------------------------------------------------------
// Context menu
// ---------------------------------------------------------------------------

function contextMenu(event) {
  const tr = event.target.closest ? event.target.closest(".fx-row") : null;
  event.preventDefault();
  const canWrite = writable();
  const clip = readClipboard();
  if (!tr) {
    const entries = [];
    if (canWrite) {
      entries.push(
        { label: "New folder", icon: "folderPlus", action: newFolder },
        { label: "Upload files…", icon: "upload", action: () => document.getElementById("fx-upload-input").click() },
      );
      if (clip) entries.push({ label: `Paste ${plural(clip.paths.length, "item")}`, icon: "clipboard", action: () => paste() });
      entries.push({ divider: true });
    }
    entries.push({ label: "Refresh", icon: "refresh", action: refresh });
    showMenu(event.clientX, event.clientY, entries);
    return;
  }
  const index = Number(tr.dataset.index);
  if (!state.selected.has(state.entries[index].path)) select(index);
  const items = selectedEntries();
  const single = items.length === 1 ? items[0] : null;
  const entries = [];
  if (single) entries.push({ label: single.is_dir ? "Open" : "Download", icon: single.is_dir ? "folder" : "download", action: () => activate(single) });
  entries.push(
    { label: "Copy", icon: "copy", action: () => toClipboard("copy") },
    { label: "Copy to…", icon: "usb", action: () => copyOrMoveTo("copy") },
  );
  if (canWrite) {
    entries.push({ label: "Cut", icon: "move", action: () => toClipboard("move") }, { label: "Move to…", icon: "move", action: () => copyOrMoveTo("move") });
    if (single && single.is_dir && clip) entries.push({ label: `Paste into “${single.name}”`, icon: "clipboard", action: () => paste({ root: state.root, path: single.path }) });
    entries.push({ divider: true });
    if (single) entries.push({ label: "Rename", icon: "edit", action: () => renameEntry(single) });
    entries.push({ label: "Delete", icon: "trash", danger: true, action: deleteSelected });
  }
  showMenu(event.clientX, event.clientY, entries);
}

content.addEventListener("contextmenu", contextMenu);
onLongPress(content, contextMenu);

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function wire() {
  hydrateIcons();
  document.getElementById("fx-up").addEventListener("click", goUp);
  document.getElementById("fx-refresh").addEventListener("click", () => Promise.all([refresh(), loadLocations()]));
  const input = document.getElementById("fx-upload-input");
  document.getElementById("fx-upload").addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    if (input.files && input.files.length) uploadFiles(input.files);
    input.value = "";
  });
  document.getElementById("fx-new-folder").addEventListener("click", newFolder);
  document.getElementById("fx-copy").addEventListener("click", () => toClipboard("copy"));
  document.getElementById("fx-cut").addEventListener("click", () => toClipboard("move"));
  document.getElementById("fx-paste").addEventListener("click", () => paste());
  document.getElementById("fx-copy-to").addEventListener("click", () => copyOrMoveTo("copy"));
  document.getElementById("fx-move-to").addEventListener("click", () => copyOrMoveTo("move"));
  document.getElementById("fx-download").addEventListener("click", () => {
    const [entry] = selectedEntries();
    if (entry && !entry.is_dir) window.location.href = downloadUrl(entry);
  });
  document.getElementById("fx-rename").addEventListener("click", () => {
    const [entry] = selectedEntries();
    if (entry) renameEntry(entry);
  });
  document.getElementById("fx-delete").addEventListener("click", deleteSelected);
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      runSearch();
    } else if (event.key === "Escape") {
      searchInput.value = "";
      runSearch();
    }
  });
  window.addEventListener("storage", (event) => {
    if (event.key === CLIPBOARD_KEY) renderSelection();
  });
}

async function init() {
  wire();
  await loadLocations();
  const remembered = storageGet(LOCATION_KEY, "");
  if (remembered && location(remembered) && remembered !== state.root) await openLocation(remembered);
  pollTransfers();
  // USB drives come and go: look for changes every few seconds.
  window.setInterval(() => {
    if (!document.hidden) loadLocations({ announce: true });
  }, 5000);
}

init();
