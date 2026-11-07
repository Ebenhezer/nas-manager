// The main pane: address bar, command bar (filter / sort / view), the item
// list in six views, selection, context menus, drag & drop, infinite
// scrolling, the selection bar and the status bar.

import { apiFetch } from "../api.js";
import { formatBytes } from "../format.js";
import {
  addToAlbum,
  addToNewAlbum,
  createFolder,
  deleteFolder,
  deleteItems,
  downloadItems,
  itemWritable,
  moveItems,
  moveItemsTo,
  removeFromAlbum,
  renameFolder,
  renameItem,
  setAlbumCover,
  setFavorite,
} from "./actions.js";
import { rescan } from "./admin.js";
import { hideMenu, onLongPress, showMenu } from "./menu.js";
import {
  SIZE_PRESETS,
  SORT_FIELDS,
  VIEWS,
  album,
  hooks,
  inFolderMode,
  library,
  locationIsWritable,
  page,
  queryParams,
  resetFilters,
  setView,
  state,
} from "./state.js";
import { DRAG_TYPE } from "./tree.js";
import { uploadFiles, uploadTargetLabel } from "./uploads.js";
import {
  API,
  el,
  formatDate,
  formatSeconds,
  hideError,
  icon,
  libraryLabel,
  option,
  plural,
  serverPath,
  copyText,
  showError,
  thumbUrl,
  typeLabel,
} from "./util.js";

const content = document.getElementById("mx-content");
const itemsBox = document.getElementById("mx-items");
const emptyBox = document.getElementById("mx-empty");
const sentinel = document.getElementById("mx-sentinel");
const breadcrumb = document.getElementById("mx-breadcrumb");
const searchInput = document.getElementById("mx-search");
const statusText = document.getElementById("mx-status");
const bulkbar = document.getElementById("mx-bulkbar");
const chips = document.getElementById("mx-chips");
const recursiveSwitch = document.getElementById("mx-filter-recursive");

const VIEW_LABELS = {
  xl: ["gridXl", "Extra large icons"],
  l: ["gridL", "Large icons"],
  m: ["gridL", "Medium icons"],
  s: ["gridS", "Small icons"],
  list: ["list", "List"],
  details: ["details", "Details"],
};

let requestSerial = 0;

// ---------------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------------

async function loadFolders() {
  const loc = state.location;
  if (!inFolderMode()) {
    state.folders = [];
    state.folderWritable = loc.type === "library" && Boolean(library(loc.libraryId)?.writable);
    return;
  }
  const result = await apiFetch(`${API}/libraries/${loc.libraryId}/folders?${new URLSearchParams({ path: loc.folder })}`);
  state.folders = result.folders;
  state.folderWritable = result.writable;
}

async function loadFacets() {
  try {
    state.facets = await apiFetch(`${API}/facets?${queryParams({ forFacets: true })}`);
  } catch (_ignored) {
    state.facets = null;
  }
  renderCameraOptions();
  renderStatus();
}

// Reload the current location from the first page.
export async function reload() {
  hideError();
  const serial = (requestSerial += 1);
  state.loading = true;
  try {
    const [result] = await Promise.all([apiFetch(`${API}/items?${queryParams({ offset: 0 })}`), loadFolders()]);
    if (serial !== requestSerial) return;
    state.items = result.items;
    state.total = result.total;
    const visible = new Set(state.items.map((item) => item.id));
    for (const id of Array.from(state.selected)) if (!visible.has(id)) state.selected.delete(id);
  } catch (error) {
    if (serial !== requestSerial) return;
    state.items = [];
    state.total = 0;
    state.folders = [];
    showError(error.message);
  } finally {
    if (serial === requestSerial) state.loading = false;
  }
  render();
  loadFacets();
  fillViewport();
}

export async function loadMore() {
  if (state.loading || state.items.length >= state.total) return false;
  const serial = requestSerial;
  state.loading = true;
  try {
    const next = await apiFetch(`${API}/items?${queryParams({ offset: state.items.length })}`);
    if (serial !== requestSerial) return false;
    const start = state.items.length;
    state.items = state.items.concat(next.items);
    state.total = next.total;
    appendItems(start);
    renderStatus();
    return true;
  } catch (error) {
    showError(error.message);
    return false;
  } finally {
    if (serial === requestSerial) state.loading = false;
  }
}

// Keep loading while the sentinel is visible (tall screens, small icons).
function fillViewport() {
  window.requestAnimationFrame(async () => {
    const box = content.getBoundingClientRect();
    if (sentinel.getBoundingClientRect().top < box.bottom + 200 && (await loadMore())) fillViewport();
  });
}

new IntersectionObserver(
  (entries) => {
    if (entries.some((entry) => entry.isIntersecting)) loadMore().then((more) => more && fillViewport());
  },
  { root: content, rootMargin: "400px" },
).observe(sentinel);

// ---------------------------------------------------------------------------
// Address bar
// ---------------------------------------------------------------------------

function locationTitle() {
  const loc = state.location;
  if (loc.type === "favorites") return "Favorites";
  if (loc.type === "recent") return "Recently added";
  if (loc.type === "album") return album(loc.albumId)?.name || "Album";
  if (loc.type === "library") return libraryLabel(library(loc.libraryId) || { name: "Library" });
  return page.defaultKind === "video" ? "All videos" : "All photos & videos";
}

