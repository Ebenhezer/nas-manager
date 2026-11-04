// Operations on items, folders and albums, shared by the browser pane, the
// navigation tree, the context menu and the viewer.

import { apiFetch } from "../api.js";
import { confirmDialog, pickFolder, promptDialog } from "./dialogs.js";
import { album, hooks, library, page, state } from "./state.js";
import { API, libraryLabel, plural, showError, showInfo } from "./util.js";

// Run an API call; null means it failed (and the error is shown). A call
// that answers with no body (204) yields true.
async function attempt(work) {
  try {
    return (await work()) ?? true;
  } catch (error) {
    showError(error.message);
    return null;
  }
}

// Whether the caller may change (move, rename, delete) this item's file.
export function itemWritable(item) {
  const lib = library(item.library_id);
  return Boolean(page.canWrite && lib && lib.writable);
}

// ---------------------------------------------------------------------------
// Items
// ---------------------------------------------------------------------------

export async function setFavorite(items, favorite) {
  if (!items.length) return;
  const result = await attempt(() =>
    apiFetch(`${API}/favorites`, { method: "POST", body: { item_ids: items.map((i) => i.id), favorite } }),
  );
  if (result === null) return;
  for (const item of items) item.is_favorite = favorite;
  if (state.location.type === "favorites" && !favorite) {
    await hooks.reload();
  } else {
    hooks.rerender();
  }
}

export async function addToAlbum(items, albumId) {
  const result = await attempt(() =>
    apiFetch(`${API}/albums/${albumId}/items`, { method: "POST", body: { item_ids: items.map((i) => i.id) } }),
  );
  if (result === null) return;
  const target = album(albumId);
  showInfo(
    result.changed
      ? `Added ${plural(result.changed, "item")} to ${target ? target.name : "the album"}.`
      : "Already in that album.",
  );
  await hooks.reloadAlbums();
}

export async function addToNewAlbum(items) {
  const created = await createAlbum();
  if (created) await addToAlbum(items, created.id);
}

export async function removeFromAlbum(items, albumId) {
  const result = await attempt(() =>
    apiFetch(`${API}/albums/${albumId}/items`, { method: "DELETE", body: { item_ids: items.map((i) => i.id) } }),
  );
  if (result === null) return;
  showInfo(`Removed ${plural(result.changed, "item")} from the album.`);
  await Promise.all([hooks.reloadAlbums(), hooks.reload()]);
}

export async function setAlbumCover(albumId, item) {
  const result = await attempt(() =>
    apiFetch(`${API}/albums/${albumId}`, { method: "PATCH", body: { cover_item_id: item.id } }),
  );
  if (result !== null) {
    showInfo("Album cover updated.");
    await hooks.reloadAlbums();
  }
}

export async function deleteItems(items) {
  const writable = items.filter(itemWritable);
  if (!writable.length) return;
  const message =
    writable.length === 1
      ? `Permanently delete "${writable[0].name}"? The file is removed from the NAS.`
      : `Permanently delete ${writable.length} items? The files are removed from the NAS.`;
  if (!(await confirmDialog({ title: "Delete", message, okLabel: "Delete", danger: true }))) return false;
  const result = await attempt(() =>
    apiFetch(`${API}/items/delete`, { method: "POST", body: { item_ids: writable.map((i) => i.id) } }),
  );
  if (result === null) return false;
  if (result.failed.length) showError(result.failed.map((f) => `#${f.item_id}: ${f.message}`).join("; "));
  if (result.changed) showInfo(`Deleted ${plural(result.changed, "item")}.`);
  state.selected.clear();
  await Promise.all([hooks.reload(), hooks.reloadAlbums(), hooks.reloadLibraries()]);
  return true;
}

export async function moveItems(items) {
  const writable = items.filter(itemWritable);
  if (!writable.length) return;
  const libraryIds = new Set(writable.map((i) => i.library_id));
  if (libraryIds.size !== 1) {
    showError("Items can only be moved within one library at a time.");
    return;
  }
  const lib = library(writable[0].library_id);
  const start = state.location.type === "library" && state.location.libraryId === lib.id ? state.location.folder : "";
  const folder = await pickFolder({
    title: `Move ${plural(writable.length, "item")} to…`,
    libraryId: lib.id,
    libraryName: libraryLabel(lib),
    start,
  });
  if (folder === null) return;
  await moveItemsTo(writable, lib.id, folder);
}

export async function moveItemsTo(items, libraryId, folder) {
  const result = await attempt(() =>
    apiFetch(`${API}/items/move`, {
      method: "POST",
      body: { item_ids: items.map((i) => i.id), library_id: libraryId, folder },
    }),
  );
  if (result === null) return;
  showInfo(result.changed ? `Moved ${plural(result.changed, "item")} to ${folder || "the top folder"}.` : "Nothing to move.");
  state.selected.clear();
  await Promise.all([hooks.reload(), hooks.refreshTree(libraryId)]);
}

