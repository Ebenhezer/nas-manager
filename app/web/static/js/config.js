import { apiFetch, getJson } from "./api.js";

const root = document.getElementById("config-root");
const canImport = root.dataset.canImport === "true";
const banner = document.getElementById("error-banner");
const infoBanner = document.getElementById("info-banner");

const state = { document: null, sourceName: "", passphrase: "", mode: "merge" };

function $(id) {
  return document.getElementById(id);
}

function showError(message) {
  banner.textContent = message;
  banner.classList.remove("d-none");
}

function hideError() {
  banner.classList.add("d-none");
}

function showInfo(message) {
  infoBanner.textContent = message;
  infoBanner.classList.remove("d-none");
}

function cell(text, className = "") {
  const td = document.createElement("td");
  if (className) td.className = className;
  td.textContent = text;
  return td;
}

function listItems(list, values) {
  list.replaceChildren();
  for (const value of values) {
    const li = document.createElement("li");
    li.textContent = value;
    list.append(li);
  }
}

function when(value) {
  return value ? new Date(`${value}${value.endsWith("Z") ? "" : "Z"}`).toLocaleString() : "–";
}

function confirmDialog(title, lines, warnings, action) {
  $("confirm-modal-title").textContent = title;
  const plan = $("confirm-modal-plan");
  plan.replaceChildren();
  for (const line of lines) {
    const p = document.createElement("p");
    p.className = "mb-1";
    p.textContent = line;
    plan.append(p);
  }
  listItems($("confirm-modal-warnings"), warnings);
  const modal = bootstrap.Modal.getOrCreateInstance($("confirm-modal"));
  $("confirm-modal-accept").onclick = async () => {
    modal.hide();
    try {
      await action();
    } catch (error) {
      showError(error.message);
    }
  };
  modal.show();
}

async function withConfirmation(title, send) {
  try {
    return await send(false);
  } catch (error) {
    if (error.code !== "confirmation_required") throw error;
    const warnings = (error.details && error.details.warnings) || [];
    return new Promise((resolve) => {
      confirmDialog(title, [error.message], warnings, async () => resolve(await send(true)));
    });
  }
}

// --------------------------------------------------------------------------
// Export
// --------------------------------------------------------------------------

function setupExport() {
  const form = $("export-form");
  if (!form) return;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    hideError();
    const passphrase = $("export-passphrase").value;
    if (passphrase !== $("export-passphrase2").value) {
      showError("The passphrases do not match.");
      return;
    }
    apiFetch("/api/v1/config/export", {
      method: "POST",
      body: { passphrase: passphrase || null },
    })
      .then((documentJson) => {
        const blob = new Blob([JSON.stringify(documentJson, null, 2)], {
          type: "application/json",
        });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        const stamp = (documentJson.exported_at || "").replace(/[-:]/g, "").slice(0, 15);
        link.href = url;
        link.download = `nas-manager-config-${stamp || "export"}.json`;
        document.body.append(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
        form.reset();
        showInfo(
          passphrase
            ? "Exported with encrypted secrets. Keep the passphrase: it is needed to import them."
            : "Exported without secrets.",
        );
      })
      .catch((error) => showError(error.message));
  });
}

// --------------------------------------------------------------------------
// Import
// --------------------------------------------------------------------------

function renderPlan(plan) {
  const body = $("plan-body");
  body.replaceChildren();
  for (const [name, section] of Object.entries(plan.sections)) {
    const tr = document.createElement("tr");
    tr.append(cell(name));
    for (const key of ["create", "update", "delete"]) {
      const names = section[key] || [];
      const td = cell(names.length ? `${names.length}: ${names.slice(0, 5).join(", ")}${names.length > 5 ? "…" : ""}` : "–", "small text-break");
      tr.append(td);
    }
    tr.append(cell(String(section.unchanged || 0)));
    body.append(tr);
  }
  const warnings = [...plan.warnings];
  if (plan.samba_apply_required) {
    warnings.push("Samba shares change: apply the configuration on the Samba page afterwards.");
  }
  if (!plan.secrets_included) {
    warnings.push("The file has no secrets: passwords and credentials are not imported.");
  }
  listItems($("plan-warnings"), warnings);
  listItems(
    $("plan-errors"),
    plan.errors.map((e) => `${e.section} / ${e.key}: ${e.error}`),
  );
  $("apply-import").disabled = plan.errors.length > 0;
  $("plan").classList.remove("d-none");
}