function renderBreadcrumb() {
  const loc = state.location;
  breadcrumb.replaceChildren();
  const crumb = (label, target, current) =>
    current
      ? el("span", { className: "mx-crumb is-current", text: label, attrs: { "aria-current": "location" } })
      : el("button", {
          className: "mx-crumb",
          text: label,
          attrs: { type: "button" },
          on: { click: () => hooks.navigate(target) },
        });
  const parts = loc.type === "library" && loc.folder ? loc.folder.split("/") : [];
  const iconName = { favorites: "star", recent: "clock", album: "album", library: "library" }[loc.type] || "image";
  breadcrumb.append(icon(iconName));
  breadcrumb.append(crumb(locationTitle(), { ...loc, folder: "" }, parts.length === 0));
  parts.forEach((part, index) => {
    breadcrumb.append(el("span", { className: "mx-crumb-sep", text: "›", attrs: { "aria-hidden": "true" } }));
    breadcrumb.append(
      crumb(part, { ...loc, folder: parts.slice(0, index + 1).join("/") }, index === parts.length - 1),
    );
  });
  const here = currentFolderPath();
  breadcrumb.title = here || "";
  if (here) {
    breadcrumb.append(
      el(
        "button",
        {
          className: "mx-crumb-copy",
          title: `Copy path on server: ${here}`,
          attrs: { type: "button", "aria-label": "Copy path on server" },
          on: { click: () => copyText(here) },
        },
        icon("copy", { size: 14 }),
      ),
    );
  }
  document.getElementById("mx-up").disabled = loc.type === "all";
}

export function goUp() {
  const loc = state.location;
  if (loc.type === "library" && loc.folder) {
    const folder = loc.folder.includes("/") ? loc.folder.slice(0, loc.folder.lastIndexOf("/")) : "";
    hooks.navigate({ ...loc, folder });
  } else if (loc.type !== "all") {
    hooks.navigate({ type: "all" });
  }
}

// ---------------------------------------------------------------------------
// Command bar: filters, sort, view
// ---------------------------------------------------------------------------

const filterControls = {
  kind: document.getElementById("mx-filter-kind"),
  orientation: document.getElementById("mx-filter-orientation"),
  dateFrom: document.getElementById("mx-filter-date-from"),
  dateTo: document.getElementById("mx-filter-date-to"),
  camera: document.getElementById("mx-filter-camera"),
  size: document.getElementById("mx-filter-size"),
  gps: document.getElementById("mx-filter-gps"),
};

function isoDate(date) {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 10);
}

const DATE_PRESETS = [
  ["Today", () => [new Date(), new Date()]],
  ["Last 7 days", () => [new Date(Date.now() - 6 * 86400000), new Date()]],
  ["Last 30 days", () => [new Date(Date.now() - 29 * 86400000), new Date()]],
  ["This year", () => [new Date(new Date().getFullYear(), 0, 1), new Date()]],
  ["Last year", () => [new Date(new Date().getFullYear() - 1, 0, 1), new Date(new Date().getFullYear() - 1, 11, 31)]],
];

function renderCameraOptions() {
  const select = filterControls.camera;
  const keep = state.filters.camera;
  select.replaceChildren(option("", "Any camera"));
  const cameras = state.facets ? state.facets.cameras : [];
  for (const camera of cameras) select.append(option(camera.name, `${camera.name} (${camera.count})`));
  if (keep && !cameras.some((camera) => camera.name === keep)) select.append(option(keep, keep));
  select.value = keep;
}

function syncFilterControls() {
  for (const [key, control] of Object.entries(filterControls)) control.value = state.filters[key];
  recursiveSwitch.checked = state.filters.recursive;
  document.getElementById("mx-recursive-wrap").classList.toggle("d-none", state.location.type !== "library");
  if (searchInput !== document.activeElement) searchInput.value = state.filters.q;
  searchInput.placeholder = `Search ${locationTitle()}`;
}

function activeFilterChips() {
  const f = state.filters;
  const list = [];
  const kindNames = { image: "Photos", video: "Videos" };
  if (f.kind && f.kind !== page.defaultKind) list.push(["kind", `Type: ${kindNames[f.kind]}`, ""]);
  if (!f.kind && page.defaultKind) list.push(["kind", "Type: photos and videos", page.defaultKind]);
  if (f.q) list.push(["q", `Search: ${f.q}`, ""]);
  if (f.dateFrom || f.dateTo) list.push(["date", `Taken: ${f.dateFrom || "…"} – ${f.dateTo || "…"}`, ""]);
  if (f.camera) list.push(["camera", `Camera: ${f.camera}`, ""]);
  if (f.size) list.push(["size", SIZE_PRESETS[f.size].label, ""]);
  if (f.orientation) list.push(["orientation", `Orientation: ${f.orientation}`, ""]);
  if (f.gps) list.push(["gps", f.gps === "yes" ? "Has location" : "No location", ""]);
  if (f.recursive && state.location.type === "library") list.push(["recursive", "Including subfolders", false]);
  return list;
}

function renderChips() {
  chips.replaceChildren();
  const active = activeFilterChips();
  for (const [key, label, reset] of active) {
    chips.append(
      el(
        "button",
        {
          className: "mx-chip",
          attrs: { type: "button", "aria-label": `Remove filter ${label}` },
          on: {
            click: () => {
              if (key === "date") {
                state.filters.dateFrom = "";
                state.filters.dateTo = "";
              } else {
                state.filters[key] = reset;
              }
              hooks.filtersChanged();
            },
          },
        },
        el("span", { text: label }),
        icon("x", { size: 12 }),
      ),
    );
  }
  const count = active.filter(([key]) => key !== "q" && key !== "recursive").length;
  const button = document.getElementById("mx-filter-button");
  button.classList.toggle("active", Boolean(count));
  button.lastChild.textContent = count ? ` Filter (${count})` : " Filter";
  chips.classList.toggle("d-none", !active.length);
}

