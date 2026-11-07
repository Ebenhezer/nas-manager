// Shared helpers for the explorer-style gallery: DOM building, icons,
// formatting and the page banners.

export const API = "/api/v1/media";

// Feather-style 24x24 stroke icons, matching the side navigation.
const ICONS = {
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  folderPlus: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z M12 11v6 M9 14h6",
  image: "M4 5h16v14H4z M4 16l5-5 4 4 3-3 4 4 M15.5 9.5h.01",
  video: "M3 6h13v12H3z M16 10l5-3v10l-5-3",
  star: "M12 3l2.8 5.7 6.2.9-4.5 4.4 1 6.2L12 17.3 6.5 20.2l1-6.2L3 9.6l6.2-.9z",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M12 7v5l3 2",
  library: "M4 4h4v16H4z M10 4h4v16h-4z M16 5l3.5-.8 3 15.6-3.5.8z",
  album: "M4 4h16v16H4z M4 15l4-4 3 3 5-5 4 4",
  gridXl: "M3 3h8v8H3z M13 3h8v8h-8z M3 13h8v8H3z M13 13h8v8h-8z",
  gridL: "M3 3h5v5H3z M10 3h5v5h-5z M17 3h4v5h-4z M3 10h5v5H3z M10 10h5v5h-5z M17 10h4v5h-4z M3 17h5v4H3z M10 17h5v4h-5z M17 17h4v4h-4z",
  gridS: "M3 3h3v3H3z M8 3h3v3H8z M13 3h3v3h-3z M18 3h3v3h-3z M3 8h3v3H3z M8 8h3v3H8z M13 8h3v3h-3z M18 8h3v3h-3z M3 13h3v3H3z M8 13h3v3H8z M13 13h3v3h-3z M18 13h3v3h-3z M3 18h3v3H3z M8 18h3v3H8z M13 18h3v3h-3z M18 18h3v3h-3z",
  list: "M8 6h13 M8 12h13 M8 18h13 M3 6h.01 M3 12h.01 M3 18h.01",
  details: "M3 4h18v16H3z M3 9h18 M3 14h18 M9 4v16",
  chevronRight: "M9 6l6 6-6 6",
  chevronDown: "M6 9l6 6 6-6",
  arrowLeft: "M19 12H5 M12 19l-7-7 7-7",
  arrowRight: "M5 12h14 M12 5l7 7-7 7",
  arrowUp: "M12 19V5 M5 12l7-7 7 7",
  upload: "M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2 M7 9l5-5 5 5 M12 4v12",
  download: "M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2 M7 11l5 5 5-5 M12 4v12",
  refresh: "M21 12a9 9 0 1 1-3-6.7L21 8 M21 3v5h-5",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z M21 21l-4.3-4.3",
  filter: "M3 5h18l-7 8v6l-4 2v-8z",
  sort: "M7 4v16 M3 16l4 4 4-4 M17 20V4 M13 8l4-4 4 4",
  x: "M18 6L6 18 M6 6l12 12",
  trash: "M3 6h18 M8 6V4h8v2 M6 6l1 14h10l1-14",
  move: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z M10 13h6 M13 10l3 3-3 3",
  play: "M7 4l13 8-13 8z",
  pause: "M7 4h4v16H7z M13 4h4v16h-4z",
  info: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M12 11v6 M12 7.5h.01",
  zoomIn: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z M21 21l-4.3-4.3 M11 8v6 M8 11h6",
  zoomOut: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z M21 21l-4.3-4.3 M8 11h6",
  maximize: "M4 9V4h5 M20 9V4h-5 M4 15v5h5 M20 15v5h-5",
  more: "M5 12h.01 M12 12h.01 M19 12h.01",
  check: "M5 12l5 5 9-11",
  edit: "M4 20h4L19 9l-4-4L4 16z M14 6l4 4",
  plus: "M12 5v14 M5 12h14",
  settings: "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  menu: "M3 6h18 M3 12h18 M3 18h18",
  copy: "M9 9h11v11H9z M5 15H4V4h11v1",
  usb: "M10 2h4v4h-4z M8 6h8v7a4 4 0 0 1-8 0z M12 17v5",
  drive: "M3 13h18v6H3z M3 13l3-8h12l3 8 M17 16h.01",
  file: "M14 3H6v18h12V7z M14 3v4h4",
  clipboard: "M9 4h6v3H9z M7 5H5v16h14V5h-2",
  location: "M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z M12 7.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z",
};

