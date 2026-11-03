import { apiFetch, getCsrfToken } from "./api.js";
import { formatBytes } from "./format.js";

const API = "/api/v1/media";
const PAGE_SIZE = 120;

const root = document.getElementById("gallery-root");
const canWrite = root.dataset.canWrite === "true";
const isAdmin = root.dataset.isAdmin === "true";
const banner = document.getElementById("error-banner");
const infoBanner = document.getElementById("info-banner");
const grid = document.getElementById("gallery-grid");
const resultCount = document.getElementById("result-count");
const loadMore = document.getElementById("load-more");
const librarySelect = document.getElementById("library-select");
const albumSelect = document.getElementById("album-select");
const kindSelect = document.getElementById("kind-select");
const rescanButton = document.getElementById("rescan-button");

const state = {
  libraries: [],
  albums: [],
  items: [],
  total: 0,
  offset: 0,
  current: -1,
};

function showError(message) {
  banner.textContent = message;
  banner.classList.remove("d-none");
}

function hideError() {
  banner.classList.add("d-none");
}

function showInfo(message) {
  infoBanner.textContent = message;
  infoBanner.classList.remove("d-none");
  window.setTimeout(() => infoBanner.classList.add("d-none"), 6000);
}

function option(value, label) {
  const element = document.createElement("option");
  element.value = String(value);
  element.textContent = label;
  return element;
}

function libraryLabel(library) {
  if (library.is_own) return "My library";
  if (library.kind === "personal") return `${library.name.slice(1)} (personal)`;
  return library.name;
}