function renderSortMenu() {
  const menu = document.getElementById("mx-sort-menu");
  menu.replaceChildren();
  const entry = (label, active, handler) =>
    el(
      "li",
      {},
      el(
        "button",
        {
          className: `dropdown-item d-flex align-items-center gap-2${active ? " active" : ""}`,
          attrs: { type: "button", "aria-pressed": active ? "true" : "false" },
          on: { click: handler },
        },
        active ? icon("check") : el("span", { className: "mx-icon-space" }),
        el("span", { text: label }),
      ),
    );
  for (const [field, label] of Object.entries(SORT_FIELDS)) {
    menu.append(entry(label, state.sort.field === field, () => setSort(field, state.sort.dir)));
  }
  menu.append(el("li", {}, el("hr", { className: "dropdown-divider" })));
  menu.append(entry("Ascending", state.sort.dir === "asc", () => setSort(state.sort.field, "asc")));
  menu.append(entry("Descending", state.sort.dir === "desc", () => setSort(state.sort.field, "desc")));
  const button = document.getElementById("mx-sort-button");
  button.lastChild.textContent = ` ${SORT_FIELDS[state.sort.field]}`;
  button.title = `Sorted by ${SORT_FIELDS[state.sort.field].toLowerCase()}, ${state.sort.dir === "asc" ? "ascending" : "descending"}`;
}

function setSort(field, dir) {
  state.sort = { field, dir };
  hooks.filtersChanged();
}