function requestBody(confirm) {
  return {
    document: state.document,
    passphrase: state.passphrase || null,
    mode: state.mode,
    source_name: state.sourceName,
    confirm,
  };
}

function setupImport() {
  const form = $("import-form");
  if (!form) return;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    hideError();
    const file = $("import-file").files[0];
    if (!file) {
      showError("Choose an export file first.");
      return;
    }
    file
      .text()
      .then(async (text) => {
        try {
          state.document = JSON.parse(text);
        } catch (_ignored) {
          throw new Error("The file is not valid JSON.");
        }
        state.sourceName = file.name;
        state.passphrase = $("import-passphrase").value;
        state.mode = $("import-mode").value;
        const plan = await apiFetch("/api/v1/config/import/preview", {
          method: "POST",
          body: requestBody(false),
        });
        renderPlan(plan);
      })
      .catch((error) => showError(error.message));
  });
  $("apply-import").addEventListener("click", () => {
    hideError();
    withConfirmation("Apply configuration import", (confirm) =>
      apiFetch("/api/v1/config/import", { method: "POST", body: requestBody(confirm) }),
    )
      .then(async (result) => {
        $("plan").classList.add("d-none");
        form.reset();
        state.document = null;
        showInfo(
          `Import #${result.run.id} applied.${result.plan.samba_apply_required ? " Apply the Samba configuration on the Samba page." : ""}`,
        );
        await loadHistory();
      })
      .catch((error) => {
        if (error.details && error.details.errors) {
          listItems(
            $("plan-errors"),
            error.details.errors.map((e) => `${e.section} / ${e.key}: ${e.error}`),
          );
        }
        showError(error.message);
      });
  });
}

// --------------------------------------------------------------------------
// History
// --------------------------------------------------------------------------

function changeSummary(sections) {
  const parts = [];
  for (const [name, section] of Object.entries(sections)) {
    const counts = ["create", "update", "delete"]
      .map((key) => [key, (section[key] || []).length])
      .filter(([, count]) => count > 0)
      .map(([key, count]) => `${count} ${key}`);
    if (counts.length) parts.push(`${name}: ${counts.join(", ")}`);
  }
  return parts.join("; ") || "no changes";
}

async function loadHistory() {
  if (!canImport) return;
  const runs = await getJson("/api/v1/config/imports");
  const body = $("history-body");
  body.replaceChildren();
  if (runs.length === 0) {
    const tr = document.createElement("tr");
    const td = cell("No imports yet.", "text-body-secondary");
    td.colSpan = 8;
    tr.append(td);
    body.append(tr);
    return;
  }
  const latestApplied = runs.find((run) => run.status === "applied");
  for (const run of runs) {
    const tr = document.createElement("tr");
    tr.append(cell(String(run.id)));
    tr.append(cell(when(run.created_at)));
    tr.append(cell(run.actor_username));
    tr.append(cell(run.mode));
    tr.append(cell(run.source_name || "–", "small text-break"));
    tr.append(cell(changeSummary(run.sections), "small"));
    tr.append(
      cell(
        run.status === "rolled_back"
          ? `rolled back ${when(run.rolled_back_at)} by ${run.rolled_back_by}`
          : run.status,
      ),
    );
    const actions = cell("", "text-end");
    if (latestApplied && run.id === latestApplied.id) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-sm btn-outline-danger";
      btn.textContent = "Roll back";
      btn.addEventListener("click", () => {
        hideError();
        withConfirmation(`Roll back import #${run.id}`, (confirm) =>
          apiFetch(`/api/v1/config/imports/${run.id}/rollback`, {
            method: "POST",
            body: { confirm },
          }),
        )
          .then(async () => {
            showInfo(`Import #${run.id} was rolled back.`);
            await loadHistory();
          })
          .catch((error) => showError(error.message));
      });
      actions.append(btn);
    }
    tr.append(actions);
    body.append(tr);
  }
}

setupExport();
setupImport();
loadHistory().catch((error) => showError(error.message));