export async function renameItem(item) {
  if (!itemWritable(item)) return;
  const name = await promptDialog({ title: "Rename", label: "New name", value: item.name, okLabel: "Rename", selectStem: true });
  if (!name || name === item.name) return;
  const updated = await attempt(() => apiFetch(`${API}/items/${item.id}`, { method: "PATCH", body: { name } }));
  if (updated === null) return;
  Object.assign(item, updated);
  hooks.rerender();
}

export function downloadUrl(items) {
  if (items.length === 1) return `${API}/items/${items[0].id}/original?download=true`;
  return `${API}/items/zip?ids=${items.map((i) => i.id).join(",")}`;
}

export function downloadItems(items) {
  if (!items.length) return;
  if (items.length > 1000) {
    showError("Select at most 1000 items to download at once.");
    return;
  }
  const link = document.createElement("a");
  link.href = downloadUrl(items);
  link.rel = "noopener";
  document.body.append(link);
  link.click();
  link.remove();
  if (items.length > 1) showInfo(`Preparing a ZIP of ${plural(items.length, "item")}…`);
}

// ---------------------------------------------------------------------------
// Folders (own personal library)
// ---------------------------------------------------------------------------

export async function createFolder(libraryId, parent) {
  const name = await promptDialog({ title: "New folder", label: "Folder name", value: "New folder", okLabel: "Create" });
  if (!name) return null;
  const path = parent ? `${parent}/${name}` : name;
  const created = await attempt(() =>
    apiFetch(`${API}/libraries/${libraryId}/folders`, { method: "POST", body: { path } }),
  );
  if (created === null) return null;
  await Promise.all([hooks.reload(), hooks.refreshTree(libraryId, parent)]);
  return created;
}

export async function renameFolder(libraryId, folder) {
  const name = await promptDialog({ title: "Rename folder", label: "New name", value: folder.name, okLabel: "Rename" });
  if (!name || name === folder.name) return;
  const renamed = await attempt(() =>
    apiFetch(`${API}/libraries/${libraryId}/folders`, { method: "PATCH", body: { path: folder.path, new_name: name } }),
  );
  if (renamed === null) return;
  const loc = state.location;
  // Follow the rename when browsing inside the renamed folder.
  if (loc.type === "library" && loc.libraryId === libraryId && (loc.folder === folder.path || loc.folder.startsWith(`${folder.path}/`))) {
    hooks.navigate({ ...loc, folder: renamed.path + loc.folder.slice(folder.path.length) });
  } else {
    await hooks.reload();
  }
  await hooks.refreshTree(libraryId);
}

export async function deleteFolder(libraryId, folder) {
  const ok = await confirmDialog({
    title: "Delete folder",
    message: `Delete the empty folder "${folder.name}"?`,
    okLabel: "Delete",
    danger: true,
  });
  if (!ok) return;
  const done = await attempt(() =>
    apiFetch(`${API}/libraries/${libraryId}/folders?${new URLSearchParams({ path: folder.path })}`, { method: "DELETE" }),
  );
  if (done === null) return;
  const loc = state.location;
  if (loc.type === "library" && loc.libraryId === libraryId && loc.folder === folder.path) {
    hooks.navigate({ ...loc, folder: folder.path.includes("/") ? folder.path.slice(0, folder.path.lastIndexOf("/")) : "" });
  } else {
    await hooks.reload();
  }
  await hooks.refreshTree(libraryId);
}

// ---------------------------------------------------------------------------
// Albums
// ---------------------------------------------------------------------------

export async function createAlbum() {
  const name = await promptDialog({ title: "New album", label: "Album name", okLabel: "Create", maxLength: 128 });
  if (!name) return null;
  const created = await attempt(() => apiFetch(`${API}/albums`, { method: "POST", body: { name } }));
  if (created !== null) await hooks.reloadAlbums();
  return created;
}

export async function renameAlbum(entry) {
  const name = await promptDialog({ title: "Rename album", label: "Album name", value: entry.name, okLabel: "Rename", maxLength: 128 });
  if (!name || name === entry.name) return;
  const updated = await attempt(() => apiFetch(`${API}/albums/${entry.id}`, { method: "PATCH", body: { name } }));
  if (updated !== null) await hooks.reloadAlbums();
}

export async function deleteAlbum(entry) {
  const ok = await confirmDialog({
    title: "Delete album",
    message: `Delete the album "${entry.name}"? The photos and videos themselves are kept.`,
    okLabel: "Delete album",
    danger: true,
  });
  if (!ok) return;
  const done = await attempt(() => apiFetch(`${API}/albums/${entry.id}`, { method: "DELETE" }));
  if (done === null) return;
  await hooks.reloadAlbums();
  if (state.location.type === "album" && state.location.albumId === entry.id) hooks.navigate({ type: "all" });
}