function renderViewButtons() {
  const group = document.getElementById("mx-view-buttons");
  group.replaceChildren();
  for (const view of VIEWS) {
    const [iconName, label] = VIEW_LABELS[view];
    const active = state.view === view;
    group.append(
      el(
        "button",
        {
          className: `btn btn-outline-secondary mx-view-${view}${active ? " active" : ""}`,
          attrs: { type: "button", title: label, "aria-label": label, "aria-pressed": active ? "true" : "false" },
          on: {
            click: () => {
              setView(view);
              renderViewButtons();
              render();
              fillViewport();
            },
          },
        },
        icon(iconName, { size: view === "m" ? 13 : 16 }),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Items
// ---------------------------------------------------------------------------

function selectedItems() {
  return state.items.filter((item) => state.selected.has(item.id));
}

function thumbnail(item, size = "small") {
  const box = el("span", { className: "mx-thumb" });
  if (item.thumb_state === "failed" || item.thumb_state === "unsupported") {
    box.append(icon(item.kind === "video" ? "video" : "image", { size: 28, className: "mx-thumb-fallback" }));
    return box;
  }
  const img = el("img", { attrs: { loading: "lazy", decoding: "async", alt: "", src: thumbUrl(item.id, size), draggable: "false" } });
  img.addEventListener("error", () =>
    img.replaceWith(icon(item.kind === "video" ? "video" : "image", { size: 28, className: "mx-thumb-fallback" })),
  );
  box.append(img);
  return box;
}

function folderThumb(folder) {
  const box = el("span", { className: "mx-thumb mx-thumb-folder" });
  box.append(icon("folder", { size: 56, className: "mx-folder-icon" }));
  if (folder.cover_item_id) {
    const img = el("img", { className: "mx-folder-cover", attrs: { loading: "lazy", alt: "", src: thumbUrl(folder.cover_item_id), draggable: "false" } });
    img.addEventListener("error", () => img.remove());
    box.append(img);
  }
  return box;
}

function badges(item) {
  const wrap = el("span", { className: "mx-badges" });
  if (item.is_favorite) wrap.append(el("span", { className: "mx-badge mx-badge-fav", attrs: { title: "Favorite" } }, icon("star", { size: 12, filled: true })));
  if (item.kind === "video") {
    wrap.append(
      el("span", { className: "mx-badge mx-badge-video" }, icon("play", { size: 10, filled: true }), item.duration_seconds ? ` ${formatSeconds(item.duration_seconds)}` : ""),
    );
  }
  return wrap;
}

// Where an item lives; the library is left out while browsing that library.
function locationLabel(lib, folder) {
  if (state.location.type === "library") return folder || "(top)";
  return `${lib ? libraryLabel(lib) : ""}${folder ? ` / ${folder}` : ""}`;
}

// "Copy path on server" for a path inside a library (administrators only).
function copyPathEntry(lib, relPath) {
  const path = serverPath(lib, relPath);
  return path ? [{ label: "Copy path on server", icon: "copy", action: () => copyText(path) }] : [];
}

function currentFolderPath() {
  const loc = state.location;
  return loc.type === "library" ? serverPath(library(loc.libraryId), loc.folder) : null;
}

function itemElement(item, index) {
  const selected = state.selected.has(item.id);
  const common = {
    className: `mx-entry mx-item${selected ? " is-selected" : ""}`,
    attrs: {
      role: "option",
      "aria-selected": selected ? "true" : "false",
      tabindex: "-1",
      draggable: "true",
      title: `${item.name}\n${formatDate(item.taken_at)} · ${formatBytes(item.size)}`,
      "aria-label": item.name,
    },
    dataset: { index },
  };
  const check = el(
    "span",
    { className: "mx-check", attrs: { "aria-hidden": "true" } },
    icon("check", { size: 14 }),
  );
  if (state.view === "details") {
    const row = el("tr", common);
    const lib = library(item.library_id);
    const folder = item.rel_path.includes("/") ? item.rel_path.slice(0, item.rel_path.lastIndexOf("/")) : "";
    row.append(
      el("td", { className: "mx-col-name" }, check, thumbnail(item), el("span", { className: "text-truncate", text: item.name }), badges(item)),
      el("td", { text: formatDate(item.taken_at) }),
      el("td", { text: typeLabel(item) }),
      el("td", { text: item.width && item.height ? `${item.width} × ${item.height}` : "" }),
      el("td", { className: "text-end", text: formatBytes(item.size) }),
      el("td", { className: "text-end", text: item.duration_seconds ? formatSeconds(item.duration_seconds) : "" }),
      el("td", { text: [item.camera_make, item.camera_model].filter(Boolean).join(" ") }),
      el("td", { text: locationLabel(lib, folder), title: serverPath(lib, item.rel_path) || undefined }),
    );
    return row;
  }
  const entry = el("div", common, check);
  const thumb = thumbnail(item, state.view === "xl" ? "large" : "small");
  thumb.append(badges(item));
  entry.append(thumb);
  if (state.view === "list") {
    entry.append(
      el("span", { className: "mx-entry-name text-truncate", text: item.name }),
      el("span", { className: "mx-entry-meta", text: formatDate(item.taken_at, { time: false }) }),
      el("span", { className: "mx-entry-meta text-end", text: formatBytes(item.size) }),
    );
  } else if (state.view !== "s") {
    entry.append(el("span", { className: "mx-entry-name text-truncate", text: item.name }));
  }
  return entry;
}

function folderElement(folder, index) {
  const common = {
    className: "mx-entry mx-folder",
    attrs: { role: "option", tabindex: "-1", "aria-selected": "false", title: folder.name, "aria-label": `Folder ${folder.name}` },
    dataset: { folderIndex: index },
  };
  if (state.view === "details") {
    return el(
      "tr",
      common,
      el("td", { className: "mx-col-name" }, el("span", { className: "mx-check-space" }), icon("folder", { size: 20, className: "mx-folder-icon" }), el("span", { className: "text-truncate", text: folder.name })),
      el("td", { text: folder.latest_taken_at ? formatDate(folder.latest_taken_at) : "" }),
      el("td", { text: "Folder" }),
      el("td"),
      el("td", { className: "text-end", text: plural(folder.item_count, "item") }),
      el("td"),
      el("td"),
      el("td"),
    );
  }
  const entry = el("div", common);
  if (state.view === "list") {
    entry.append(
      el("span", { className: "mx-check-space" }),
      el("span", { className: "mx-thumb" }, icon("folder", { size: 22, className: "mx-folder-icon" })),
      el("span", { className: "mx-entry-name text-truncate", text: folder.name }),
      el("span", { className: "mx-entry-meta", text: "Folder" }),
      el("span", { className: "mx-entry-meta text-end", text: plural(folder.item_count, "item") }),
    );
  } else {
    entry.append(folderThumb(folder), el("span", { className: "mx-entry-name text-truncate", text: folder.name }));
  }
  return entry;
}

const DETAIL_COLUMNS = [
  ["Name", "name"],
  ["Date taken", "taken"],
  ["Type", "type"],
  ["Dimensions", null],
  ["Size", "size"],
  ["Duration", "duration"],
  ["Camera", null],
  ["Location", null],
];

function detailsTable() {
  const head = el("tr");
  for (const [label, field] of DETAIL_COLUMNS) {
    const th = el("th", { attrs: { scope: "col" }, className: label === "Size" || label === "Duration" ? "text-end" : "" });
    if (field) {
      const active = state.sort.field === field;
      const arrow = active ? (state.sort.dir === "asc" ? " ▲" : " ▼") : "";
      th.setAttribute("aria-sort", active ? (state.sort.dir === "asc" ? "ascending" : "descending") : "none");
      th.append(
        el("button", {
          className: "mx-th-sort",
          text: `${label}${arrow}`,
          attrs: { type: "button" },
          on: {
            click: () => setSort(field, active && state.sort.dir === "asc" ? "desc" : active ? "asc" : field === "name" || field === "type" ? "asc" : "desc"),
          },
        }),
      );
    } else {
      th.textContent = label;
    }
    head.append(th);
  }
  return el("table", { className: "table table-sm table-hover align-middle mb-0 mx-details" }, el("thead", {}, head), el("tbody"));
}

function listContainer() {
  return state.view === "details" ? itemsBox.querySelector("tbody") : itemsBox;
}

function render() {
  hideMenu();
  renderBreadcrumb();
  syncFilterControls();
  renderChips();
  renderSortMenu();
  itemsBox.dataset.view = state.view;
  itemsBox.className = `mx-items ${state.view === "details" ? "mx-items-table" : state.view === "list" ? "mx-items-list" : "mx-items-grid"}`;
  itemsBox.replaceChildren();
  if (state.view === "details") itemsBox.append(detailsTable());
  const box = listContainer();
  state.folders.forEach((folder, index) => box.append(folderElement(folder, index)));
  appendItems(0);
  const empty = !state.items.length && !state.folders.length && !state.loading;
  emptyBox.classList.toggle("d-none", !empty);
  emptyBox.textContent = emptyMessage();
  renderSelection();
  renderStatus();
  rescanButton();
  const newFolder = document.getElementById("mx-new-folder");
  if (newFolder) newFolder.classList.toggle("d-none", !locationIsWritable());
  const focusable = itemsBox.querySelector(".mx-entry");
  if (focusable) focusable.tabIndex = 0;
}

function emptyMessage() {
  const loc = state.location;
  if (activeFilterChips().some(([key]) => key !== "recursive")) return "No items match the filters.";
  if (loc.type === "favorites") return "No favorites yet. Star photos and videos to find them here.";
  if (loc.type === "album") return "This album is empty. Select items and choose “Add to album”.";
  if (loc.type === "library" && locationIsWritable()) return "This folder is empty. Drop photos or videos here to upload them.";
  if (loc.type === "library") return "This folder is empty.";
  return page.canWrite ? "Nothing here yet. Drop photos or videos here to upload them." : "Nothing here yet.";
}

function appendItems(start) {
  const box = listContainer();
  for (let index = start; index < state.items.length; index += 1) box.append(itemElement(state.items[index], index));
  if (start > 0) emptyBox.classList.add("d-none");
}

export function rerender() {
  const scroll = content.scrollTop;
  render();
  content.scrollTop = scroll;
}

function renderStatus() {
  const parts = [];
  if (state.folders.length) parts.push(plural(state.folders.length, "folder"));
  parts.push(plural(state.total, "item"));
  if (state.facets && state.facets.total === state.total && state.total) parts.push(formatBytes(state.facets.total_bytes));
  if (state.items.length < state.total) parts.push(`${state.items.length} loaded`);
  const selected = selectedItems();
  if (selected.length) {
    parts.push(`${selected.length} selected (${formatBytes(selected.reduce((sum, item) => sum + item.size, 0))})`);
  }
  statusText.textContent = parts.join(" · ");
}

function rescanButton() {
  const button = document.getElementById("mx-rescan");
  const loc = state.location;
  const lib = loc.type === "library" ? library(loc.libraryId) : state.libraries.find((entry) => entry.is_own);
  const allowed = lib && (page.isAdmin || (lib.is_own && page.canWrite));
  button.disabled = !allowed;
  button.dataset.libraryId = allowed ? String(lib.id) : "";
}

// ---------------------------------------------------------------------------
// Selection
// ---------------------------------------------------------------------------

function renderSelection() {
  const selected = selectedItems();
  for (const entry of itemsBox.querySelectorAll(".mx-item")) {
    const item = state.items[Number(entry.dataset.index)];
    const on = state.selected.has(item.id);
    entry.classList.toggle("is-selected", on);
    entry.setAttribute("aria-selected", on ? "true" : "false");
  }
  itemsBox.classList.toggle("has-selection", selected.length > 0);
  bulkbar.classList.toggle("d-none", !selected.length);
  if (!selected.length) {
    renderStatus();
    return;
  }
  document.getElementById("mx-selection-count").textContent = `${selected.length} selected`;
  const writable = selected.every(itemWritable);
  document.getElementById("mx-bulk-move").classList.toggle("d-none", !writable);
  document.getElementById("mx-bulk-delete").classList.toggle("d-none", !writable);
  document.getElementById("mx-bulk-remove-album").classList.toggle("d-none", !(page.canWrite && state.location.type === "album"));
  document.getElementById("mx-bulk-album").classList.toggle("d-none", !page.canWrite);
  const allFav = selected.every((item) => item.is_favorite);
  const favButton = document.getElementById("mx-bulk-favorite");
  favButton.lastChild.textContent = allFav ? " Unfavorite" : " Favorite";
  favButton.dataset.favorite = allFav ? "false" : "true";
  renderStatus();
}

function select(index, { toggle = false, range = false } = {}) {
  const item = state.items[index];
  if (!item) return;
  if (range && state.anchor >= 0) {
    if (!toggle) state.selected.clear();
    const [from, to] = [Math.min(state.anchor, index), Math.max(state.anchor, index)];
    for (let i = from; i <= to; i += 1) state.selected.add(state.items[i].id);
  } else if (toggle) {
    if (state.selected.has(item.id)) state.selected.delete(item.id);
    else state.selected.add(item.id);
    state.anchor = index;
  } else {
    state.selected.clear();
    state.selected.add(item.id);
    state.anchor = index;
  }
  renderSelection();
}

export function clearSelection() {
  state.selected.clear();
  state.anchor = -1;
  renderSelection();
}

function selectAll() {
  for (const item of state.items) state.selected.add(item.id);
  renderSelection();
}

// ---------------------------------------------------------------------------
// Pointer interaction
// ---------------------------------------------------------------------------

let lastPointerType = "mouse";

function entryOf(target) {
  return target.closest ? target.closest(".mx-entry") : null;
}

function openFolder(folder) {
  hooks.navigate({ ...state.location, folder: folder.path });
}

function activate(entry) {
  if (entry.classList.contains("mx-folder")) {
    openFolder(state.folders[Number(entry.dataset.folderIndex)]);
  } else {
    hooks.openViewer(Number(entry.dataset.index));
  }
}

itemsBox.addEventListener("pointerdown", (event) => {
  lastPointerType = event.pointerType;
});

itemsBox.addEventListener("click", (event) => {
  const entry = entryOf(event.target);
  if (!entry) return;
  focusEntry(entry);
  if (entry.classList.contains("mx-folder")) {
    if (lastPointerType !== "mouse") activate(entry);
    return;
  }
  const index = Number(entry.dataset.index);
  const onCheck = Boolean(event.target.closest(".mx-check"));
  const selecting = state.selected.size > 0;
  if (lastPointerType !== "mouse" && !onCheck && !selecting) {
    hooks.openViewer(index);
    return;
  }
  select(index, {
    toggle: onCheck || event.ctrlKey || event.metaKey || (lastPointerType !== "mouse" && selecting),
    range: event.shiftKey,
  });
});

itemsBox.addEventListener("dblclick", (event) => {
  const entry = entryOf(event.target);
  if (entry && !event.target.closest(".mx-check")) activate(entry);
});

function itemMenuEntries(items) {
  const single = items.length === 1 ? items[0] : null;
  const writable = items.every(itemWritable);
  const allFav = items.every((item) => item.is_favorite);
  const entries = [];
  if (single) entries.push({ label: "Open", icon: single.kind === "video" ? "play" : "image", action: () => hooks.openViewer(state.items.indexOf(single)) });
  entries.push(
    { label: items.length > 1 ? `Download ${items.length} as ZIP` : "Download", icon: "download", action: () => downloadItems(items) },
    { label: allFav ? "Remove from favorites" : "Add to favorites", icon: "star", filled: !allFav, action: () => setFavorite(items, !allFav) },
  );
  if (page.canWrite) {
    entries.push({ label: "Add to album…", icon: "album", action: () => albumMenu(items) });
    if (state.location.type === "album") {
      entries.push({ label: "Remove from album", icon: "x", action: () => removeFromAlbum(items, state.location.albumId) });
      if (single) entries.push({ label: "Use as album cover", icon: "image", action: () => setAlbumCover(state.location.albumId, single) });
    }
  }
  if (writable) {
    entries.push({ divider: true });
    if (single) entries.push({ label: "Rename", icon: "edit", action: () => renameItem(single) });
    entries.push(
      { label: "Move to…", icon: "move", action: () => moveItems(items) },
      { divider: true },
      { label: "Delete", icon: "trash", danger: true, action: () => deleteItems(items) },
    );
  }
  if (single) {
    entries.push(
      { divider: true },
      ...copyPathEntry(library(single.library_id), single.rel_path),
      { label: "Properties", icon: "info", action: () => hooks.openViewer(state.items.indexOf(single), { info: true }) },
    );
  }
  return entries;
}

let menuPoint = { x: 0, y: 0 };

function albumMenu(items) {
  const entries = [{ header: "Add to album" }];
  for (const entry of state.albums) entries.push({ label: entry.name, icon: "album", action: () => addToAlbum(items, entry.id) });
  entries.push({ divider: true }, { label: "New album…", icon: "plus", action: () => addToNewAlbum(items) });
  showMenu(menuPoint.x, menuPoint.y, entries);
}

function folderMenuEntries(folder) {
  const loc = state.location;
  const entries = [
    { label: "Open", icon: "folder", action: () => openFolder(folder) },
    ...copyPathEntry(library(loc.libraryId), folder.path),
  ];
  if (locationIsWritable()) {
    entries.push(
      { label: "Rename", icon: "edit", action: () => renameFolder(loc.libraryId, folder) },
      { divider: true },
      { label: "Delete", icon: "trash", danger: true, action: () => deleteFolder(loc.libraryId, folder) },
    );
  }
  return entries;
}

function backgroundMenuEntries() {
  const loc = state.location;
  const entries = [];
  if (locationIsWritable()) {
    entries.push(
      { label: "New folder", icon: "folderPlus", action: () => createFolder(loc.libraryId, loc.folder) },
      { label: "Upload here…", icon: "upload", action: () => document.getElementById("mx-upload-input").click() },
      { divider: true },
    );
  }
  if (state.items.length) entries.push({ label: "Select all", icon: "check", action: selectAll });
  const here = currentFolderPath();
  if (here) entries.push({ label: "Copy path on server", icon: "copy", action: () => copyText(here) });
  entries.push({ label: "Refresh", icon: "refresh", action: () => reload() });
  return entries;
}

function contextMenu(event) {
  event.preventDefault();
  menuPoint = { x: event.clientX, y: event.clientY };
  const entry = entryOf(event.target);
  if (!entry) {
    showMenu(event.clientX, event.clientY, backgroundMenuEntries());
    return;
  }
  if (entry.classList.contains("mx-folder")) {
    showMenu(event.clientX, event.clientY, folderMenuEntries(state.folders[Number(entry.dataset.folderIndex)]));
    return;
  }
  const index = Number(entry.dataset.index);
  if (!state.selected.has(state.items[index].id)) select(index);
  showMenu(event.clientX, event.clientY, itemMenuEntries(selectedItems()));
}

content.addEventListener("contextmenu", contextMenu);
onLongPress(content, contextMenu);

// Rubber-band selection in the icon views (mouse only).
let band = null;

content.addEventListener("pointerdown", (event) => {
  if (event.pointerType !== "mouse" || event.button !== 0) return;
  if (entryOf(event.target) || state.view === "details") return;
  if (event.target.closest("button, a, input, select")) return;
  // Leave the scrollbar alone.
  if (event.offsetX >= content.clientWidth || event.offsetY >= content.clientHeight) return;
  const origin = content.getBoundingClientRect();
  band = {
    x: event.clientX - origin.left + content.scrollLeft,
    y: event.clientY - origin.top + content.scrollTop,
    additive: event.ctrlKey || event.metaKey,
    keep: event.ctrlKey || event.metaKey ? new Set(state.selected) : new Set(),
    moved: false,
    box: null,
  };
  content.setPointerCapture(event.pointerId);
});

content.addEventListener("pointermove", (event) => {
  if (!band) return;
  const origin = content.getBoundingClientRect();
  const x = event.clientX - origin.left + content.scrollLeft;
  const y = event.clientY - origin.top + content.scrollTop;
  if (!band.moved && Math.hypot(x - band.x, y - band.y) < 5) return;
  if (!band.box) {
    band.box = el("div", { className: "mx-marquee" });
    content.append(band.box);
  }
  band.moved = true;
  const left = Math.min(x, band.x);
  const top = Math.min(y, band.y);
  const width = Math.abs(x - band.x);
  const height = Math.abs(y - band.y);
  Object.assign(band.box.style, { left: `${left}px`, top: `${top}px`, width: `${width}px`, height: `${height}px` });
  const area = band.box.getBoundingClientRect();
  state.selected = new Set(band.keep);
  for (const entry of itemsBox.querySelectorAll(".mx-item")) {
    const rect = entry.getBoundingClientRect();
    if (rect.right > area.left && rect.left < area.right && rect.bottom > area.top && rect.top < area.bottom) {
      state.selected.add(state.items[Number(entry.dataset.index)].id);
    }
  }
  renderSelection();
});

function endBand() {
  if (!band) return;
  if (band.box) band.box.remove();
  // A plain click on the background clears the selection.
  if (!band.moved && !band.additive) clearSelection();
  band = null;
}

content.addEventListener("pointerup", endBand);
content.addEventListener("pointercancel", endBand);

// ---------------------------------------------------------------------------
// Keyboard
// ---------------------------------------------------------------------------

function focusEntry(entry) {
  for (const other of itemsBox.querySelectorAll('.mx-entry[tabindex="0"]')) other.tabIndex = -1;
  entry.tabIndex = 0;
  entry.focus({ preventScroll: true });
  entry.scrollIntoView({ block: "nearest" });
}

function columns(entries) {
  if (state.view === "list" || state.view === "details" || entries.length < 2) return 1;
  const top = entries[0].offsetTop;
  let count = 0;
  while (count < entries.length && entries[count].offsetTop === top) count += 1;
  return Math.max(1, count);
}

itemsBox.addEventListener("keydown", async (event) => {
  const entry = entryOf(event.target);
  if (!entry) return;
  const entries = Array.from(itemsBox.querySelectorAll(".mx-entry"));
  const position = entries.indexOf(entry);
  const isItem = entry.classList.contains("mx-item");
  const index = isItem ? Number(entry.dataset.index) : -1;
  const steps = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -columns(entries), ArrowDown: columns(entries) };
  if (event.key in steps && !event.altKey) {
    event.preventDefault();
    if (state.view === "list" || state.view === "details") {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") return;
    }
    let target = entries[Math.max(0, Math.min(entries.length - 1, position + steps[event.key]))];
    if (position + steps[event.key] >= entries.length && (await loadMore())) {
      target = itemsBox.querySelectorAll(".mx-entry")[position + steps[event.key]] || target;
    }
    focusEntry(target);
    if (target.classList.contains("mx-item") && (event.shiftKey || !(event.ctrlKey || event.metaKey))) {
      select(Number(target.dataset.index), { range: event.shiftKey });
    }
  } else if (event.key === "Home" || event.key === "End") {
    event.preventDefault();
    focusEntry(event.key === "Home" ? entries[0] : entries[entries.length - 1]);
  } else if (event.key === "Enter") {
    event.preventDefault();
    activate(entry);
  } else if (event.key === " " && isItem) {
    event.preventDefault();
    select(index, { toggle: true });
  } else if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
    event.preventDefault();
    const rect = entry.getBoundingClientRect();
    contextMenu({ preventDefault() {}, clientX: rect.left + 20, clientY: rect.top + 20, target: entry });
  } else if (event.key === "F2") {
    event.preventDefault();
    if (isItem) renameItem(state.items[index]);
    else if (locationIsWritable()) renameFolder(state.location.libraryId, state.folders[Number(entry.dataset.folderIndex)]);
  } else if (event.key === "Delete" && state.selected.size) {
    event.preventDefault();
    deleteItems(selectedItems());
  }
});

content.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
    event.preventDefault();
    selectAll();
  } else if (event.key === "Escape" && state.selected.size) {
    clearSelection();
  } else if (event.key === "Backspace" && !event.target.closest("input")) {
    event.preventDefault();
    goUp();
  }
});

