// Promise-based confirm / prompt / folder-picker dialogs on one Bootstrap modal.

import { apiFetch } from "../api.js";
import { API, el, icon } from "./util.js";

const modalElement = document.getElementById("mx-dialog");
const titleElement = document.getElementById("mx-dialog-title");
const bodyElement = document.getElementById("mx-dialog-body");
const okButton = document.getElementById("mx-dialog-ok");
const form = document.getElementById("mx-dialog-form");

let settle = null;

function modal() {
  return bootstrap.Modal.getOrCreateInstance(modalElement);
}

modalElement.addEventListener("hidden.bs.modal", () => {
  if (settle) settle(null);
  settle = null;
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!settle) return;
  const done = settle;
  settle = null;
  done(true);
  modal().hide();
});

function open({ title, okLabel = "OK", danger = false, body, onShown }) {
  titleElement.textContent = title;
  okButton.textContent = okLabel;
  okButton.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
  okButton.disabled = false;
  bodyElement.replaceChildren(...body);
  return new Promise((resolve) => {
    if (settle) settle(null);
    settle = resolve;
    if (onShown) modalElement.addEventListener("shown.bs.modal", onShown, { once: true });
    modal().show();
  });
}

export async function confirmDialog({ title, message, okLabel = "OK", danger = false }) {
  const result = await open({ title, okLabel, danger, body: [el("p", { className: "mb-0", text: message })] });
  return result !== null;
}

// Ask for a name. With ``selectStem`` the part before the extension is
// pre-selected, as a file manager does on rename.
export async function promptDialog({ title, label, value = "", okLabel = "OK", selectStem = false, maxLength = 255 }) {
  const input = el("input", {
    className: "form-control",
    attrs: { type: "text", id: "mx-dialog-input", maxlength: maxLength, required: true, autocomplete: "off" },
  });
  input.value = value;
  const result = await open({
    title,
    okLabel,
    body: [el("label", { className: "form-label", text: label, attrs: { for: "mx-dialog-input" } }), input],
    onShown: () => {
      input.focus();
      const dot = value.lastIndexOf(".");
      if (selectStem && dot > 0) input.setSelectionRange(0, dot);
      else input.select();
    },
  });
  if (result === null) return null;
  const text = input.value.trim();
  return text || null;
}

// Choose a folder inside ``libraryId``; resolves to its path ("" = top) or null.
export async function pickFolder({ title, libraryId, libraryName, start = "", okLabel = "Move here" }) {
  let current = start;
  const crumbs = el("nav", { className: "mx-picker-crumbs mb-2", attrs: { "aria-label": "Folder" } });
  const list = el("div", { className: "list-group mx-picker-list" });
  const newFolder = el(
    "button",
    { className: "btn btn-sm btn-outline-secondary mt-2", attrs: { type: "button" } },
    icon("folderPlus"),
    " New folder",
  );

  async function load(path) {
    current = path;
    crumbs.replaceChildren();
    const parts = path ? path.split("/") : [];
    const trail = [[libraryName, ""], ...parts.map((part, index) => [part, parts.slice(0, index + 1).join("/")])];
    trail.forEach(([name, target], index) => {
      if (index) crumbs.append(el("span", { className: "text-body-secondary mx-1", text: "›" }));
      crumbs.append(
        el("button", {
          className: "btn btn-link btn-sm p-0",
          text: name,
          attrs: { type: "button" },
          on: { click: () => load(target) },
        }),
      );
    });
    list.replaceChildren(el("div", { className: "list-group-item text-body-secondary small", text: "Loading…" }));
    try {
      const result = await apiFetch(`${API}/libraries/${libraryId}/folders?${new URLSearchParams({ path })}`);
      list.replaceChildren();
      for (const folder of result.folders) {
        list.append(
          el(
            "button",
            {
              className: "list-group-item list-group-item-action d-flex align-items-center gap-2",
              attrs: { type: "button" },
              on: { click: () => load(folder.path) },
            },
            icon("folder"),
            el("span", { className: "text-truncate", text: folder.name }),
          ),
        );
      }
      if (!result.folders.length) {
        list.append(el("div", { className: "list-group-item text-body-secondary small", text: "No subfolders." }));
      }
    } catch (error) {
      list.replaceChildren(el("div", { className: "list-group-item text-danger small", text: error.message }));
    }
  }

  newFolder.addEventListener("click", () => {
    const input = el("input", {
      className: "form-control form-control-sm",
      attrs: { type: "text", maxlength: 255, placeholder: "Folder name", "aria-label": "New folder name" },
    });
    const create = el("button", { className: "btn btn-sm btn-primary", text: "Create", attrs: { type: "button" } });
    const row = el("div", { className: "input-group input-group-sm mt-2" }, input, create);
    newFolder.replaceWith(row);
    input.focus();
    const submit = async () => {
      const name = input.value.trim();
      if (!name) return;
      const path = current ? `${current}/${name}` : name;
      try {
        await apiFetch(`${API}/libraries/${libraryId}/folders`, { method: "POST", body: { path } });
        row.replaceWith(newFolder);
        await load(path);
      } catch (error) {
        input.classList.add("is-invalid");
        input.title = error.message;
      }
    };
    create.addEventListener("click", submit);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submit();
      }
    });
  });

  const pending = open({ title, okLabel, body: [crumbs, list, newFolder] });
  await load(start);
  const result = await pending;
  return result === null ? null : current;
}