const SVG_NS = "http://www.w3.org/2000/svg";

export function icon(name, { size = 16, filled = false, className = "" } = {}) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", filled ? "currentColor" : "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", `mx-icon ${className}`.trim());
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", ICONS[name] || ICONS.image);
  svg.append(path);
  return svg;
}

// Replace the content of every [data-icon] element in ``root`` with its icon.
export function hydrateIcons(root = document) {
  for (const element of root.querySelectorAll("[data-icon]")) {
    const svg = icon(element.dataset.icon, { size: Number(element.dataset.iconSize) || 16 });
    element.prepend(svg);
    element.removeAttribute("data-icon");
  }
}

// Small element factory: el("button", { className, text, attrs, on }, ...children)
export function el(tag, options = {}, ...children) {
  const element = document.createElement(tag);
  if (options.className) element.className = options.className;
  if (options.text !== undefined) element.textContent = options.text;
  if (options.title) element.title = options.title;
  for (const [key, value] of Object.entries(options.attrs || {})) {
    if (value !== null && value !== undefined && value !== false) element.setAttribute(key, String(value));
  }
  for (const [key, value] of Object.entries(options.dataset || {})) {
    element.dataset[key] = String(value);
  }
  for (const [event, handler] of Object.entries(options.on || {})) {
    element.addEventListener(event, handler);
  }
  for (const child of children) {
    if (child !== null && child !== undefined && child !== false) element.append(child);
  }
  return element;
}

export function option(value, label) {
  return el("option", { text: label, attrs: { value: String(value) } });
}

export function libraryLabel(library) {
  if (library.is_own) return "My library";
  if (library.kind === "personal") return `${library.name.slice(1)} (personal)`;
  return library.name;
}

export function formatDate(value, { time = true } = {}) {
  if (!value) return "–";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return time ? date.toLocaleString() : date.toLocaleDateString();
}

export function formatSeconds(value) {
  const total = Math.round(Number(value) || 0);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}` : `${minutes}:${seconds}`;
}

export function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

export function thumbUrl(itemId, size = "small") {
  return `${API}/items/${itemId}/thumbnail?size=${size}`;
}

export function typeLabel(item) {
  const extension = item.name.includes(".") ? item.name.split(".").pop().toUpperCase() : "";
  return `${extension ? `${extension} ` : ""}${item.kind === "video" ? "video" : "image"}`;
}

const banner = document.getElementById("error-banner");
const infoBanner = document.getElementById("info-banner");
let infoTimer = null;

export function showError(message) {
  banner.textContent = message;
  banner.classList.remove("d-none");
}

export function hideError() {
  banner.classList.add("d-none");
}

export function showInfo(message) {
  infoBanner.textContent = message;
  infoBanner.classList.remove("d-none");
  window.clearTimeout(infoTimer);
  infoTimer = window.setTimeout(() => infoBanner.classList.add("d-none"), 6000);
}

// Absolute path on the server of ``relPath`` inside ``lib`` (administrators
// get the library's ``disk_path``; everyone else gets null).
export function serverPath(lib, relPath = "") {
  if (!lib || !lib.disk_path) return null;
  return relPath ? `${lib.disk_path.replace(/\/+$/, "")}/${relPath}` : lib.disk_path;
}

// Copy ``text``; the Clipboard API needs HTTPS or localhost, and a NAS is
// often opened over plain HTTP on the LAN, so fall back to execCommand.
export async function copyText(text, label = "Path") {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const area = el("textarea", { className: "mx-copy-buffer", attrs: { readonly: true, "aria-hidden": "true" } });
      area.value = text;
      document.body.append(area);
      area.select();
      const copied = document.execCommand("copy");
      area.remove();
      if (!copied) throw new Error("copy refused");
    }
    showInfo(`${label} copied: ${text}`);
  } catch (_ignored) {
    showError(`Could not copy automatically. ${label}: ${text}`);
  }
}

export function storageGet(key, fallback) {
  try {
    const value = window.localStorage.getItem(key);
    return value === null ? fallback : value;
  } catch (_ignored) {
    return fallback;
  }
}

export function storageSet(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (_ignored) {
    // private mode or blocked storage: the preference just is not kept
  }
}
