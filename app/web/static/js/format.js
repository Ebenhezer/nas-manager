const UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];

export function formatBytes(value) {
  let size = Number(value);
  if (!Number.isFinite(size) || size < 0) {
    return "–";
  }
  let unit = 0;
  while (size >= 1024 && unit < UNITS.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${UNITS[unit]}`;
}

export function formatDuration(totalSeconds) {
  let seconds = Math.max(0, Math.floor(Number(totalSeconds)));
  const days = Math.floor(seconds / 86400);
  seconds -= days * 86400;
  const hours = Math.floor(seconds / 3600);
  seconds -= hours * 3600;
  const minutes = Math.floor(seconds / 60);
  const parts = [];
  if (days) parts.push(`${days}d`);
  if (days || hours) parts.push(`${hours}h`);
  parts.push(`${minutes}m`);
  return parts.join(" ");
}

export function formatPercent(value) {
  return `${Number(value).toFixed(1)}%`;
}