function formatDate(value) {
  if (!value) return "–";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatSeconds(value) {
  const total = Math.round(Number(value) || 0);
  const minutes = Math.floor(total / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

// ---------------------------------------------------------------------------
// Libraries, albums and filters
// ---------------------------------------------------------------------------

function selectedLibrary() {
  const id = Number(librarySelect.value);
  return state.libraries.find((library) => library.id === id) || null;
}

function ownLibrary() {
  return state.libraries.find((library) => library.is_own) || null;
}

function updateRescanButton() {
  const library = selectedLibrary() || ownLibrary();
  const allowed = library && (isAdmin || (library.is_own && canWrite));
  rescanButton.disabled = !allowed;
  rescanButton.dataset.libraryId = library ? String(library.id) : "";
}

async function loadLibraries() {
  state.libraries = await apiFetch(`${API}/libraries`);
  const keep = librarySelect.value;
  librarySelect.replaceChildren(option("", "All libraries"));
  for (const library of state.libraries.filter((lib) => lib.browsable)) {
    librarySelect.append(option(library.id, libraryLabel(library)));
  }
  librarySelect.value = keep;
  updateRescanButton();
  if (isAdmin) renderLibraryTable();
}

async function loadAlbums() {
  state.albums = await apiFetch(`${API}/albums`);
  const keep = albumSelect.value;
  albumSelect.replaceChildren(option("", "Any"));
  const viewerAlbum = document.getElementById("viewer-album");
  viewerAlbum.replaceChildren();
  const list = document.getElementById("albums-list");
  list.replaceChildren();
  for (const album of state.albums) {
    albumSelect.append(option(album.id, `${album.name} (${album.item_count})`));
    viewerAlbum.append(option(album.id, album.name));
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-sm btn-outline-secondary";
    button.textContent = `${album.name} · ${album.item_count}`;
    button.addEventListener("click", () => {
      albumSelect.value = String(album.id);
      search(true);
    });
    item.append(button);
    if (canWrite) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn btn-sm btn-link text-danger";
      remove.textContent = "Delete";
      remove.setAttribute("aria-label", `Delete album ${album.name}`);
      remove.addEventListener("click", () => deleteAlbum(album));
      item.append(remove);
    }
    list.append(item);
  }
  if (!state.albums.length) {
    const empty = document.createElement("li");
    empty.className = "text-body-secondary small";
    empty.textContent = "No albums yet.";
    list.append(empty);
  }
  albumSelect.value = state.albums.some((a) => String(a.id) === keep) ? keep : "";
  const disabled = !canWrite || !state.albums.length;
  viewerAlbum.disabled = disabled;
  document.getElementById("viewer-add-album").disabled = disabled;
}

async function deleteAlbum(album) {
  if (!window.confirm(`Delete the album "${album.name}"? The photos themselves are kept.`)) return;
  try {
    await apiFetch(`${API}/albums/${album.id}`, { method: "DELETE" });
    if (albumSelect.value === String(album.id)) albumSelect.value = "";
    await loadAlbums();
    await search(true);
  } catch (error) {
    showError(error.message);
  }
}

function queryString(offset) {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
  const fields = {
    library_id: librarySelect.value,
    q: document.getElementById("search-q").value.trim(),
    kind: kindSelect.value,
    album_id: albumSelect.value,
    sort: document.getElementById("sort-select").value,
  };
  for (const [key, value] of Object.entries(fields)) {
    if (value) params.set(key, value);
  }
  const from = document.getElementById("date-from").value;
  const to = document.getElementById("date-to").value;
  if (from) params.set("date_from", `${from}T00:00:00`);
  if (to) params.set("date_to", `${to}T23:59:59`);
  return params.toString();
}

// ---------------------------------------------------------------------------
// Grid
// ---------------------------------------------------------------------------

function tile(item, index) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "gallery-tile";
  button.title = item.name;
  button.setAttribute("aria-label", `Open ${item.name}`);
  if (item.thumb_state === "failed" || item.thumb_state === "unsupported") {
    const label = document.createElement("span");
    label.className = "gallery-tile-name";
    label.textContent = item.name;
    button.append(label);
  } else {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.decoding = "async";
    img.alt = item.name;
    img.src = `${API}/items/${item.id}/thumbnail?size=small`;
    img.addEventListener("error", () => {
      const label = document.createElement("span");
      label.className = "gallery-tile-name";
      label.textContent = item.name;
      img.replaceWith(label);
    });
    button.append(img);
  }
  if (item.kind === "video") {
    const badge = document.createElement("span");
    badge.className = "badge text-bg-dark gallery-badge";
    badge.textContent = item.duration_seconds ? `▶ ${formatSeconds(item.duration_seconds)}` : "▶";
    button.append(badge);
  }
  button.addEventListener("click", () => openViewer(index));
  return button;
}

function renderGrid(reset) {
  if (reset) grid.replaceChildren();
  const start = grid.children.length;
  for (let index = start; index < state.items.length; index += 1) {
    grid.append(tile(state.items[index], index));
  }
  resultCount.textContent = state.total
    ? `${state.items.length} of ${state.total} item${state.total === 1 ? "" : "s"}`
    : "Nothing here yet.";
  loadMore.classList.toggle("d-none", state.items.length >= state.total);
}

async function search(reset) {
  hideError();
  const offset = reset ? 0 : state.items.length;
  try {
    const page = await apiFetch(`${API}/items?${queryString(offset)}`);
    state.items = reset ? page.items : state.items.concat(page.items);
    state.total = page.total;
    renderGrid(reset);
  } catch (error) {
    showError(error.message);
  }
}

// ---------------------------------------------------------------------------
// Viewer
// ---------------------------------------------------------------------------

function metaRow(list, label, value) {
  if (value === null || value === undefined || value === "") return;
  const dt = document.createElement("dt");
  dt.className = "col-5 text-body-secondary";
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.className = "col-7 text-break";
  dd.textContent = value;
  list.append(dt, dd);
}

function renderMeta(item) {
  const list = document.getElementById("viewer-meta");
  list.replaceChildren();
  const library = state.libraries.find((lib) => lib.id === item.library_id);
  metaRow(list, "Library", library ? libraryLabel(library) : String(item.library_id));
  metaRow(list, "Path", item.rel_path);
  metaRow(list, "Taken", formatDate(item.taken_at));
  metaRow(list, "Size", formatBytes(item.size));
  if (item.width && item.height) metaRow(list, "Dimensions", `${item.width} × ${item.height}`);
  const camera = [item.camera_make, item.camera_model].filter(Boolean).join(" ");
  metaRow(list, "Camera", camera);
  if (item.gps_lat !== null && item.gps_lon !== null) {
    metaRow(list, "Location", `${item.gps_lat.toFixed(5)}, ${item.gps_lon.toFixed(5)}`);
  }
  if (item.kind === "video") {
    if (item.duration_seconds) metaRow(list, "Duration", formatSeconds(item.duration_seconds));
    const playback = {
      direct: "Plays directly",
      transcode: "Needs a transcode",
      unsupported: "Not playable",
    };
    metaRow(list, "Playback", playback[item.playback] || "Not analysed");
    const codecs = [item.video_codec, item.audio_codec].filter(Boolean).join(" / ");
    metaRow(list, "Codecs", codecs);
    metaRow(list, "Container", item.container);
  }
  metaRow(list, "Type", item.mime);
}

function showVideoMessage(message, canTranscode = false) {
  document.getElementById("viewer-video-status").classList.remove("d-none");
  document.getElementById("viewer-video-message").textContent = message;
  document.getElementById("viewer-transcode").classList.toggle("d-none", !canTranscode);
}

let transcodeTimer = null;

function stopTranscodePolling() {
  window.clearTimeout(transcodeTimer);
  transcodeTimer = null;
}

function playVideo(item) {
  const video = document.getElementById("viewer-video");
  if (item.thumb_state === "ready") {
    video.poster = `${API}/items/${item.id}/thumbnail?size=large`;
  } else {
    video.removeAttribute("poster");
  }
  video.src = `${API}/items/${item.id}/stream`;
  video.classList.remove("d-none");
}

function describeTranscode(status) {
  if (status.state === "running") return `Preparing for playback… ${Math.round(status.progress_percent)}%`;
  if (status.state === "queued") return "Waiting for the worker to prepare this video…";
  if (status.state === "failed") return `Preparing this video failed: ${status.error || "unknown error"}`;
  return "This video's format cannot be played in the browser directly.";
}

async function pollTranscode(item) {
  stopTranscodePolling();
  if (state.items[state.current] !== item) return;
  try {
    const status = await apiFetch(`${API}/items/${item.id}/transcode`);
    if (status.state === "ready") {
      document.getElementById("viewer-video-status").classList.add("d-none");
      playVideo(item);
      return;
    }
    const idle = status.state === null || status.state === "failed";
    if (!status.transcoding_available) {
      showVideoMessage("This video's format cannot be played in the browser, and transcoding is not available on this server. Use Download instead.");
      return;
    }
    showVideoMessage(describeTranscode(status), idle);
    if (!idle) transcodeTimer = window.setTimeout(() => pollTranscode(item), 3000);
  } catch (error) {
    showError(error.message);
  }
}

async function requestTranscode() {
  const item = state.items[state.current];
  if (!item) return;
  try {
    await apiFetch(`${API}/items/${item.id}/transcode`, { method: "POST" });
    await pollTranscode(item);
  } catch (error) {
    showError(error.message);
  }
}

function renderMedia(item) {
  const image = document.getElementById("viewer-image");
  const video = document.getElementById("viewer-video");
  stopTranscodePolling();
  document.getElementById("viewer-video-status").classList.add("d-none");
  document.getElementById("viewer-transcode").classList.add("d-none");
  video.pause();
  video.removeAttribute("src");
  video.load();
  video.classList.add("d-none");
  image.classList.add("d-none");
  if (item.kind === "image") {
    image.alt = item.name;
    image.src =
      item.thumb_state === "ready" || item.thumb_state === "pending"
        ? `${API}/items/${item.id}/thumbnail?size=large`
        : `${API}/items/${item.id}/original`;
    image.classList.remove("d-none");
  } else if (item.playback === "direct") {
    playVideo(item);
  } else if (item.playback === "transcode") {
    pollTranscode(item);
  } else if (item.playback === "unsupported") {
    showVideoMessage("This file has no playable video stream. Use Download instead.");
  } else {
    showVideoMessage("This video has not been analysed yet (ffmpeg is needed). Use Download instead.");
  }
}

function openViewer(index) {
  state.current = index;
  const item = state.items[index];
  if (!item) return;
  document.getElementById("viewer-title").textContent = item.name;
  renderMedia(item);
  renderMeta(item);
  const download = document.getElementById("viewer-download");
  download.href = `${API}/items/${item.id}/original?download=true`;
  const library = state.libraries.find((lib) => lib.id === item.library_id);
  document
    .getElementById("viewer-delete")
    .classList.toggle("d-none", !(library && library.writable));
  document
    .getElementById("viewer-remove-album")
    .classList.toggle("d-none", !(canWrite && albumSelect.value));
  document.getElementById("viewer-prev").disabled = index <= 0;
  document.getElementById("viewer-next").disabled = index >= state.items.length - 1;
  bootstrap.Modal.getOrCreateInstance(document.getElementById("viewer-modal")).show();
}

async function step(delta) {
  const next = state.current + delta;
  if (next >= state.items.length && state.items.length < state.total) await search(false);
  if (next >= 0 && next < state.items.length) openViewer(next);
}

async function addToAlbum() {
  const item = state.items[state.current];
  const albumId = document.getElementById("viewer-album").value;
  if (!item || !albumId) return;
  try {
    const result = await apiFetch(`${API}/albums/${albumId}/items`, {
      method: "POST",
      body: { item_ids: [item.id] },
    });
    showInfo(result.changed ? "Added to the album." : "It was already in that album.");
    await loadAlbums();
  } catch (error) {
    showError(error.message);
  }
}

async function removeFromAlbum() {
  const item = state.items[state.current];
  const albumId = albumSelect.value;
  if (!item || !albumId) return;
  try {
    await apiFetch(`${API}/albums/${albumId}/items`, {
      method: "DELETE",
      body: { item_ids: [item.id] },
    });
    bootstrap.Modal.getOrCreateInstance(document.getElementById("viewer-modal")).hide();
    await loadAlbums();
    await search(true);
  } catch (error) {
    showError(error.message);
  }
}

async function deleteItem() {
  const item = state.items[state.current];
  if (!item) return;
  if (!window.confirm(`Permanently delete "${item.name}" from your library?`)) return;
  try {
    await apiFetch(`${API}/items/${item.id}`, { method: "DELETE" });
    bootstrap.Modal.getOrCreateInstance(document.getElementById("viewer-modal")).hide();
    await Promise.all([search(true), loadLibraries(), loadAlbums()]);
  } catch (error) {
    showError(error.message);
  }
}

// ---------------------------------------------------------------------------
// Uploads
// ---------------------------------------------------------------------------

function uploadOne(file) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file, file.name);
    if (file.lastModified) form.append("taken_at", new Date(file.lastModified).toISOString());
    const request = new XMLHttpRequest();
    request.open("POST", `${API}/upload`);
    request.setRequestHeader("X-CSRF-Token", getCsrfToken());
    request.addEventListener("load", () => {
      if (request.status >= 200 && request.status < 300) {
        resolve();
        return;
      }
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
  const status = document.getElementById("upload-status");
  const filesToSend = Array.from(fileList);
  const failures = [];
  for (let index = 0; index < filesToSend.length; index += 1) {
    status.textContent = `Uploading ${index + 1} of ${filesToSend.length}…`;
    try {
      await uploadOne(filesToSend[index]);
    } catch (error) {
      failures.push(error.message);
    }
  }
  status.textContent = "";
  if (failures.length) showError(failures.join("; "));
  const sent = filesToSend.length - failures.length;
  if (sent) showInfo(`Uploaded ${sent} file${sent === 1 ? "" : "s"}.`);
  await Promise.all([loadLibraries(), search(true)]);
}

function wireUploads() {
  if (!canWrite) return;
  const input = document.getElementById("upload-input");
  const zone = document.getElementById("drop-zone");
  document.getElementById("upload-button").addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    if (input.files && input.files.length) uploadFiles(input.files);
    input.value = "";
  });
  zone.addEventListener("dragover", (event) => {
    event.preventDefault();
    zone.classList.add("is-dragging");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("is-dragging"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("is-dragging");
    if (event.dataTransfer && event.dataTransfer.files.length) uploadFiles(event.dataTransfer.files);
  });
}

