// Navigation pane: quick access, a lazily loaded folder tree per library,
// and the user's albums. Folders of a writable library and albums accept
// dropped items (move / add to album).

import { apiFetch } from "../api.js";
import { addToAlbum, createAlbum, createFolder, deleteAlbum, deleteFolder, moveItemsTo, renameAlbum, renameFolder } from "./actions.js";
import { onLongPress, showMenu } from "./menu.js";
import { hooks, page, state } from "./state.js";
import { API, el, icon, libraryLabel, showError } from "./util.js";

const body = document.getElementById("mx-nav-body");

// "<libraryId>:<path>" -> list of child folders (loaded) ; expanded keys.
const children = new Map();
const expanded = new Set();

function key(libraryId, path) {
  return `${libraryId}:${path}`;
}

export const DRAG_TYPE = "application/x-nas-media";

async function loadChildren(libraryId, path) {
  try {
    const result = await apiFetch(`${API}/libraries/${libraryId}/folders?${new URLSearchParams({ path })}`);
    children.set(key(libraryId, path), result.folders);
  } catch (error) {
    children.set(key(libraryId, path), []);
    showError(error.message);
  }
}

// Reload the cached children of ``path`` (all of the library when omitted).
export async function refreshTree(libraryId, path) {
  if (libraryId === undefined) {
    children.clear();
  } else if (path === undefined) {
    for (const cached of Array.from(children.keys())) {
      if (cached.startsWith(`${libraryId}:`)) children.delete(cached);
    }
  } else {
    children.delete(key(libraryId, path));
  }
  const reloads = [];
  for (const open of expanded) {
    const [id, ...rest] = open.split(":");
    if (!children.has(open)) reloads.push(loadChildren(Number(id), rest.join(":")));
  }
  await Promise.all(reloads);
  renderTree();
}

// Expand the tree down to the current location.
export async function revealLocation() {
  const loc = state.location;
  if (loc.type !== "library") return;
  const parts = loc.folder ? loc.folder.split("/") : [];
  // The library root and every ancestor of the folder, so the folder shows.
  const paths = [""];
  for (let index = 1; index < parts.length; index += 1) paths.push(parts.slice(0, index).join("/"));
  for (const path of paths) {
    const k = key(loc.libraryId, path);
    expanded.add(k);
    if (!children.has(k)) await loadChildren(loc.libraryId, path);
  }
}

function isActive(target) {
  const loc = state.location;
  if (target.type !== loc.type) return false;
  if (target.type === "library") return target.libraryId === loc.libraryId && target.folder === loc.folder;
  if (target.type === "album") return target.albumId === loc.albumId;
  return true;
}

function closeOffcanvas() {
  const pane = document.getElementById("mx-nav");
  const instance = bootstrap.Offcanvas.getInstance(pane);
  if (instance) instance.hide();
}

function go(target) {
  closeOffcanvas();
  hooks.navigate(target);
}

function acceptDrops(row, onDrop) {
  row.addEventListener("dragover", (event) => {
    if (!event.dataTransfer.types.includes(DRAG_TYPE)) return;
    event.preventDefault();
    row.classList.add("is-drop-target");
  });
  row.addEventListener("dragleave", () => row.classList.remove("is-drop-target"));
  row.addEventListener("drop", (event) => {
    row.classList.remove("is-drop-target");
    const raw = event.dataTransfer.getData(DRAG_TYPE);
    if (!raw) return;
    event.preventDefault();
    const ids = new Set(JSON.parse(raw));
    onDrop(state.items.filter((item) => ids.has(item.id)));
  });
}

function navRow({ label, iconName, target, depth = 0, caret = null, count = null, onContext = null }) {
  const row = el("div", {
    className: `mx-tree-row${isActive(target) ? " is-active" : ""}`,
    attrs: { role: "treeitem", "aria-selected": isActive(target) ? "true" : "false" },
  });
  row.style.setProperty("--depth", String(depth));
  if (caret) row.append(caret);
  else row.append(el("span", { className: "mx-tree-caret-space" }));
  const link = el(
    "button",
    {
      className: "mx-tree-link",
      attrs: { type: "button", title: label },
      on: { click: () => go(target) },
    },
    icon(iconName),
    el("span", { className: "text-truncate", text: label }),
  );
  if (count !== null) link.append(el("span", { className: "mx-tree-count", text: String(count) }));
  row.append(link);
  if (onContext) {
    row.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      onContext(event);
    });
    onLongPress(row, onContext);
    link.addEventListener("keydown", (event) => {
      if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
        event.preventDefault();
        const rect = link.getBoundingClientRect();
        onContext({ clientX: rect.left + 16, clientY: rect.bottom });
      }
    });
  }
  return row;
}

