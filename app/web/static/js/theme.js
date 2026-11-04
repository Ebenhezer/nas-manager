// Bootstrap 5.3 colour modes: follow the operating system unless the user
// has picked a theme with the top-bar toggle (remembered per browser).
(function () {
  const KEY = "nas-theme";
  const query = window.matchMedia("(prefers-color-scheme: dark)");

  const saved = () => {
    try {
      const value = localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : null;
    } catch (e) {
      return null;
    }
  };
  const current = () => saved() || (query.matches ? "dark" : "light");

  const syncToggles = () => {
    const dark = current() === "dark";
    const label = dark ? "Switch to light theme" : "Switch to dark theme";
    document.querySelectorAll("[data-theme-toggle]").forEach((btn) => {
      btn.setAttribute("aria-label", label);
      btn.setAttribute("title", label);
      btn.setAttribute("aria-pressed", String(dark));
    });
  };

  const apply = () => {
    document.documentElement.setAttribute("data-bs-theme", current());
    syncToggles();
  };

  apply();
  query.addEventListener("change", () => {
    if (!saved()) apply();
  });

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-theme-toggle]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const next = current() === "dark" ? "light" : "dark";
        try {
          localStorage.setItem(KEY, next);
        } catch (e) {
          // Storage unavailable: still switch for this page view.
          document.documentElement.setAttribute("data-bs-theme", next);
          return;
        }
        apply();
      });
    });
    syncToggles();
  });
})();
