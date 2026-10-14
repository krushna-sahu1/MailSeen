/**
 * Gmail Email Open Tracker - Background Service Worker (Manifest V3)
 * Handles cross-origin requests to the self-hosted Flask backend,
 * bypassing Gmail CSP restrictions on content scripts.
 */

importScripts("common.js");

const DEFAULT_SETTINGS = {
  backendUrl: DEFAULT_BACKEND_URL,
  trackingEnabled: true,
  includeDisclosure: true,
  disclosureText: DEFAULT_DISCLOSURE_TEXT
};

// Earlier versions defaulted to a bare emoji, which told recipients nothing. Replace that exact
// value on update; any text the user wrote themselves is kept.
const LEGACY_DISCLOSURE_DEFAULTS = ["⚡ ", "⚡", ""];

// Initialize default settings on install and update
chrome.runtime.onInstalled.addListener(async () => {
  const current = await chrome.storage.sync.get(DEFAULT_SETTINGS);
  if (LEGACY_DISCLOSURE_DEFAULTS.includes(current.disclosureText)) {
    current.disclosureText = DEFAULT_SETTINGS.disclosureText;
  }
  await chrome.storage.sync.set(current);
  console.log("[EmailTracker] Service worker installed with settings:", current);
});

// The API token stays in local (not synced) storage and is never handed to the Gmail content script.
const DEFAULT_LOCAL_SETTINGS = {
  apiToken: ""
};

async function getSettings() {
  const [synced, local] = await Promise.all([
    chrome.storage.sync.get(DEFAULT_SETTINGS),
    chrome.storage.local.get(DEFAULT_LOCAL_SETTINGS)
  ]);
  return { ...synced, ...local };
}

function authHeaders(apiToken, extra = {}) {
  const headers = { Accept: "application/json", ...extra };
  if (apiToken) headers.Authorization = `Bearer ${apiToken}`;
  return headers;
}

function httpErrorMessage(res, fallback) {
  if (res.status === 401) return "Unauthorized: set a valid API token in the extension popup";
  return fallback;
}

async function missingPermissionError(baseUrl) {
  if (await hasBackendPermission(baseUrl)) return null;
  return `No permission to reach ${new URL(baseUrl).origin}. Open the extension popup and click Save to grant access.`;
}