function caretFor(libraryId, path, hasChildren) {
  const k = key(libraryId, path);
  const open = expanded.has(k);
  if (!hasChildren) return null;
  return el(
    "button",
    {
      className: "mx-tree-caret",
      attrs: { type: "button", "aria-label": open ? "Collapse" : "Expand", "aria-expanded": open ? "true" : "false" },
      on: {
        click: async () => {
          if (open) {
            expanded.delete(k);
          } else {
            expanded.add(k);
            if (!children.has(k)) await loadChildren(libraryId, path);
          }
          renderTree();
        },
      },
    },
    icon(open ? "chevronDown" : "chevronRight", { size: 14 }),
  );
}

function folderMenu(lib, folder) {
  return (event) => {
    const entries = [{ label: "Open", icon: "folder", action: () => go({ type: "library", libraryId: lib.id, folder: folder.path }) }];
    if (lib.writable && page.canWrite) {
      entries.push(
        { label: "New folder", icon: "folderPlus", action: () => createFolder(lib.id, folder.path) },
        { label: "Rename", icon: "edit", action: () => renameFolder(lib.id, folder) },
        { divider: true },
        { label: "Delete", icon: "trash", danger: true, action: () => deleteFolder(lib.id, folder) },
      );
    }
    showMenu(event.clientX, event.clientY, entries);
  };
}

function renderFolders(container, lib, path, depth) {
  const list = children.get(key(lib.id, path));
  if (!list) return;
  for (const folder of list) {
    const k = key(lib.id, folder.path);
    const known = children.get(k);
    // Unknown children: show a caret so the user can try to expand it.
    const hasChildren = known === undefined || known.length > 0;
    const row = navRow({
      label: folder.name,
      iconName: "folder",
      target: { type: "library", libraryId: lib.id, folder: folder.path },
      depth,
      caret: caretFor(lib.id, folder.path, hasChildren),
      onContext: folderMenu(lib, folder),
    });
    if (lib.writable && page.canWrite) acceptDrops(row, (items) => moveItemsTo(items, lib.id, folder.path));
    container.append(row);
    if (expanded.has(k)) renderFolders(container, lib, folder.path, depth + 1);
  }
}

function section(title, ...extra) {
  return el("div", { className: "mx-tree-heading" }, el("span", { text: title }), ...extra);
}

export function renderTree() {
  const tree = el("div", { className: "mx-tree", attrs: { role: "tree", "aria-label": "Locations" } });
  const videos = page.defaultKind === "video";
  tree.append(section("Quick access"));
  tree.append(
    navRow({ label: videos ? "All videos" : "All photos & videos", iconName: videos ? "video" : "image", target: { type: "all" } }),
    navRow({ label: "Favorites", iconName: "star", target: { type: "favorites" } }),
    navRow({ label: "Recently added", iconName: "clock", target: { type: "recent" } }),
  );

  tree.append(section("Libraries"));
  for (const lib of state.libraries.filter((entry) => entry.browsable)) {
    const counts = lib.counts.image + lib.counts.video;
    const row = navRow({
      label: libraryLabel(lib),
      iconName: "library",
      target: { type: "library", libraryId: lib.id, folder: "" },
      caret: caretFor(lib.id, "", true),
      count: counts,
      onContext: (event) => {
        const entries = [{ label: "Open", icon: "folder", action: () => go({ type: "library", libraryId: lib.id, folder: "" }) }];
        if (lib.writable && page.canWrite) entries.push({ label: "New folder", icon: "folderPlus", action: () => createFolder(lib.id, "") });
        showMenu(event.clientX, event.clientY, entries);
      },
    });
    if (lib.writable && page.canWrite) acceptDrops(row, (items) => moveItemsTo(items, lib.id, ""));
    tree.append(row);
    if (expanded.has(key(lib.id, ""))) renderFolders(tree, lib, "", 1);
  }

  const addAlbum = page.canWrite
    ? el(
        "button",
        {
          className: "btn btn-sm btn-link p-0 mx-tree-add",
          attrs: { type: "button", "aria-label": "New album", title: "New album" },
          on: { click: () => createAlbum() },
        },
        icon("plus"),
      )
    : null;
  tree.append(section("Albums", addAlbum));
  for (const entry of state.albums) {
    const row = navRow({
      label: entry.name,
      iconName: "album",
      target: { type: "album", albumId: entry.id },
      count: entry.item_count,
      onContext: page.canWrite
        ? (event) =>
            showMenu(event.clientX, event.clientY, [
              { label: "Open", icon: "album", action: () => go({ type: "album", albumId: entry.id }) },
              { label: "Rename", icon: "edit", action: () => renameAlbum(entry) },
              { divider: true },
              { label: "Delete album", icon: "trash", danger: true, action: () => deleteAlbum(entry) },
            ])
        : null,
    });
    if (page.canWrite) acceptDrops(row, (items) => addToAlbum(items, entry.id));
    tree.append(row);
  }
  if (!state.albums.length) tree.append(el("p", { className: "mx-tree-empty", text: "No albums yet." }));
  body.replaceChildren(tree);
}
