import { apiFetch } from "./api.js";

const form = document.getElementById("change-password-form");
const banner = document.getElementById("error-banner");
const success = document.getElementById("success-banner");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  banner.classList.add("d-none");
  success.classList.add("d-none");
  const data = new FormData(form);
  const payload = {
    current_password: String(data.get("current")),
    new_password: String(data.get("new")),
  };
  try {
    await apiFetch("/api/v1/auth/change-password", { method: "POST", body: payload });
    success.textContent = "Password changed.";
    success.classList.remove("d-none");
    form.reset();
  } catch (error) {
    banner.textContent = error.message;
    banner.classList.remove("d-none");
  }
});