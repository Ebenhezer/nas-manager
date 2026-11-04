// Uploads into the caller's personal library: into the folder being browsed
// when that folder is writable, otherwise into the default YYYY/MM folders.

import { getCsrfToken } from "../api.js";
import { hooks, locationIsWritable, page, state } from "./state.js";
import { API, el, plural, showError, showInfo } from "./util.js";

const status = document.getElementById("mx-upload-status");

function uploadOne(file, folder, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file, file.name);
    if (file.lastModified) form.append("taken_at", new Date(file.lastModified).toISOString());
    // "/" is the library's top folder; leaving it out means YYYY/MM.
    if (folder !== null) form.append("path", folder || "/");
    const request = new XMLHttpRequest();
    request.open("POST", `${API}/upload`);
    request.setRequestHeader("X-CSRF-Token", getCsrfToken());
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    });
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

// Where uploads land: the current folder if writable, else null (default folders).
export function uploadTarget() {
  return locationIsWritable() ? state.location.folder : null;
}

export function uploadTargetLabel() {
  const folder = uploadTarget();
  if (folder === null) return "your library (dated folders)";
  return folder ? `“${folder.split("/").pop()}”` : "the top folder of your library";
}

export async function uploadFiles(fileList) {
  if (!page.canWrite) return;
  const files = Array.from(fileList);
  if (!files.length) return;
  const folder = uploadTarget();
  const failures = [];
  const bar = el("div", { className: "progress mx-upload-progress", attrs: { role: "progressbar", "aria-label": "Upload progress" } });
  const fill = el("div", { className: "progress-bar" });
  bar.append(fill);
  const label = el("span");
  status.replaceChildren(label, bar);
  for (let index = 0; index < files.length; index += 1) {
    label.textContent = `Uploading ${index + 1} of ${files.length}…`;
    try {
      await uploadOne(files[index], folder, (fraction) => {
        fill.style.width = `${Math.round(((index + fraction) / files.length) * 100)}%`;
      });
    } catch (error) {
      failures.push(error.message);
    }
  }
  status.replaceChildren();
  if (failures.length) showError(failures.join("; "));
  const sent = files.length - failures.length;
  if (sent) showInfo(`Uploaded ${plural(sent, "file")}.`);
  await Promise.all([hooks.reloadLibraries(), hooks.reload()]);
}

export function wireUploads() {
  if (!page.canWrite) return;
  const input = document.getElementById("mx-upload-input");
  document.getElementById("mx-upload").addEventListener("click", () => input.click());
  input.addEventListener("change", () => {
    if (input.files && input.files.length) uploadFiles(input.files);
    input.value = "";
  });
}
