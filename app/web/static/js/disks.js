import { apiFetch } from "./api.js";
import { formatBytes } from "./format.js";

const banner = document.getElementById("error-banner");
const helperBadge = document.getElementById("helper-badge");
const disksBody = document.getElementById("disks-body");
const volumesBody = document.getElementById("volumes-body");
const raidBody = document.getElementById("raid-body");
const canWrite = Boolean(document.getElementById("mount-modal"));

function showError(message) {
  banner.textContent = message;
  banner.classList.remove("d-none");
  banner.classList.add("alert-danger");
}

function hideError() {
  banner.classList.add("d-none");
}

function cell(text, className = "") {
  const td = document.createElement("td");
  if (className) td.className = className;
  td.textContent = text;
  return td;
}

function yesNo(value) {
  return value ? "yes" : "no";
}

function badge(text, kind) {
  const span = document.createElement("span");
  span.className = `badge text-bg-${kind}`;
  span.textContent = text;
  return span;
}

function mountpointsText(mountpoints) {
  if (!Array.isArray(mountpoints) || mountpoints.length === 0) return "–";
  return mountpoints.join(", ") || "–";
}

function renderDisks(disks) {
  disksBody.replaceChildren();
  if (disks.length === 0) {
    const row = document.createElement("tr");
    row.append(cell("No physical disks discovered.", "text-body-secondary"));
    disksBody.append(row);
    return;
  }
  for (const disk of disks) {
    const row = document.createElement("tr");
    row.append(
      cell(disk.name),
      cell(disk.model || "–"),
      cell(formatBytes(disk.size_bytes)),
      cell(disk.transport || "–"),
      cell(disk.rotational == null ? "–" : yesNo(disk.rotational)),
      cell(mountpointsText(disk.mountpoints)),
    );
    if (canWrite) {
      const actions = document.createElement("td");
      actions.className = "text-end text-nowrap";
      const mountBtn = document.createElement("button");
      mountBtn.type = "button";
      mountBtn.className = "btn btn-sm btn-outline-primary ms-1";
      mountBtn.textContent = "Mount";
      mountBtn.addEventListener("click", () => openMountModal(disk));
      const umountBtn = document.createElement("button");
      umountBtn.type = "button";
      umountBtn.className = "btn btn-sm btn-outline-warning ms-1";
      umountBtn.textContent = "Unmount";
      umountBtn.addEventListener("click", () => unmountDisk(disk));
      const smartBtn = document.createElement("button");
      smartBtn.type = "button";
      smartBtn.className = "btn btn-sm btn-outline-secondary ms-1";
      smartBtn.textContent = "SMART";
      smartBtn.addEventListener("click", () => showSmart(disk));
      actions.append(mountBtn, smartBtn, umountBtn);
      row.append(actions);
    }
    disksBody.append(row);
  }
}

function renderVolumes(volumes) {
  volumesBody.replaceChildren();
  if (volumes.length === 0) {
    const row = document.createElement("tr");
    row.append(cell("No mounted volumes.", "text-body-secondary"));
    volumesBody.append(row);
    return;
  }
  for (const volume of volumes) {
    const row = document.createElement("tr");
    row.append(
      cell(volume.target),
      cell(volume.source),
      cell(volume.fs_type || "–"),
      cell(volume.options.join(",") || "–"),
      cell(volume.used_bytes == null ? "–" : formatBytes(volume.used_bytes)),
      cell(volume.avail_bytes == null ? "–" : formatBytes(volume.avail_bytes)),
    );
    volumesBody.append(row);
  }
}

function renderRaid(arrays) {
  raidBody.replaceChildren();
  if (arrays.length === 0) {
    const row = document.createElement("tr");
    row.append(cell("No RAID arrays.", "text-body-secondary"));
    raidBody.append(row);
    return;
  }
  for (const array of arrays) {
    const row = document.createElement("tr");
    const nameCell = cell(array.name);
    if (array.degraded) {
      nameCell.append(" ", badge("degraded", "danger"));
    }
    row.append(
      nameCell,
      cell(array.level || "–"),
      cell(array.state || "–"),
      cell(array.size_bytes == null ? "–" : formatBytes(array.size_bytes)),
      cell(array.active_disks == null ? "–" : `${array.active_disks} / ${array.total_disks ?? "?"}`),
      cell(array.members.map((member) => `${member.device} (${member.state})`).join(", ") || "–"),
      cell(array.sync_action || "idle"),
    );
    if (canWrite) {
      const actions = document.createElement("td");
      actions.className = "text-end";
      const scrubBtn = document.createElement("button");
      scrubBtn.type = "button";
      scrubBtn.className = "btn btn-sm btn-outline-danger ms-1";
      scrubBtn.textContent = "Scrub";
      scrubBtn.addEventListener("click", () => scrubArray(array));
      actions.append(scrubBtn);
      row.append(actions);
    }
    raidBody.append(row);
  }
}

