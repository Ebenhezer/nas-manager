// A context menu at the pointer (right-click, long-press or a "⋯" button).
// Entries: { label, icon, action, danger, disabled } | { divider: true } | { header }

import { el, icon } from "./util.js";

const menu = document.getElementById("mx-context-menu");
let returnFocus = null;

export function hideMenu() {
  if (!menu.classList.contains("show")) return;
  menu.classList.remove("show");
  menu.replaceChildren();
  if (returnFocus && document.contains(returnFocus)) returnFocus.focus();
  returnFocus = null;
}

export function showMenu(x, y, entries) {
  returnFocus = document.activeElement;
  menu.replaceChildren();
  for (const entry of entries) {
    if (entry.divider) {
      menu.append(el("hr", { className: "dropdown-divider" }));
    } else if (entry.header) {
      menu.append(el("h6", { className: "dropdown-header", text: entry.header }));
    } else {
      const button = el(
        "button",
        {
          className: `dropdown-item d-flex align-items-center gap-2${entry.danger ? " text-danger" : ""}`,
          attrs: { type: "button", role: "menuitem", disabled: entry.disabled },
          on: {
            click: (event) => {
              event.stopPropagation();
              hideMenu();
              entry.action();
            },
          },
        },
        entry.icon ? icon(entry.icon, { filled: entry.filled }) : el("span", { className: "mx-icon-space" }),
        el("span", { text: entry.label }),
      );
      menu.append(button);
    }
  }
  menu.classList.add("show");
  // Keep the menu on screen.
  const { innerWidth, innerHeight } = window;
  const rect = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(4, Math.min(x, innerWidth - rect.width - 4))}px`;
  menu.style.top = `${Math.max(4, Math.min(y, innerHeight - rect.height - 4))}px`;
  const first = menu.querySelector("button:not([disabled])");
  if (first) first.focus();
}

// Open the menu below an element (keyboard / button use).
export function showMenuAt(element, entries) {
  const rect = element.getBoundingClientRect();
  showMenu(rect.left, rect.bottom + 2, entries);
}

document.addEventListener("pointerdown", (event) => {
  if (!menu.contains(event.target)) hideMenu();
});
window.addEventListener("blur", hideMenu);
window.addEventListener("resize", hideMenu);
document.addEventListener("scroll", hideMenu, true);
menu.addEventListener("keydown", (event) => {
  const buttons = Array.from(menu.querySelectorAll("button:not([disabled])"));
  const index = buttons.indexOf(document.activeElement);
  if (event.key === "Escape") {
    event.preventDefault();
    hideMenu();
  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    const next = buttons[(index + step + buttons.length) % buttons.length];
    if (next) next.focus();
  } else if (event.key === "Tab") {
    hideMenu();
  }
});

// Long-press on touch screens opens the context menu.
export function onLongPress(element, handler) {
  let timer = null;
  let start = null;
  element.addEventListener("pointerdown", (event) => {
    if (event.pointerType !== "touch") return;
    start = { x: event.clientX, y: event.clientY, target: event.target };
    timer = window.setTimeout(() => {
      timer = null;
      handler({ clientX: start.x, clientY: start.y, target: start.target, preventDefault() {} });
    }, 550);
  });
  const cancel = (event) => {
    if (!timer) return;
    if (event.type === "pointermove" && Math.hypot(event.clientX - start.x, event.clientY - start.y) < 10) return;
    window.clearTimeout(timer);
    timer = null;
  };
  element.addEventListener("pointermove", cancel);
  element.addEventListener("pointerup", cancel);
  element.addEventListener("pointercancel", cancel);
}