// ---------------------------------------------------------------------------
// Administration (media:admin)
// ---------------------------------------------------------------------------

function cell(text) {
  const td = document.createElement("td");
  td.textContent = text;
  return td;
}

function actionButton(label, kind, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `btn btn-sm btn-outline-${kind} ms-1`;
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

function renderLibraryTable() {
  const body = document.getElementById("libraries-body");
  body.replaceChildren();
  for (const library of state.libraries) {
    const row = document.createElement("tr");
    const folder = library.kind === "shared" ? `${library.root}/${library.path}` : library.path;
    const counts = `${library.counts.image} photos · ${library.counts.video} videos`;
    let scan = library.scan_state === "idle" ? formatDate(library.last_scan_at) : library.scan_state;
    if (library.last_scan_status === "failed") scan += ` (failed: ${library.last_scan_error})`;
    row.append(
      cell(libraryLabel(library) + (library.enabled ? "" : " (disabled)")),
      cell(library.kind),
      cell(folder),
      cell(counts),
      cell(scan),
    );
    const actions = document.createElement("td");
    actions.className = "text-end text-nowrap";
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
    row.append(actions);
    body.append(row);
  }
}

async function patchLibrary(id, body) {
  try {
    await apiFetch(`${API}/libraries/${id}`, { method: "PATCH", body });
    await loadLibraries();
  } catch (error) {
    showError(error.message);
  }
}

async function transcodeLibrary(id) {
  try {
    const result = await apiFetch(`${API}/libraries/${id}/transcode`, { method: "POST" });
    showInfo(`${result.queued} video${result.queued === 1 ? "" : "s"} queued for transcoding.`);
  } catch (error) {
    showError(error.message);
  }
}

async function removeLibrary(library) {
  const message = `Remove the library "${library.name}"? Its index and album entries are dropped; the files on disk are not touched.`;
  if (!window.confirm(message)) return;
  try {
    await apiFetch(`${API}/libraries/${library.id}`, { method: "DELETE" });
    await Promise.all([loadLibraries(), search(true)]);
  } catch (error) {
    showError(error.message);
  }
}

async function loadAdminData() {
  if (!isAdmin) return;
  const rootSelect = document.getElementById("library-root");
  const groupSelect = document.getElementById("library-groups");
  try {
    const status = await apiFetch(`${API}/status`);
    const transcoding = status.transcoding_available
      ? `${status.queued_transcodes + status.running_transcodes} transcodes pending · ${formatBytes(status.transcode_cache_bytes)} cached`
      : "transcoding unavailable";
    document.getElementById("media-status").textContent =
      `${status.items} items · ${status.pending_thumbnails} thumbnails pending · ${transcoding} · worker ${status.worker_online ? "online" : "offline"}`;
    if (root.dataset.canListRoots === "true") {
      const roots = await apiFetch("/api/v1/files/roots");
      rootSelect.replaceChildren(...roots.map((value) => option(value, value)));
    }
    if (root.dataset.canListGroups === "true") {
      const groups = await apiFetch("/api/v1/groups");
      groupSelect.replaceChildren(...groups.map((group) => option(group.id, group.name)));
    }
  } catch (error) {
    showError(error.message);
  }
  document.getElementById("library-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    hideError();
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
      await loadLibraries();
    } catch (error) {
      showError(error.message);
    }
  });
}

