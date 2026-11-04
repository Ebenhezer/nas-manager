// Explorer-style gallery (Phase 15): wires the modules under ./gallery/.
// Locations, filters and sort are kept in the URL hash; the browser's
// back/forward buttons move between them.

import { apiFetch } from "./api.js";
import { renderLibraryTable, wireAdmin } from "./gallery/admin.js";
import { clearSelection, reload, rerender, wireBrowser } from "./gallery/browser.js";
import { fromHash, hooks, state, toHash } from "./gallery/state.js";
import { refreshTree, renderTree, revealLocation } from "./gallery/tree.js";
import { wireUploads } from "./gallery/uploads.js";
import { API, hydrateIcons, showError } from "./gallery/util.js";
import { openViewer, wireViewer } from "./gallery/viewer.js";

async function reloadLibraries() {
  state.libraries = await apiFetch(`${API}/libraries`);
  renderLibraryTable();
  renderTree();
}

async function reloadAlbums() {
  state.albums = await apiFetch(`${API}/albums`);
  renderTree();
}

function writeHash({ replace = false } = {}) {
  const hash = toHash();
  const url = `${window.location.pathname}${window.location.search}${hash ? `#${hash}` : ""}`;
  if (`${window.location.pathname}${window.location.search}${window.location.hash}` === url) return;
  if (replace) window.history.replaceState(null, "", url);
  else window.history.pushState(null, "", url);
}

async function show() {
  clearSelection();
  document.getElementById("mx-content").scrollTop = 0;
  await revealLocation();
  renderTree();
  await reload();
}

function navigate(target) {
  state.location = {
    type: target.type,
    libraryId: target.libraryId ?? null,
    folder: target.folder ?? "",
    albumId: target.albumId ?? null,
  };
  // A search belongs to the place it was made in.
  state.filters.q = "";
  writeHash();
  show();
}

function filtersChanged({ replace = false } = {}) {
  writeHash({ replace });
  clearSelection();
  reload();
}

window.addEventListener("popstate", (event) => {
  if (event.state && event.state.mxViewer) return;
  // Closing the viewer through Back lands on the same hash: nothing to reload.
  if (window.location.hash.replace(/^#/, "") === toHash()) return;
  fromHash(window.location.hash);
  show();
});

Object.assign(hooks, {
  navigate,
  filtersChanged,
  reload,
  rerender,
  reloadAlbums: () => reloadAlbums().catch((error) => showError(error.message)),
  reloadLibraries: () => reloadLibraries().catch((error) => showError(error.message)),
  refreshTree,
  openViewer,
});

async function init() {
  hydrateIcons();
  fromHash(window.location.hash);
  wireBrowser();
  wireUploads();
  wireViewer();
  try {
    await Promise.all([reloadLibraries(), reloadAlbums()]);
  } catch (error) {
    showError(error.message);
  }
  const loc = state.location;
  if (loc.type === "library" && !state.libraries.some((lib) => lib.id === loc.libraryId && lib.browsable)) {
    state.location = { type: "all", libraryId: null, folder: "", albumId: null };
    writeHash({ replace: true });
  }
  await show();
  await wireAdmin();
}

init();
