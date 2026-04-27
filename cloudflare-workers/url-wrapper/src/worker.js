/**
 * Patchbay Relay URL wrapper
 *
 * Telegram does not recognize custom URL schemes (obsidian://, things://,
 * shortcuts://, etc.) as clickable links. This Cloudflare Worker wraps
 * those schemes in plain https:// URLs by serving an HTML page that
 * redirects via meta refresh and a JS fallback. From a Telegram message
 * it is a single tap to jump straight into the native app.
 *
 * Routes:
 *   /obs/<vault>/<path>   redirects to obsidian://open?vault=<vault>&file=<path>
 *   /raw/<base64url>      redirects to any custom scheme (base64url-encoded)
 *   /                     usage page
 *
 * Deploy: see README.md in this directory.
 */

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/" || path === "") {
      return htmlResponse(usagePage());
    }

    if (path.startsWith("/obs/")) {
      const rest = decodeURIComponent(path.slice(5));
      const slashIndex = rest.indexOf("/");
      if (slashIndex === -1) {
        return errorResponse("Missing file path. Format: /obs/<vault>/<path>");
      }
      const vault = rest.slice(0, slashIndex);
      const file = rest.slice(slashIndex + 1);
      const appUri = `obsidian://open?vault=${encodeURIComponent(vault)}&file=${encodeURIComponent(file)}`;
      return redirectPage(appUri, `Opening "${file}" in Obsidian`);
    }

    if (path.startsWith("/raw/")) {
      const encoded = path.slice(5);
      try {
        const appUri = atob(encoded.replace(/-/g, "+").replace(/_/g, "/"));
        return redirectPage(appUri, "Redirecting to app");
      } catch {
        return errorResponse("Invalid base64url encoding.");
      }
    }

    return errorResponse(`Unknown route: ${path}`);
  },
};

function redirectPage(appUri, message) {
  const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="0;url=${escapeAttr(appUri)}">
  <title>${escapeHtml(message)}</title>
  <style>
    body { font-family: -apple-system, system-ui, sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; background: #1a1a2e; color: #e0e0e0; }
    .card { text-align: center; padding: 2rem; max-width: 400px; }
    .spinner { width: 40px; height: 40px; border: 3px solid #333; border-top-color: #7c3aed; border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 1rem; }
    @keyframes spin { to { transform: rotate(360deg); } }
    a { color: #7c3aed; }
    .fallback { margin-top: 1.5rem; font-size: 0.85rem; color: #888; }
  </style>
</head>
<body>
  <div class="card">
    <div class="spinner"></div>
    <p>${escapeHtml(message)}</p>
    <p><a href="${escapeAttr(appUri)}">Tap here if nothing happened</a></p>
    <p class="fallback">This link opens a native app. It will not work in a desktop browser without the app installed.</p>
  </div>
  <script>window.location.href = ${JSON.stringify(appUri)};</script>
</body>
</html>`;
  return htmlResponse(html);
}

function usagePage() {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Patchbay Relay URL wrapper</title>
  <style>
    body { font-family: -apple-system, system-ui, sans-serif; max-width: 600px; margin: 2rem auto; padding: 0 1rem; background: #1a1a2e; color: #e0e0e0; }
    h1 { color: #7c3aed; }
    code { background: #2a2a3e; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }
    pre { background: #2a2a3e; padding: 1rem; border-radius: 6px; overflow-x: auto; }
    table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
    th, td { text-align: left; padding: 0.5rem; border-bottom: 1px solid #333; }
    th { color: #7c3aed; }
    a { color: #a78bfa; }
  </style>
</head>
<body>
  <h1>Patchbay Relay URL wrapper</h1>
  <p>Wraps custom URL schemes in https:// links so Telegram (and other chat apps) recognize them as tappable.</p>
  <table>
    <tr><th>Route</th><th>Opens</th></tr>
    <tr><td><code>/obs/{vault}/{path}</code></td><td>Obsidian note</td></tr>
    <tr><td><code>/raw/{base64url}</code></td><td>Any custom URL scheme</td></tr>
  </table>
  <h2>Examples</h2>
  <pre>/obs/MyVault/notes/today.md
/raw/dGhpbmdzOi8vLw   (things://)</pre>
  <p>Source: <a href="https://github.com/synodic-studio/patchbay-relay">github.com/synodic-studio/patchbay-relay</a></p>
</body>
</html>`;
}

function htmlResponse(html, status = 200) {
  return new Response(html, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

function errorResponse(msg) {
  return new Response(`Error: ${msg}`, {
    status: 400,
    headers: { "Content-Type": "text/plain" },
  });
}

export function escapeHtml(str) {
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

export function escapeAttr(str) {
  return str.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
