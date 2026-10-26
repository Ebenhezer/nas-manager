import { apiFetch } from "./api.js";

const banner = document.getElementById("error-banner");
const toggles = {
  operator_view_files_samba: document.getElementById("operator-view-files-samba"),
  public_upload_enabled: document.getElementById("public-upload-enabled"),
};

function showError(message) {
  banner.textContent = message;
  banner.classList.remove("d-none");
  banner.classList.add("alert-danger");
}

function hideError() {
  banner.classList.add("d-none");
}

function showSuccess(message) {
  banner.textContent = message;
  banner.classList.remove("d-none");
  banner.classList.remove("alert-danger");
  banner.classList.add("alert-success");
}

async function refresh() {
  hideError();
  try {
    const settings = await apiFetch("/api/v1/settings");
    for (const [key, toggle] of Object.entries(toggles)) {
      if (toggle) {
        toggle.checked = settings[key];
      }
    }
  } catch (error) {
    showError(`Could not load settings: ${error.message}`);
  }
}

for (const [key, toggle] of Object.entries(toggles)) {
  if (!toggle) {
    continue;
  }
  toggle.addEventListener("change", async () => {
    hideError();
    const enabled = toggle.checked;
    const payload = {};
    payload[key] = enabled;
    try {
      await apiFetch("/api/v1/settings", { method: "PUT", body: payload });
      if (key === "operator_view_files_samba") {
        showSuccess(
          enabled
            ? "Operators can now view the Files and Samba pages (read-only)."
            : "Operators can no longer view the Files and Samba pages.",
        );
      } else {
        showSuccess(
          enabled
            ? "Public uploads are accepted in the drop folder."
            : "Public uploads are rejected; existing files are kept.",
        );
      }
    } catch (error) {
      toggle.checked = !enabled;
      showError(error.message);
    }
  });
}

refresh();
