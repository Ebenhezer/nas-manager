// Full-screen lightbox: prev/next with keys and swipes, zoom and pan
// (wheel, pinch, double-click), a slideshow, a filmstrip, a details panel
// and the video playback / transcode flow.

import { apiFetch } from "../api.js";
import { formatBytes } from "../format.js";
import { addToAlbum, addToNewAlbum, deleteItems, downloadUrl, itemWritable, setFavorite } from "./actions.js";
import { loadMore } from "./browser.js";
import { showMenuAt } from "./menu.js";
import { library, page, state } from "./state.js";
import { API, el, formatDate, formatSeconds, icon, libraryLabel, showError, thumbUrl } from "./util.js";

const box = document.getElementById("mx-lightbox");
const stage = document.getElementById("mx-lb-stage");
const image = document.getElementById("mx-lb-image");
const video = document.getElementById("mx-lb-video");
const filmstrip = document.getElementById("mx-lb-filmstrip");
const infoPanel = document.getElementById("mx-lb-info");
const slideshowButton = document.getElementById("mx-lb-slideshow");

// Types browsers render natively; others are shown from the large thumbnail.
const BROWSER_IMAGES = new Set(["image/jpeg", "image/png", "image/gif", "image/webp", "image/avif", "image/bmp"]);
const MAX_ZOOM = 8;

let returnFocus = null;
let transcodeTimer = null;
let slideshowTimer = null;
let slideshowOn = false;
const view = { scale: 1, x: 0, y: 0 };

function current() {
  return state.items[state.current] || null;
}

export function isOpen() {
  return !box.classList.contains("d-none");
}

// ---------------------------------------------------------------------------
// Details panel
// ---------------------------------------------------------------------------

function metaRow(list, label, value) {
  if (value === null || value === undefined || value === "") return;
  list.append(
    el("dt", { className: "col-5 mx-lb-muted", text: label }),
    el("dd", { className: "col-7 text-break", text: value }),
  );
}

function renderMeta(item) {
  const list = document.getElementById("mx-lb-meta");
  list.replaceChildren();
  const lib = library(item.library_id);
  metaRow(list, "Name", item.name);
  metaRow(list, "Library", lib ? libraryLabel(lib) : String(item.library_id));
  metaRow(list, "Folder", item.rel_path.includes("/") ? item.rel_path.slice(0, item.rel_path.lastIndexOf("/")) : "(top)");
  metaRow(list, "Taken", formatDate(item.taken_at));
  metaRow(list, "Size", formatBytes(item.size));
  if (item.width && item.height) {
    const megapixels = (item.width * item.height) / 1e6;
    metaRow(list, "Dimensions", `${item.width} × ${item.height}${item.kind === "image" ? ` (${megapixels.toFixed(1)} MP)` : ""}`);
  }
  metaRow(list, "Camera", [item.camera_make, item.camera_model].filter(Boolean).join(" "));
  if (item.gps_lat !== null && item.gps_lon !== null) {
    metaRow(list, "Location", `${item.gps_lat.toFixed(5)}, ${item.gps_lon.toFixed(5)}`);
  }
  if (item.kind === "video") {
    if (item.duration_seconds) metaRow(list, "Duration", formatSeconds(item.duration_seconds));
    const playback = { direct: "Plays directly", transcode: "Needs a transcode", unsupported: "Not playable" };
    metaRow(list, "Playback", playback[item.playback] || "Not analysed");
    metaRow(list, "Codecs", [item.video_codec, item.audio_codec].filter(Boolean).join(" / "));
    metaRow(list, "Container", item.container);
    if (item.bitrate) metaRow(list, "Bitrate", `${(item.bitrate / 1e6).toFixed(1)} Mbit/s`);
  }
  metaRow(list, "Type", item.mime);
}

function setInfo(open) {
  infoPanel.classList.toggle("d-none", !open);
  document.getElementById("mx-lb-info-toggle").setAttribute("aria-pressed", open ? "true" : "false");
}