// --------------------------------------------------------------------------
// Confirmation dialog
// --------------------------------------------------------------------------

function confirmDialog(title, planLines, warnings, action) {
  document.getElementById("confirm-modal-title").textContent = title;
  const plan = document.getElementById("confirm-modal-plan");
  plan.replaceChildren();
  for (const line of planLines) {
    const p = document.createElement("p");
    p.className = "mb-1";
    p.textContent = line;
    plan.append(p);
  }
  const warningList = document.getElementById("confirm-modal-warnings");
  warningList.replaceChildren();
  for (const warning of warnings) {
    const li = document.createElement("li");
    li.textContent = warning;
    warningList.append(li);
  }
  const accept = document.getElementById("confirm-modal-accept");
  const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById("confirm-modal"));
  accept.onclick = async () => {
    try {
      await action();
      modal.hide();
    } catch (error) {
      showError(error.message);
      modal.hide();
    }
  };
  modal.show();
}

// --------------------------------------------------------------------------
// SMART status (read-only; shown as a detail alert)
// --------------------------------------------------------------------------

async function showSmart(disk) {
  hideError();
  try {
    const smart = await apiFetch(`/api/v1/disks/${encodeURIComponent(disk.name)}/smart`);
    const lines = [`Device: ${smart.device}`, `Health: ${smart.health ?? "unknown"}`];
    if (smart.model) lines.push(`Model: ${smart.model}`);
    if (smart.serial) lines.push(`Serial: ${smart.serial}`);
    if (smart.firmware) lines.push(`Firmware: ${smart.firmware}`);
    if (smart.temperature != null) lines.push(`Temperature: ${smart.temperature} °C`);
    if (smart.capacity_bytes != null) lines.push(`Capacity: ${formatBytes(smart.capacity_bytes)}`);
    if (smart.attributes && smart.attributes.length > 0) {
      const rows = smart.attributes
        .filter((attribute) => attribute.name)
        .slice(0, 12)
        .map((attribute) => `${attribute.name}: ${attribute.raw ?? "?"}`);
      lines.push(`Attributes: ${rows.join("; ")}`);
    }
    for (const message of smart.messages || []) lines.push(message);
    if (!smart.available) lines.push(smart.error || "SMART data is not available on this device.");
    window.alert(lines.join("\n"));
  } catch (error) {
    showError(error.message);
  }
}

// --------------------------------------------------------------------------
// Mount flow: modal → dry-run preview → explicit confirmation → execute
// --------------------------------------------------------------------------

function openMountModal(disk) {
  document.getElementById("mount-device").value = disk.name;
  document.getElementById("mount-mountpoint").value = "";
  document.getElementById("mount-error").classList.add("d-none");
  bootstrap.Modal.getOrCreateInstance(document.getElementById("mount-modal")).show();
  document.getElementById("mount-mountpoint").focus();
}

async function previewMount() {
  const mountError = document.getElementById("mount-error");
  mountError.classList.add("d-none");
  const name = document.getElementById("mount-device").value;
  const payload = {
    mountpoint: document.getElementById("mount-mountpoint").value.trim(),
    fstype: document.getElementById("mount-fstype").value,
    options: document.getElementById("mount-options").value.trim() || "defaults",
  };
  if (!payload.mountpoint) {
    mountError.textContent = "Enter an absolute mount point.";
    mountError.classList.remove("d-none");
    return;
  }
  try {
    const preview = await apiFetch(`/api/v1/disks/${encodeURIComponent(name)}/mount/preview`, {
      method: "POST",
      body: payload,
    });
    bootstrap.Modal.getOrCreateInstance(document.getElementById("mount-modal")).hide();
    const lines = [
      `Mount: ${preview.device} at ${preview.mountpoint}`,
      `Filesystem: ${preview.fstype}`,
      `Options: ${preview.options}`,
    ];
    confirmDialog("Confirm mount", lines, preview.warnings, async () => {
      await apiFetch(`/api/v1/disks/${encodeURIComponent(name)}/mount`, {
        method: "POST",
        body: { ...payload, confirm: true, intent: preview.intent },
      });
      await refresh();
    });
  } catch (error) {
    mountError.textContent = error.message;
    mountError.classList.remove("d-none");
  }
}

// --------------------------------------------------------------------------
// Unmount flow
// --------------------------------------------------------------------------

async function unmountDisk(disk) {
  try {
    const preview = await apiFetch(`/api/v1/disks/${encodeURIComponent(disk.name)}/unmount/preview`, {
      method: "POST",
      body: { mountpoint: null },
    });
    const lines = [`Unmount: ${preview.device} from ${preview.mountpoint}`];
    confirmDialog("Confirm unmount", lines, preview.warnings, async () => {
      await apiFetch(`/api/v1/disks/${encodeURIComponent(disk.name)}/unmount`, {
        method: "POST",
        body: { mountpoint: preview.mountpoint, confirm: true, intent: preview.intent },
      });
      await refresh();
    });
  } catch (error) {
    showError(error.message);
  }
}