document.addEventListener("keydown", (event) => {
  if (!document.getElementById("mx-lightbox").classList.contains("d-none")) return;
  if (event.altKey && event.key === "ArrowUp") {
    event.preventDefault();
    goUp();
  } else if (event.altKey && event.key === "ArrowLeft") {
    event.preventDefault();
    window.history.back();
  } else if (event.key === "/" && !event.target.closest("input, textarea, select")) {
    event.preventDefault();
    searchInput.focus();
  }
});

// ---------------------------------------------------------------------------
// Drag & drop: items onto folders, files from the desktop into the folder
// ---------------------------------------------------------------------------

itemsBox.addEventListener("dragstart", (event) => {
  const entry = entryOf(event.target);
  if (!entry || !entry.classList.contains("mx-item")) {
    event.preventDefault();
    return;
  }
  const index = Number(entry.dataset.index);
  if (!state.selected.has(state.items[index].id)) select(index);
  const ids = selectedItems().map((item) => item.id);
  event.dataTransfer.setData(DRAG_TYPE, JSON.stringify(ids));
  event.dataTransfer.effectAllowed = "copyMove";
  const label = el("div", { className: "mx-drag-label", text: plural(ids.length, "item") });
  document.body.append(label);
  event.dataTransfer.setDragImage(label, 12, 12);
  window.setTimeout(() => label.remove(), 0);
});