// ---------------------------------------------------------------------------
// Zoom and pan
// ---------------------------------------------------------------------------

function applyTransform() {
  image.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
  image.classList.toggle("is-zoomed", view.scale > 1);
  stage.classList.toggle("is-zoomed", view.scale > 1);
}

function resetZoom() {
  view.scale = 1;
  view.x = 0;
  view.y = 0;
  applyTransform();
}

// Zoom to ``scale`` keeping the stage point (cx, cy) fixed on screen.
function zoomTo(scale, cx, cy) {
  const item = current();
  if (!item || item.kind !== "image") return;
  const next = Math.max(1, Math.min(MAX_ZOOM, scale));
  const rect = stage.getBoundingClientRect();
  const px = (cx ?? rect.left + rect.width / 2) - (rect.left + rect.width / 2);
  const py = (cy ?? rect.top + rect.height / 2) - (rect.top + rect.height / 2);
  view.x = px - ((px - view.x) * next) / view.scale;
  view.y = py - ((py - view.y) * next) / view.scale;
  view.scale = next;
  if (next === 1) {
    view.x = 0;
    view.y = 0;
  }
  // Load the full-resolution original once zoomed in, when the browser can show it.
  if (next > 1.5 && image.dataset.full === "false" && BROWSER_IMAGES.has(item.mime)) {
    image.dataset.full = "true";
    image.src = `${API}/items/${item.id}/original`;
  }
  applyTransform();
}

stage.addEventListener(
  "wheel",
  (event) => {
    if (!isOpen() || current()?.kind !== "image") return;
    event.preventDefault();
    zoomTo(view.scale * Math.exp(-event.deltaY * 0.0015), event.clientX, event.clientY);
  },
  { passive: false },
);

image.addEventListener("dblclick", (event) => {
  zoomTo(view.scale > 1 ? 1 : 2.5, event.clientX, event.clientY);
});

const pointers = new Map();
let gesture = null;

stage.addEventListener("pointerdown", (event) => {
  if (event.target.closest("button, video, .mx-lb-status")) return;
  pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  stage.setPointerCapture(event.pointerId);
  if (pointers.size === 2) {
    const [a, b] = Array.from(pointers.values());
    gesture = { type: "pinch", distance: Math.hypot(a.x - b.x, a.y - b.y), scale: view.scale };
  } else {
    gesture = { type: view.scale > 1 ? "pan" : "swipe", startX: event.clientX, startY: event.clientY, x: view.x, y: view.y };
  }
});

stage.addEventListener("pointermove", (event) => {
  if (!pointers.has(event.pointerId) || !gesture) return;
  pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  if (gesture.type === "pinch" && pointers.size === 2) {
    const [a, b] = Array.from(pointers.values());
    const distance = Math.hypot(a.x - b.x, a.y - b.y);
    zoomTo((gesture.scale * distance) / gesture.distance, (a.x + b.x) / 2, (a.y + b.y) / 2);
  } else if (gesture.type === "pan") {
    view.x = gesture.x + event.clientX - gesture.startX;
    view.y = gesture.y + event.clientY - gesture.startY;
    applyTransform();
  }
});

function endPointer(event) {
  if (!pointers.has(event.pointerId)) return;
  pointers.delete(event.pointerId);
  if (gesture && gesture.type === "swipe" && pointers.size === 0) {
    const dx = event.clientX - gesture.startX;
    const dy = event.clientY - gesture.startY;
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) step(dx < 0 ? 1 : -1);
    else if (dy > 120 && Math.abs(dy) > Math.abs(dx) * 1.5) requestClose();
  }
  if (pointers.size === 0) gesture = null;
}

stage.addEventListener("pointerup", endPointer);
stage.addEventListener("pointercancel", endPointer);

// ---------------------------------------------------------------------------
// Video
// ---------------------------------------------------------------------------

