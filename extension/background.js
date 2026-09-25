/**
 * Gmail Email Open Tracker - Background Service Worker (Manifest V3)
 * Handles cross-origin requests to the self-hosted Flask backend,
 * bypassing Gmail CSP restrictions on content scripts.
 */

const DEFAULT_SETTINGS = {
  backendUrl: "https://mailseen.onrender.com",
  trackingEnabled: true,
  includeDisclosure: true,
  disclosureText: "⚡ "
};

// Initialize default settings on installation
chrome.runtime.onInstalled.addListener(async () => {
  const current = await chrome.storage.sync.get(DEFAULT_SETTINGS);
  await chrome.storage.sync.set(current);
  console.log("[EmailTracker] Service worker installed with settings:", current);
});

async function getSettings() {
  return await chrome.storage.sync.get(DEFAULT_SETTINGS);
}

// Handle messages from content script or popup
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      const settings = await getSettings();
      const baseUrl = (settings.backendUrl || "http://localhost:5000").replace(/\/+$/, "");

      if (message.type === "GET_SETTINGS") {
        sendResponse({ success: true, settings });
        return;
      }

      if (message.type === "SAVE_SETTINGS") {
        await chrome.storage.sync.set(message.settings);
        sendResponse({ success: true });
        return;
      }

      if (message.type === "TEST_CONNECTION") {
        const testUrl = (message.backendUrl || baseUrl) + "/api/my-ip";
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 5000);

        try {
          const res = await fetch(testUrl, {
            method: "GET",
            headers: { Accept: "application/json" },
            signal: controller.signal
          });
          clearTimeout(timeoutId);

          if (!res.ok) {
            sendResponse({ success: false, error: `Server returned HTTP ${res.status}` });
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
        const targetUrl = `${baseUrl}/new`;

        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 7000);

        try {
          const res = await fetch(targetUrl, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Accept: "application/json"
            },
            body: JSON.stringify({ label }),
            signal: controller.signal
          });
          clearTimeout(timeoutId);

          if (!res.ok) {
            sendResponse({ success: false, error: `Failed to create tracker (HTTP ${res.status})` });
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

      if (message.type === "GET_IGNORED_IPS") {
        const targetUrl = `${baseUrl}/api/ignored-ips`;
        const res = await fetch(targetUrl, { headers: { Accept: "application/json" } });
        const data = await res.json();
        sendResponse({ success: true, data });
        return;
      }

      if (message.type === "TOGGLE_IGNORED_IP") {
        const targetUrl = `${baseUrl}/api/ignored-ips`;
        const res = await fetch(targetUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({
            ip: message.ip,
            action: message.action,
            label: message.label || "Chrome extension user"
          })
        });
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
