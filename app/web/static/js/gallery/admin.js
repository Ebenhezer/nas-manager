// Library administration (media:admin), in the "Libraries" dialog.

import { apiFetch } from "../api.js";
import { formatBytes } from "../format.js";
import { confirmDialog } from "./dialogs.js";
import { hooks, page, state } from "./state.js";
import { API, el, formatDate, libraryLabel, option, plural, showError, showInfo } from "./util.js";

function cell(text) {
  return el("td", { text });
}

function actionButton(label, kind, handler) {
  return el("button", {
    className: `btn btn-sm btn-outline-${kind} ms-1`,
    text: label,
    attrs: { type: "button" },
    on: { click: handler },
  });
}

export function renderLibraryTable() {
  if (!page.isAdmin) return;
  const body = document.getElementById("libraries-body");
  body.replaceChildren();
  for (const library of state.libraries) {
    const folder = library.kind === "shared" ? `${library.root}/${library.path}` : library.path;
    const counts = `${library.counts.image} photos · ${library.counts.video} videos`;
    let scan = library.scan_state === "idle" ? formatDate(library.last_scan_at) : library.scan_state;
    if (library.last_scan_status === "failed") scan += ` (failed: ${library.last_scan_error})`;
    const actions = el("td", { className: "text-end text-nowrap" });
    actions.append(
      actionButton("Rescan", "secondary", () => rescan(library.id)),
      actionButton(library.enabled ? "Disable" : "Enable", "secondary", () =>
        patchLibrary(library.id, { enabled: !library.enabled }),
      ),
    );
    if (library.counts.video) {
      actions.append(actionButton("Prepare videos", "secondary", () => transcodeLibrary(library.id)));
    }
    if (library.kind === "shared") {
      actions.append(actionButton("Remove", "danger", () => removeLibrary(library)));
    }
    body.append(
      el(
        "tr",
        {},
        cell(libraryLabel(library) + (library.enabled ? "" : " (disabled)")),
        cell(library.kind),
        cell(folder),
        cell(counts),
        cell(scan),
        actions,
      ),
    );
  }
}

async function patchLibrary(id, body) {
  try {
    await apiFetch(`${API}/libraries/${id}`, { method: "PATCH", body });
    await hooks.reloadLibraries();
  } catch (error) {
    showError(error.message);
  }
}

async function transcodeLibrary(id) {
  try {
    const result = await apiFetch(`${API}/libraries/${id}/transcode`, { method: "POST" });
    showInfo(`${plural(result.queued, "video")} queued for transcoding.`);
  } catch (error) {
    showError(error.message);
  }
}

async function removeLibrary(library) {
  const ok = await confirmDialog({
    title: "Remove library",
    message: `Remove the library "${library.name}"? Its index and album entries are dropped; the files on disk are not touched.`,
    okLabel: "Remove",
    danger: true,
  });
  if (!ok) return;
  try {
    await apiFetch(`${API}/libraries/${library.id}`, { method: "DELETE" });
    if (state.location.type === "library" && state.location.libraryId === library.id) hooks.navigate({ type: "all" });
    await Promise.all([hooks.reloadLibraries(), hooks.reload()]);
  } catch (error) {
    showError(error.message);
  }
}

export async function rescan(id) {
  try {
    await apiFetch(`${API}/libraries/${id}/scan`, { method: "POST" });
    showInfo("Rescan queued; the worker will pick it up shortly.");
    await hooks.reloadLibraries();
  } catch (error) {
    showError(error.message);
  }
}

async function loadStatus() {
  const status = await apiFetch(`${API}/status`);
  const transcoding = status.transcoding_available
    ? `${status.queued_transcodes + status.running_transcodes} transcodes pending · ${formatBytes(status.transcode_cache_bytes)} cached`
    : "transcoding unavailable";
  document.getElementById("media-status").textContent =
    `${status.items} items · ${status.pending_thumbnails} thumbnails pending · ${transcoding} · worker ${status.worker_online ? "online" : "offline"}`;
}

export async function wireAdmin() {
  if (!page.isAdmin) return;
  const rootSelect = document.getElementById("library-root");
  const groupSelect = document.getElementById("library-groups");
  document.getElementById("mx-manage-modal").addEventListener("show.bs.modal", async () => {
    try {
      await Promise.all([loadStatus(), hooks.reloadLibraries()]);
    } catch (error) {
      showError(error.message);
    }
  });
  try {
    if (page.canListRoots) {
      const roots = await apiFetch("/api/v1/files/roots");
      rootSelect.replaceChildren(...roots.map((value) => option(value, value)));
    }
    if (page.canListGroups) {
      const groups = await apiFetch("/api/v1/groups");
      groupSelect.replaceChildren(...groups.map((group) => option(group.id, group.name)));
    }
  } catch (error) {
    showError(error.message);
  }
  document.getElementById("library-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {
      name: document.getElementById("library-name").value.trim(),
      root: rootSelect.value,
      path: document.getElementById("library-path").value.trim(),
      include_videos: document.getElementById("library-videos").checked,
      group_ids: Array.from(groupSelect.selectedOptions).map((opt) => Number(opt.value)),
    };
    try {
      await apiFetch(`${API}/libraries`, { method: "POST", body });
      event.target.reset();
      showInfo("Library added; the worker will index it shortly.");
      await hooks.reloadLibraries();
    } catch (error) {
      showError(error.message);
    }
  });
}