function showVideoMessage(message, canTranscode = false) {
  document.getElementById("mx-lb-video-status").classList.remove("d-none");
  document.getElementById("mx-lb-video-message").textContent = message;
  document.getElementById("mx-lb-transcode").classList.toggle("d-none", !canTranscode);
}

function stopTranscodePolling() {
  window.clearTimeout(transcodeTimer);
  transcodeTimer = null;
}

function playVideo(item) {
  if (item.thumb_state === "ready") video.poster = thumbUrl(item.id, "large");
  else video.removeAttribute("poster");
  video.src = `${API}/items/${item.id}/stream`;
  video.classList.remove("d-none");
  if (slideshowOn) video.play().catch(() => {});
}

function describeTranscode(status) {
  if (status.state === "running") return `Preparing for playback… ${Math.round(status.progress_percent)}%`;
  if (status.state === "queued") return "Waiting for the worker to prepare this video…";
  if (status.state === "failed") return `Preparing this video failed: ${status.error || "unknown error"}`;
  return "This video's format cannot be played in the browser directly.";
}

async function pollTranscode(item) {
  stopTranscodePolling();
  if (current() !== item) return;
  try {
    const status = await apiFetch(`${API}/items/${item.id}/transcode`);
    if (current() !== item) return;
    if (status.state === "ready") {
      document.getElementById("mx-lb-video-status").classList.add("d-none");
      playVideo(item);
      return;
    }
    if (!status.transcoding_available) {
      showVideoMessage("This video's format cannot be played in the browser, and transcoding is not available on this server. Use Download instead.");
      return;
    }
    const idle = status.state === null || status.state === "failed";
    showVideoMessage(describeTranscode(status), idle);
    if (!idle) transcodeTimer = window.setTimeout(() => pollTranscode(item), 3000);
  } catch (error) {
    showError(error.message);
  }
}

