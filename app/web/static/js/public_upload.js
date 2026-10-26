import { getCsrfToken } from "./api.js";
import { formatBytes } from "./format.js";

const dropZone = document.getElementById("drop-zone");
const fileInput = document.getElementById("file-input");
const dropLabel = document.getElementById("drop-label");
const policyText = document.getElementById("policy-text");
const messages = document.getElementById("messages");

let enabled = false;
let maxFileBytes = 0;

function addMessage(text, ok) {
  const div = document.createElement("div");
  div.className = `alert alert-${ok ? "success" : "danger"} d-flex align-items-center gap-2 mt-1`;
  div.textContent = text;
  messages.prepend(div);
}

async function loadPolicy() {
  const response = await fetch("/api/v1/public/uploads");
  const policy = await response.json().catch(() => ({}));
  if (!response.ok) {
    policyText.textContent = "The folder is temporarily unavailable.";
    return;
  }
  enabled = policy.enabled;
  maxFileBytes = policy.max_file_bytes;
  if (enabled) {
    policyText.textContent =
      `${formatBytes(maxFileBytes)} max per file. Quota per uploader enforced.`;
    fileInput.removeAttribute("disabled");
  } else {
    policyText.textContent = "Uploads are currently disabled by the administrator.";
    fileInput.setAttribute("disabled", "");
  }
}

async function uploadOne(file) {
  const form = new FormData();
  form.append("file", file, file.name);
  const response = await fetch("/api/v1/public/uploads", {
    method: "POST",
    headers: { "X-CSRF-Token": getCsrfToken() },
    body: form,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = body && body.error ? body.error.message : `${response.status} upload failed`;
    throw new Error(`${file.name}: ${message}`);
  }
  return body;
}

async function uploadFiles(files) {
  for (const file of files) {
    if (!enabled) {
      addMessage("Uploads are disabled.", false);
      break;
    }
    addMessage(`Uploading ${file.name}…`, true);
    try {
      const result = await uploadOne(file);
      const finalName = result.filename !== file.name ? ` (stored as ${result.filename})` : "";
      addMessage(`${file.name}: uploaded (${formatBytes(result.size_bytes)})${finalName}.`, true);
    } catch (error) {
      addMessage(error.message, false);
    }
  }
}

fileInput.addEventListener("change", () => {
  if (fileInput.files) {
    uploadFiles(Array.from(fileInput.files));
  }
  fileInput.value = "";
});

dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    fileInput.click();
  }
});
dropZone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropZone.classList.add("border-primary");
});
dropZone.addEventListener("dragleave", () => {
  dropZone.classList.remove("border-primary");
});
dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropZone.classList.remove("border-primary");
  if (event.dataTransfer && event.dataTransfer.files) {
    uploadFiles(Array.from(event.dataTransfer.files));
  }
});

loadPolicy();