itemsBox.addEventListener("dragover", (event) => {
  const entry = entryOf(event.target);
  if (!entry || !entry.classList.contains("mx-folder") || !locationIsWritable()) return;
  if (!event.dataTransfer.types.includes(DRAG_TYPE)) return;
  event.preventDefault();
  entry.classList.add("is-drop-target");
});

itemsBox.addEventListener("dragleave", (event) => {
  const entry = entryOf(event.target);
  if (entry) entry.classList.remove("is-drop-target");
});

itemsBox.addEventListener("drop", (event) => {
  const entry = entryOf(event.target);
  if (!entry || !entry.classList.contains("mx-folder")) return;
  entry.classList.remove("is-drop-target");
  const raw = event.dataTransfer.getData(DRAG_TYPE);
  if (!raw) return;
  event.preventDefault();
  event.stopPropagation();
  const ids = new Set(JSON.parse(raw));
  const folder = state.folders[Number(entry.dataset.folderIndex)];
  moveItemsTo(state.items.filter((item) => ids.has(item.id)), state.location.libraryId, folder.path);
});

const overlay = document.getElementById("mx-drop-overlay");
let dragDepth = 0;

function hasFiles(event) {
  return page.canWrite && event.dataTransfer && Array.from(event.dataTransfer.types).includes("Files");
}

