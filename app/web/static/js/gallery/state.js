// Gallery state: where the user is (location), how the list is filtered,
// sorted and shown, and what is selected. Location, filters and sort live
// in the URL hash so back/forward and reload work; the view is a stored
// per-browser preference.

import { storageGet, storageSet } from "./util.js";

const root = document.getElementById("gallery-root");

export const page = {
  canWrite: root.dataset.canWrite === "true",
  isAdmin: root.dataset.isAdmin === "true",
  canListRoots: root.dataset.canListRoots === "true",
  canListGroups: root.dataset.canListGroups === "true",
  defaultKind: root.dataset.defaultKind || "",
};

export const PAGE_SIZE = 120;
export const VIEWS = ["xl", "l", "m", "s", "list", "details"];
export const SORT_FIELDS = {
  taken: "Date taken",
  name: "Name",
  modified: "Date modified",
  size: "Size",
  type: "Type",
  duration: "Duration",
};
// How many days "Recent" reaches back (by the time an item was indexed).
export const RECENT_DAYS = 30;

const VIEW_KEY = "nas.gallery.view";

function emptyFilters() {
  return {
    q: "",
    kind: page.defaultKind,
    dateFrom: "",
    dateTo: "",
    camera: "",
    size: "",
    orientation: "",
    gps: "",
    recursive: false,
  };
}

export const state = {
  libraries: [],
  albums: [],
  // type: all | favorites | recent | library | album
  location: { type: "all", libraryId: null, folder: "", albumId: null },
  filters: emptyFilters(),
  sort: { field: "taken", dir: "desc" },
  view: VIEWS.includes(storageGet(VIEW_KEY, "l")) ? storageGet(VIEW_KEY, "l") : "l",
  folders: [],
  folderWritable: false,
  items: [],
  total: 0,
  loading: false,
  facets: null,
  selected: new Set(),
  anchor: -1,
  // Index of the item the viewer shows (into state.items).
  current: -1,
};

export function setView(view) {
  state.view = view;
  storageSet(VIEW_KEY, view);
}

export function resetFilters() {
  state.filters = emptyFilters();
}

// Size presets (bytes), as in a desktop file manager.
export const SIZE_PRESETS = {
  "": { label: "Any size" },
  tiny: { label: "Tiny (under 100 KB)", max: 100 * 1024 },
  small: { label: "Small (100 KB – 1 MB)", min: 100 * 1024, max: 1024 * 1024 },
  medium: { label: "Medium (1 – 16 MB)", min: 1024 * 1024, max: 16 * 1024 * 1024 },
  large: { label: "Large (16 – 128 MB)", min: 16 * 1024 * 1024, max: 128 * 1024 * 1024 },
  huge: { label: "Huge (over 128 MB)", min: 128 * 1024 * 1024 },
};

export function library(id) {
  return state.libraries.find((lib) => lib.id === id) || null;
}

export function album(id) {
  return state.albums.find((entry) => entry.id === id) || null;
}

export function ownLibrary() {
  return state.libraries.find((lib) => lib.is_own) || null;
}

export function locationIsWritable() {
  const loc = state.location;
  return loc.type === "library" && state.folderWritable;
}

export function inFolderMode() {
  // Folders are listed when browsing a folder without a search or flattening.
  return state.location.type === "library" && !state.filters.q && !state.filters.recursive;
}

export function apiSort() {
  const { field, dir } = state.sort;
  if (field === "taken") return `taken_${dir}`;
  if (field === "name") return dir === "asc" ? "name" : "name_desc";
  if (field === "type") return "type";
  return `${field}_${dir}`;
}

// Query parameters for /media/items and /media/facets for the current view.
export function queryParams({ offset = 0, forFacets = false } = {}) {
  const params = new URLSearchParams();
  const loc = state.location;
  const f = state.filters;
  if (loc.type === "library") {
    params.set("library_id", String(loc.libraryId));
    params.set("folder", loc.folder);
    if (f.recursive || f.q) params.set("recursive", "true");
  } else if (loc.type === "album") {
    params.set("album_id", String(loc.albumId));
  } else if (loc.type === "favorites") {
    params.set("favorites", "true");
  }
  if (f.kind) params.set("kind", f.kind);
  if (forFacets) return params;
  if (loc.type === "recent") {
    const since = new Date(Date.now() - RECENT_DAYS * 86400 * 1000);
    params.set("indexed_after", since.toISOString().slice(0, 19));
  }
  if (f.q) params.set("q", f.q);
  if (f.camera) params.set("camera", f.camera);
  if (f.orientation) params.set("orientation", f.orientation);
  if (f.gps) params.set("has_gps", f.gps === "yes" ? "true" : "false");
  if (f.dateFrom) params.set("date_from", `${f.dateFrom}T00:00:00`);
  if (f.dateTo) params.set("date_to", `${f.dateTo}T23:59:59`);
  const size = SIZE_PRESETS[f.size];
  if (size && size.min !== undefined) params.set("min_size", String(size.min));
  if (size && size.max !== undefined) params.set("max_size", String(size.max));
  params.set("sort", apiSort());
  params.set("limit", String(PAGE_SIZE));
  params.set("offset", String(offset));
  return params;
}

// ---------------------------------------------------------------------------
// URL hash
// ---------------------------------------------------------------------------

export function toHash() {
  const params = new URLSearchParams();
  const loc = state.location;
  if (loc.type === "library") {
    params.set("lib", String(loc.libraryId));
    if (loc.folder) params.set("path", loc.folder);
  } else if (loc.type === "album") {
    params.set("album", String(loc.albumId));
  } else if (loc.type !== "all") {
    params.set("view", loc.type);
  }
  const f = state.filters;
  if (f.q) params.set("q", f.q);
  if (f.kind !== page.defaultKind) params.set("kind", f.kind || "any");
  for (const key of ["dateFrom", "dateTo", "camera", "size", "orientation", "gps"]) {
    if (f[key]) params.set(key, f[key]);
  }
  if (f.recursive) params.set("all", "1");
  if (state.sort.field !== "taken" || state.sort.dir !== "desc") {
    params.set("sort", `${state.sort.field}:${state.sort.dir}`);
  }
  return params.toString();
}

export function fromHash(hash) {
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  const lib = Number(params.get("lib"));
  const albumId = Number(params.get("album"));
  const special = params.get("view");
  if (lib) {
    state.location = { type: "library", libraryId: lib, folder: params.get("path") || "", albumId: null };
  } else if (albumId) {
    state.location = { type: "album", libraryId: null, folder: "", albumId };
  } else if (special === "favorites" || special === "recent") {
    state.location = { type: special, libraryId: null, folder: "", albumId: null };
  } else {
    state.location = { type: "all", libraryId: null, folder: "", albumId: null };
  }
  const f = emptyFilters();
  f.q = params.get("q") || "";
  const kind = params.get("kind");
  if (kind) f.kind = kind === "any" ? "" : kind;
  for (const key of ["dateFrom", "dateTo", "camera", "size", "orientation", "gps"]) {
    f[key] = params.get(key) || "";
  }
  f.recursive = params.get("all") === "1";
  state.filters = f;
  const [field, dir] = (params.get("sort") || "taken:desc").split(":");
  state.sort = {
    field: field in SORT_FIELDS ? field : "taken",
    dir: dir === "asc" ? "asc" : "desc",
  };
}

// Cross-module callbacks, filled in by gallery.js (keeps the modules free of
// import cycles): navigate(location), reload(), reloadAlbums(), rerender(),
// refreshTree(libraryId, folder), openViewer(index).
export const hooks = {};
