// Follow the operating system colour scheme (Bootstrap 5.3 colour modes).
(function () {
  const query = window.matchMedia("(prefers-color-scheme: dark)");
  const apply = () => {
    document.documentElement.setAttribute("data-bs-theme", query.matches ? "dark" : "light");
  };
  apply();
  query.addEventListener("change", apply);
})();
