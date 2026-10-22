import { apiFetch } from "./api.js";

const banner = document.getElementById("error-banner");
const toggle = document.getElementById("operator-view-files-samba");

function showError(message) {
  banner.textContent = message;
  banner.classList.remove("d-none");
  banner.classList.add("alert-danger");
}

function hideError() {
  banner.classList.add("d-none");
}

async function refresh() {
  hideError();
  try {
    const settings = await apiFetch("/api/v1/settings");
    toggle.checked = settings.operator_view_files_samba;
  } catch (error) {
    showError(`Could not load settings: ${error.message}`);
  }
}

toggle.addEventListener("change", async () => {
  hideError();
  const enabled = toggle.checked;
  try {
    await apiFetch("/api/v1/settings", {
      method: "PUT",
      body: { operator_view_files_samba: enabled },
    });
    banner.textContent = enabled
      ? "Operators can now view the Files and Samba pages (read-only)."
      : "Operators can no longer view the Files and Samba pages.";
    banner.classList.remove("d-none");
    banner.classList.remove("alert-danger");
    banner.classList.add("alert-success");
  } catch (error) {
    toggle.checked = !enabled;
    showError(error.message);
  }
});

refresh();