content.addEventListener("dragenter", (event) => {
  if (!hasFiles(event)) return;
  dragDepth += 1;
  document.getElementById("mx-drop-label").textContent = `Drop to upload to ${uploadTargetLabel()}`;
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
// Wiring
// ---------------------------------------------------------------------------

let searchTimer = null;

export function wireBrowser() {
  const sizeSelect = filterControls.size;
  for (const [value, preset] of Object.entries(SIZE_PRESETS)) sizeSelect.append(option(value, preset.label));
  const presets = document.getElementById("mx-date-presets");
  for (const [label, range] of DATE_PRESETS) {
    presets.append(
      el("button", {
        className: "btn btn-sm btn-outline-secondary",
        text: label,
        attrs: { type: "button" },
        on: {
          click: () => {
            const [from, to] = range();
            state.filters.dateFrom = isoDate(from);
            state.filters.dateTo = isoDate(to);
            hooks.filtersChanged();
          },
        },
      }),
    );
  }
  const form = document.getElementById("mx-filter-form");
  form.addEventListener("submit", (event) => event.preventDefault());
  form.addEventListener("change", () => {
    for (const [key, control] of Object.entries(filterControls)) state.filters[key] = control.value;
    hooks.filtersChanged();
  });
  document.getElementById("mx-filter-clear").addEventListener("click", () => {
    const q = state.filters.q;
    resetFilters();
    state.filters.q = q;
    hooks.filtersChanged();
  });
  recursiveSwitch.addEventListener("change", () => {
    state.filters.recursive = recursiveSwitch.checked;
    hooks.filtersChanged();
  });
  searchInput.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      state.filters.q = searchInput.value.trim();
      hooks.filtersChanged({ replace: true });
    }, 300);
  });
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && searchInput.value) {
      searchInput.value = "";
      state.filters.q = "";
      hooks.filtersChanged({ replace: true });
    } else if (event.key === "ArrowDown") {
      const first = itemsBox.querySelector(".mx-entry");
      if (first) {
        event.preventDefault();
        focusEntry(first);
      }
    }
  });
  document.getElementById("mx-back").addEventListener("click", () => window.history.back());
  document.getElementById("mx-up").addEventListener("click", goUp);
  document.getElementById("mx-refresh").addEventListener("click", () => Promise.all([reload(), hooks.refreshTree()]));
  const rescanTrigger = document.getElementById("mx-rescan");
  rescanTrigger.addEventListener("click", () => {
    if (rescanTrigger.dataset.libraryId) rescan(Number(rescanTrigger.dataset.libraryId));
  });
  const newFolder = document.getElementById("mx-new-folder");
  if (newFolder) {
    newFolder.addEventListener("click", () => {
      if (locationIsWritable()) createFolder(state.location.libraryId, state.location.folder);
    });
  }

  // Selection bar
  document.getElementById("mx-bulk-clear").addEventListener("click", clearSelection);
  document.getElementById("mx-bulk-favorite").addEventListener("click", (event) =>
    setFavorite(selectedItems(), event.currentTarget.dataset.favorite !== "false"),
  );
  document.getElementById("mx-bulk-move").addEventListener("click", () => moveItems(selectedItems()));
  document.getElementById("mx-bulk-download").addEventListener("click", () => downloadItems(selectedItems()));
  document.getElementById("mx-bulk-delete").addEventListener("click", () => deleteItems(selectedItems()));
  document.getElementById("mx-bulk-remove-album").addEventListener("click", () =>
    removeFromAlbum(selectedItems(), state.location.albumId),
  );
  document.getElementById("mx-bulk-album").addEventListener("show.bs.dropdown", () => {
    const menu = document.getElementById("mx-bulk-album-menu");
    menu.replaceChildren();
    for (const entry of state.albums) {
      menu.append(
        el("li", {}, el("button", {
          className: "dropdown-item",
          text: `${entry.name} (${entry.item_count})`,
          attrs: { type: "button" },
          on: { click: () => addToAlbum(selectedItems(), entry.id) },
        })),
      );
    }
    if (state.albums.length) menu.append(el("li", {}, el("hr", { className: "dropdown-divider" })));
    menu.append(
      el("li", {}, el("button", {
        className: "dropdown-item",
        text: "New album…",
        attrs: { type: "button" },
        on: { click: () => addToNewAlbum(selectedItems()) },
      })),
    );
  });
  renderViewButtons();
}

export { selectedItems };