// Handle messages from content script or popup
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      const settings = await getSettings();
      const baseUrl = normalizeBackendUrl(settings.backendUrl);

      if (message.type === "GET_SETTINGS") {
        const { apiToken, ...publicSettings } = settings;
        // Content scripts (sender.tab set) only learn whether a token exists
        sendResponse({
          success: true,
          settings: sender.tab ? { ...publicSettings, hasApiToken: !!apiToken } : settings
        });
        return;
      }

      if (message.type === "SAVE_SETTINGS") {
        const { apiToken, ...syncedSettings } = message.settings;
        await chrome.storage.sync.set(syncedSettings);
        if (apiToken !== undefined) {
          await chrome.storage.local.set({ apiToken });
        }
        sendResponse({ success: true });
        return;
      }

      // Every message below talks to the backend
      const targetBaseUrl =
        message.type === "TEST_CONNECTION" && message.backendUrl ? normalizeBackendUrl(message.backendUrl) : baseUrl;
      const noAccess = await missingPermissionError(targetBaseUrl);
      if (noAccess) {
        sendResponse({ success: false, error: noAccess });
        return;
      }

      if (message.type === "TEST_CONNECTION") {
        const testUrl = targetBaseUrl + "/api/my-ip";
        const apiToken = message.apiToken !== undefined ? message.apiToken : settings.apiToken;
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 5000);

        try {
          const res = await fetch(testUrl, {
            method: "GET",
            headers: authHeaders(apiToken),
            signal: controller.signal
          });
          clearTimeout(timeoutId);

          if (!res.ok) {
            sendResponse({ success: false, error: httpErrorMessage(res, `Server returned HTTP ${res.status}`) });
            return;
          }

          const data = await res.json();
          sendResponse({ success: true, data });
        } catch (err) {
          clearTimeout(timeoutId);
          sendResponse({
            success: false,
            error: err.name === "AbortError" ? "Connection timed out (5s)" : err.message
          });
        }
        return;
      }

      if (message.type === "CREATE_TRACKER") {
        const label = message.label || "Gmail message";
        const attachment_ids = message.attachment_ids || [];
        const targetUrl = `${baseUrl}/new`;

        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 7000);

        try {
          const res = await fetch(targetUrl, {
            method: "POST",
            headers: authHeaders(settings.apiToken, { "Content-Type": "application/json" }),
            body: JSON.stringify({ label, subject: message.subject, recipients: message.recipients, attachment_ids }),
            signal: controller.signal
          });
          clearTimeout(timeoutId);

          if (!res.ok) {
            sendResponse({
              success: false,
              error: httpErrorMessage(res, `Failed to create tracker (HTTP ${res.status})`)
            });
            return;
          }

          const data = await res.json();
          sendResponse({
            success: true,
            tracking_id: data.tracking_id,
            pixel_url: data.pixel_url,
            html_snippet: data.html_snippet,
            disclosure_html: data.disclosure_html,
            disclosure_text: settings.disclosureText || data.disclosure_text,
            include_disclosure: settings.includeDisclosure,
            combined_snippet: data.combined_snippet
          });
        } catch (err) {
          clearTimeout(timeoutId);
          sendResponse({
            success: false,
            error: err.name === "AbortError" ? "Backend request timed out" : err.message
          });
        }
        return;
      }

      // ─── Attachment Upload ────────────────────────────────────────
      if (message.type === "UPLOAD_ATTACHMENT") {
        const targetUrl = `${baseUrl}/api/upload-attachment`;

        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 30000); // 30s for large files

        try {
          // Extension messages are JSON, so the file arrives as base64. Decoding through a data: URL is
          // native code, unlike the old per-byte JS loop. The server ignores this type anyway.
          const dataUrl = `data:application/octet-stream;base64,${message.fileData}`;
          const blob = await (await fetch(dataUrl)).blob();

          const formData = new FormData();
          formData.append("file", blob, message.fileName);
          formData.append("label", message.label || message.fileName);

          const res = await fetch(targetUrl, {
            method: "POST",
            headers: authHeaders(settings.apiToken),
            body: formData,
            signal: controller.signal
          });
          clearTimeout(timeoutId);

          if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            sendResponse({
              success: false,
              error: httpErrorMessage(res, errData.error || `Upload failed (HTTP ${res.status})`)
            });
            return;
          }

          const data = await res.json();
          sendResponse({
            success: true,
            attachment_id: data.attachment_id,
            filename: data.filename,
            file_size: data.file_size,
            file_size_display: data.file_size_display,
            view_url: data.view_url,
            file_url: data.file_url
          });
        } catch (err) {
          clearTimeout(timeoutId);
          sendResponse({
            success: false,
            error: err.name === "AbortError" ? "Upload timed out (30s)" : err.message
          });
        }
        return;
      }

      if (message.type === "SELF_VIEW") {
        const res = await fetch(`${baseUrl}/api/self-view`, {
          method: "POST",
          headers: authHeaders(settings.apiToken, { "Content-Type": "application/json" }),
          body: JSON.stringify({ tracking_ids: message.tracking_ids || [] })
        });
        sendResponse({ success: res.ok });
        return;
      }

      if (message.type === "GET_IGNORED_IPS") {
        const targetUrl = `${baseUrl}/api/ignored-ips`;
        const res = await fetch(targetUrl, { headers: authHeaders(settings.apiToken) });
        if (!res.ok) {
          sendResponse({ success: false, error: httpErrorMessage(res, `HTTP ${res.status}`) });
          return;
        }
        const data = await res.json();
        sendResponse({ success: true, data });
        return;
      }

      if (message.type === "TOGGLE_IGNORED_IP") {
        const targetUrl = `${baseUrl}/api/ignored-ips`;
        const res = await fetch(targetUrl, {
          method: "POST",
          headers: authHeaders(settings.apiToken, { "Content-Type": "application/json" }),
          body: JSON.stringify({
            ip: message.ip,
            action: message.action,
            label: message.label || "Chrome extension user"
          })
        });
        if (!res.ok) {
          sendResponse({ success: false, error: httpErrorMessage(res, `HTTP ${res.status}`) });
          return;
        }
        const data = await res.json();
        sendResponse({ success: true, data });
        return;
      }

      sendResponse({ success: false, error: "Unknown message type: " + message.type });
    } catch (err) {
      console.error("[EmailTracker] Background error:", err);
      sendResponse({ success: false, error: err.message });
    }
  })();

  return true; // Keep message channel open for async sendResponse
});