async function requestTranscode() {
  const item = current();
  if (!item) return;
  try {
    await apiFetch(`${API}/items/${item.id}/transcode`, { method: "POST" });
    await pollTranscode(item);
  } catch (error) {
    showError(error.message);
  }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function renderMedia(item) {
  stopTranscodePolling();
  document.getElementById("mx-lb-video-status").classList.add("d-none");
  document.getElementById("mx-lb-transcode").classList.add("d-none");
  video.pause();
  video.removeAttribute("src");
  video.load();
  video.classList.add("d-none");
  image.classList.add("d-none");
  resetZoom();
  if (item.kind === "image") {
    image.alt = item.name;
    const useThumb = item.thumb_state === "ready" || item.thumb_state === "pending";
    image.dataset.full = useThumb ? "false" : "true";
    image.src = useThumb ? thumbUrl(item.id, "large") : `${API}/items/${item.id}/original`;
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
  for (const id of ["mx-lb-zoom-in", "mx-lb-zoom-out"]) {
    document.getElementById(id).disabled = item.kind !== "image";
  }
}

function renderFilmstrip() {
  filmstrip.replaceChildren();
  const from = Math.max(0, state.current - 20);
  const to = Math.min(state.items.length, state.current + 21);
  for (let index = from; index < to; index += 1) {
    const item = state.items[index];
    const thumb = el(
      "button",
      {
        className: `mx-lb-thumb${index === state.current ? " is-current" : ""}`,
        attrs: { type: "button", "aria-label": item.name, "aria-current": index === state.current ? "true" : null },
        on: { click: () => show(index) },
      },
      el("img", { attrs: { src: thumbUrl(item.id), alt: "", loading: "lazy", draggable: "false" } }),
    );
    if (item.kind === "video") thumb.append(icon("play", { size: 12, filled: true, className: "mx-lb-thumb-play" }));
    filmstrip.append(thumb);
  }
  const active = filmstrip.querySelector(".is-current");
  if (active) active.scrollIntoView({ inline: "center", block: "nearest" });
}

function renderActions(item) {
  const fav = document.getElementById("mx-lb-fav");
  fav.replaceChildren(icon("star", { size: 20, filled: item.is_favorite }));
  fav.setAttribute("aria-pressed", item.is_favorite ? "true" : "false");
  fav.setAttribute("aria-label", item.is_favorite ? "Remove from favorites" : "Add to favorites");
  fav.classList.toggle("is-on", item.is_favorite);
  document.getElementById("mx-lb-download").href = downloadUrl([item]);
  document.getElementById("mx-lb-delete").classList.toggle("d-none", !itemWritable(item));
  document.getElementById("mx-lb-album").classList.toggle("d-none", !page.canWrite);
}

function show(index) {
  const item = state.items[index];
  if (!item) return;
  state.current = index;
  document.getElementById("mx-lb-title").textContent = item.name;
  document.getElementById("mx-lb-counter").textContent = `${index + 1} of ${state.total} · ${formatDate(item.taken_at)}`;
  renderMedia(item);
  renderMeta(item);
  renderActions(item);
  renderFilmstrip();
  document.getElementById("mx-lb-prev").disabled = index <= 0;
  document.getElementById("mx-lb-next").disabled = index >= state.total - 1;
  // Preload the neighbours.
  for (const near of [state.items[index + 1], state.items[index - 1]]) {
    if (near && near.kind === "image" && near.thumb_state !== "failed") new Image().src = thumbUrl(near.id, "large");
  }
  scheduleSlide();
}

async function step(delta) {
  const next = state.current + delta;
  if (next >= state.items.length && state.items.length < state.total) await loadMore();
  if (next >= 0 && next < state.items.length) {
    show(next);
  } else if (slideshowOn) {
    toggleSlideshow(false);
  }
}

// ---------------------------------------------------------------------------
// Slideshow
// ---------------------------------------------------------------------------

function scheduleSlide() {
  window.clearTimeout(slideshowTimer);
  if (!slideshowOn) return;
  const item = current();
  if (item && item.kind === "video" && item.playback === "direct") return; // advances on "ended"
  const seconds = Number(document.getElementById("mx-lb-interval").value) || 5;
  slideshowTimer = window.setTimeout(() => step(1), seconds * 1000);
}

function toggleSlideshow(on = !slideshowOn) {
  slideshowOn = on;
  box.classList.toggle("is-slideshow", on);
  slideshowButton.replaceChildren(icon(on ? "pause" : "play", { size: 20 }));
  slideshowButton.setAttribute("aria-label", on ? "Pause slideshow" : "Start slideshow");
  if (on && current()?.kind === "video" && video.src) video.play().catch(() => {});
  scheduleSlide();
}

video.addEventListener("ended", () => {
  if (slideshowOn) step(1);
});

// ---------------------------------------------------------------------------
// Open / close
// ---------------------------------------------------------------------------

// The open viewer has its own history entry, so Back closes it (phones).
function requestClose() {
  if (window.history.state && window.history.state.mxViewer) window.history.back();
  else closeViewer();
}

window.addEventListener("popstate", () => {
  if (isOpen() && !(window.history.state && window.history.state.mxViewer)) closeViewer();
});

export function openViewer(index, { info = false } = {}) {
  if (!state.items[index]) return;
  if (!isOpen()) {
    window.history.pushState({ mxViewer: true }, "", window.location.href);
    returnFocus = document.activeElement;
    box.classList.remove("d-none");
    document.body.classList.add("mx-lightbox-open");
  }
  setInfo(info || !infoPanel.classList.contains("d-none"));
  show(index);
  box.focus();
}

export function closeViewer() {
  if (!isOpen()) return;
  toggleSlideshow(false);
  stopTranscodePolling();
  video.pause();
  video.removeAttribute("src");
  video.load();
  image.removeAttribute("src");
  box.classList.add("d-none");
  document.body.classList.remove("mx-lightbox-open");
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  // Return focus to the item that was shown (it may be a different one now).
  const entry = document.querySelector(`.mx-item[data-index="${state.current}"]`);
  const target = entry || returnFocus;
  if (target && document.contains(target)) {
    target.focus({ preventScroll: false });
    if (entry) entry.scrollIntoView({ block: "nearest" });
  }
  state.current = -1;
}

async function deleteCurrent() {
  const item = current();
  if (!item) return;
  const index = state.current;
  if (await deleteItems([item])) {
    if (state.items.length) openViewer(Math.min(index, state.items.length - 1));
    else requestClose();
  }
}

function albumMenu(anchor) {
  const item = current();
  if (!item) return;
  const entries = [{ header: "Add to album" }];
  for (const entry of state.albums) entries.push({ label: entry.name, icon: "album", action: () => addToAlbum([item], entry.id) });
  entries.push({ divider: true }, { label: "New album…", icon: "plus", action: () => addToNewAlbum([item]) });
  showMenuAt(anchor, entries);
}

export function wireViewer() {
  document.getElementById("mx-lb-close").addEventListener("click", requestClose);
  document.getElementById("mx-lb-prev").addEventListener("click", () => step(-1));
  document.getElementById("mx-lb-next").addEventListener("click", () => step(1));
  document.getElementById("mx-lb-transcode").addEventListener("click", requestTranscode);
  document.getElementById("mx-lb-zoom-in").addEventListener("click", () => zoomTo(view.scale * 1.5));
  document.getElementById("mx-lb-zoom-out").addEventListener("click", () => zoomTo(view.scale / 1.5));
  slideshowButton.addEventListener("click", () => toggleSlideshow());
  document.getElementById("mx-lb-interval").addEventListener("change", scheduleSlide);
  document.getElementById("mx-lb-info-toggle").addEventListener("click", () => setInfo(infoPanel.classList.contains("d-none")));
  document.getElementById("mx-lb-fav").addEventListener("click", async () => {
    const item = current();
    if (!item) return;
    await setFavorite([item], !item.is_favorite);
    if (current() === item) renderActions(item);
    else if (current()) show(state.current);
    else requestClose();
  });
  document.getElementById("mx-lb-album").addEventListener("click", (event) => albumMenu(event.currentTarget));
  document.getElementById("mx-lb-delete").addEventListener("click", deleteCurrent);
  document.getElementById("mx-lb-fullscreen").addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else if (box.requestFullscreen) box.requestFullscreen().catch(() => {});
  });
  box.addEventListener("keydown", (event) => {
    if (event.target.closest("select, .mx-context-menu")) return;
    const keys = {
      ArrowLeft: () => step(-1),
      ArrowRight: () => step(1),
      Escape: () => requestClose(),
      " ": () => toggleSlideshow(),
      "+": () => zoomTo(view.scale * 1.5),
      "=": () => zoomTo(view.scale * 1.5),
      "-": () => zoomTo(view.scale / 1.5),
      "0": () => resetZoom(),
      f: () => document.getElementById("mx-lb-fullscreen").click(),
      i: () => setInfo(infoPanel.classList.contains("d-none")),
      s: () => document.getElementById("mx-lb-fav").click(),
      Delete: () => deleteCurrent(),
      Home: () => show(0),
      End: () => show(state.items.length - 1),
    };
    if (event.target === video && (event.key === " " || event.key.startsWith("Arrow"))) return;
    const action = keys[event.key];
    if (!action || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === "Escape" && document.fullscreenElement) return; // the browser leaves full screen first
    event.preventDefault();
    action();
  });
  // Keep Tab inside the dialog.
  box.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const focusable = Array.from(box.querySelectorAll("button:not([disabled]), a[href], select, video[controls]")).filter(
      (node) => node.offsetParent !== null,
    );
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
}
