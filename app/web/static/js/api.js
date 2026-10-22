/**
 * JSON helpers for the REST API.
 * Cookies carry the session and the CSRF token; cookies are sent automatically,
 * but the CSRF token must be echoed in X-CSRF-Token on every unsafe request.
 */

export function getCsrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  // Browsers also have the raw token in the nas_csrf cookie (not HttpOnly).
  const cookie = document.cookie
    .split("; ")
    .find((part) => part.startsWith("nas_csrf="));
  return meta ? meta.content : cookie ? decodeURIComponent(cookie.split("=").slice(1).join("=")) : "";
}

async function parseError(status, body) {
  if (body && body.error && body.error.message) {
    return `${body.error.message}${body.error.details && body.error.details.permission ? ` (${body.error.details.permission})` : ""}`;
  }
  return `${status} ${body && body.error ? body.error.code : "request failed"}`;
}

export function getJson(path, { allowStatuses = [] } = {}) {
  return apiFetch(path, { method: "GET", allowStatuses });
}

export function apiFetch(path, { method = "GET", body, allowStatuses = [] } = {}) {
  const headers = {};
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = getCsrfToken();
    body = JSON.stringify(body);
  }
  if (method !== "GET" && method !== "HEAD") {
    headers["X-CSRF-Token"] = getCsrfToken();
  }
  const options = { method, headers };
  if (body !== undefined) options.body = body;
  return fetch(path, options).then(async (response) => {
    const text = await response.text();
    let parsed = null;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch (_ignored) {
      parsed = null;
    }
    if (!response.ok && !allowStatuses.includes(response.status)) {
      throw new Error(await parseError(response.status, parsed));
    }
    return parsed;
  });
}