async function rescan(id) {
  try {
    await apiFetch(`${API}/libraries/${id}/scan`, { method: "POST" });
    showInfo("Rescan queued; the worker will pick it up shortly.");
    await loadLibraries();
  } catch (error) {
    showError(error.message);
  }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

let searchTimer = null;

function wireFilters() {
  const form = document.getElementById("filter-form");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    search(true);
  });
  form.addEventListener("change", () => {
    updateRescanButton();
    search(true);
  });
  document.getElementById("search-q").addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => search(true), 300);
  });
  loadMore.addEventListener("click", () => search(false));
  rescanButton.addEventListener("click", () => {
    if (rescanButton.dataset.libraryId) rescan(Number(rescanButton.dataset.libraryId));
  });
  document.getElementById("viewer-prev").addEventListener("click", () => step(-1));
  document.getElementById("viewer-next").addEventListener("click", () => step(1));
  document.getElementById("viewer-add-album").addEventListener("click", addToAlbum);
  document.getElementById("viewer-remove-album").addEventListener("click", removeFromAlbum);
  document.getElementById("viewer-delete").addEventListener("click", deleteItem);
  document.getElementById("viewer-modal").addEventListener("hidden.bs.modal", () => {
    stopTranscodePolling();
    document.getElementById("viewer-video").pause();
  });
  document.getElementById("viewer-transcode").addEventListener("click", requestTranscode);
  document.getElementById("viewer-modal").addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft") step(-1);
    if (event.key === "ArrowRight") step(1);
  });
  const albumForm = document.getElementById("album-form");
  if (albumForm) {
    albumForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = document.getElementById("album-name");
      try {
        await apiFetch(`${API}/albums`, { method: "POST", body: { name: input.value.trim() } });
        input.value = "";
        await loadAlbums();
      } catch (error) {
        showError(error.message);
      }
    });
  }
}

async function init() {
  kindSelect.value = root.dataset.defaultKind || "";
  wireFilters();
  wireUploads();
  try {
    await Promise.all([loadLibraries(), loadAlbums()]);
  } catch (error) {
    showError(error.message);
  }
  await loadAdminData();
  await search(true);
}

init();