// --------------------------------------------------------------------------
// RAID scrub flow
// --------------------------------------------------------------------------

async function scrubArray(array) {
  try {
    const preview = await apiFetch(`/api/v1/storage/raid/${encodeURIComponent(array.name)}/scrub/preview`, {
      method: "POST",
      body: {},
    });
    const lines = [
      `Scrub: ${preview.name} (action: ${preview.sync_action})`,
      "Checks every block and reports mismatches; it does not write or rebuild.",
    ];
    confirmDialog("Confirm scrub", lines, preview.warnings, async () => {
      await apiFetch(`/api/v1/storage/raid/${encodeURIComponent(array.name)}/scrub`, {
        method: "POST",
        body: { confirm: true, intent: preview.intent },
      });
      await refresh();
    });
  } catch (error) {
    showError(error.message);
  }
}

function renderHelperBadge(status) {
  helperBadge.replaceChildren();
  if (status.available) {
    helperBadge.append(badge("helper online", "success"), " ", document.createTextNode(status.socket));
  } else {
    helperBadge.append(
      badge("helper offline", "danger"),
      " ",
      document.createTextNode(status.error || status.socket),
    );
  }
}

const USB_STATES = {
  mounted: ["ready", "success"],
  pending: ["mounting…", "info"],
  failed: ["could not mount", "danger"],
  unsupported: ["not supported", "warning"],
  ignored: ["left alone", "secondary"],
};

function renderUsb(status, removable) {
  const automount = document.getElementById("usb-automount");
  automount.replaceChildren();
  if (!status.available) {
    automount.append(badge("helper offline: no automatic mounting", "danger"));
  } else if (status.removable_automount === false) {
    automount.append(badge("automatic mounting is off", "secondary"));
  } else if (status.removable_automount) {
    automount.append(badge("automatic", "success"), " ", document.createTextNode(`mounted under ${status.removable_mount_dir}`));
  } else {
    automount.append(badge("helper too old: re-run the installer and restart nas-manager-helper", "warning"));
  }
  const list = document.getElementById("usb-list");
  list.replaceChildren();
  const devices = removable ? removable.devices : [];
  if (!devices.length) {
    const item = document.createElement("li");
    item.className = "text-body-secondary small";
    item.textContent = removable ? "No USB drive is plugged in." : "USB drive details are not available.";
    list.append(item);
  }
  for (const device of devices) {
    const item = document.createElement("li");
    item.className = "mb-2";
    const head = document.createElement("div");
    head.className = "d-flex flex-wrap gap-2 align-items-center";
    const name = document.createElement("strong");
    name.textContent = device.label || device.name;
    const [label, kind] = USB_STATES[device.state] || [device.state, "secondary"];
    head.append(
      name,
      document.createTextNode(`${formatBytes(device.size_bytes)} · ${device.fstype || "unknown file system"} · ${device.device}`),
      badge(label, kind),
    );
    item.append(head);
    const detail = document.createElement("div");
    detail.className = "small text-body-secondary text-break";
    if (device.state === "mounted" && device.mountpoint) detail.textContent = `Open it on the Files page (${device.mountpoint}).`;
    else if (device.reason) detail.textContent = device.reason;
    if (detail.textContent) item.append(detail);
    list.append(item);
  }
  const retry = document.getElementById("usb-retry");
  if (retry) retry.classList.toggle("d-none", !(status.available && status.removable_automount));
}

async function retryUsb() {
  const button = document.getElementById("usb-retry");
  button.disabled = true;
  try {
    await apiFetch("/api/v1/disks/removable/rescan", { method: "POST" });
    await refresh();
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
  }
}

async function refresh() {
  hideError();
  try {
    // The status never fails, so the USB card can say "helper offline" even
    // when everything else below does.
    const status = await apiFetch("/api/v1/disks/status");
    let removable = null;
    if (status.available) {
      try {
        removable = await apiFetch("/api/v1/disks/removable");
      } catch (_ignored) {
        removable = null; // an older helper without removable.devices
      }
    }
    renderUsb(status, removable);
  } catch (_ignored) {
    // reported below
  }
  try {
    const [status, disks, volumes, raid] = await Promise.all([
      apiFetch("/api/v1/disks/status"),
      apiFetch("/api/v1/disks"),
      apiFetch("/api/v1/storage/volumes"),
      apiFetch("/api/v1/storage/raid"),
    ]);
    renderHelperBadge(status);
    renderDisks(disks);
    renderVolumes(volumes);
    renderRaid(raid.arrays);
  } catch (error) {
    showError(`Could not load storage data: ${error.message}`);
  }
}

document.getElementById("mount-preview-btn")?.addEventListener("click", previewMount);
document.getElementById("usb-retry")?.addEventListener("click", retryUsb);
refresh();
// USB drives come and go: refresh while the page is visible and no dialog is open.
window.setInterval(() => {
  if (!document.hidden && !document.querySelector(".modal.show")) refresh();
}, 10000);
