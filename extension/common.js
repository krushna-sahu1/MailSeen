/**
 * Shared by the background worker (importScripts), popup and options pages.
 */

const DEFAULT_BACKEND_URL = "https://mailseen.onrender.com";
const DEFAULT_DISCLOSURE_TEXT = "⚡ The sender is notified when this email is opened.";

// Host-permission match pattern for a backend URL, e.g. "https://example.com/*" (ports are not part of patterns)
function backendOriginPattern(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return null;
    return `${parsed.protocol}//${parsed.hostname}/*`;
  } catch {
    return null;
  }
}

// Only the default backend is granted at install time. Any other backend needs an optional host
// permission, and Chrome only shows that prompt from a user gesture: call this first thing in a
// click handler, before any other await. Resolves immediately when access is already granted.
function requestBackendPermission(url) {
  const pattern = backendOriginPattern(url);
  if (!pattern) return Promise.resolve(false);
  return chrome.permissions.request({ origins: [pattern] }).catch(() => false);
}

function hasBackendPermission(url) {
  const pattern = backendOriginPattern(url);
  if (!pattern) return Promise.resolve(false);
  return chrome.permissions.contains({ origins: [pattern] });
}

function normalizeBackendUrl(url) {
  return (url || "").trim().replace(/\/+$/, "") || DEFAULT_BACKEND_URL;
}

function isLocalBackendUrl(url) {
  const clean = (url || "").toLowerCase();
  return !clean || clean.includes("localhost") || clean.includes("127.0.0.1") || clean.includes("0.0.0.0");
}

// ─── Settings form (popup and options page) ─────────────────────────
// `form` holds the inputs: backendUrl, apiToken, toggleTracking, toggleDisclosure, disclosureText,
// plus disclosureGroup (shown when the disclosure is on) and localhostNotice.

function syncSettingsFormVisibility(form) {
  form.disclosureGroup.style.display = form.toggleDisclosure.checked ? "block" : "none";
  if (form.localhostNotice) {
    form.localhostNotice.style.display = isLocalBackendUrl(form.backendUrl.value) ? "block" : "none";
  }
}

function fillSettingsForm(form, settings) {
  form.backendUrl.value = settings.backendUrl || DEFAULT_BACKEND_URL;
  form.apiToken.value = settings.apiToken || "";
  form.toggleTracking.checked = settings.trackingEnabled !== false;
  form.toggleDisclosure.checked = settings.includeDisclosure !== false;
  form.disclosureText.value = settings.disclosureText || DEFAULT_DISCLOSURE_TEXT;
  syncSettingsFormVisibility(form);
}

function readSettingsForm(form) {
  return {
    backendUrl: normalizeBackendUrl(form.backendUrl.value),
    apiToken: form.apiToken.value.trim(),
    trackingEnabled: form.toggleTracking.checked,
    includeDisclosure: form.toggleDisclosure.checked,
    disclosureText: form.disclosureText.value.trim()
  };
}

function bindSettingsForm(form) {
  form.backendUrl.addEventListener("input", () => syncSettingsFormVisibility(form));
  form.toggleDisclosure.addEventListener("change", () => syncSettingsFormVisibility(form));
  chrome.runtime.sendMessage({ type: "GET_SETTINGS" }, (res) => {
    if (res && res.success && res.settings) {
      fillSettingsForm(form, res.settings);
      if (form.onLoaded) form.onLoaded(res.settings);
    }
  });
}

// Save after making sure the extension may reach the backend. Must be called from a click handler.
async function saveSettingsForm(form) {
  const settings = readSettingsForm(form);
  if (!(await requestBackendPermission(settings.backendUrl))) {
    return { success: false, error: `Access to ${settings.backendUrl} was not granted` };
  }
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: "SAVE_SETTINGS", settings }, (res) => {
      resolve(res && res.success ? { success: true, settings } : { success: false, error: "Could not save settings" });
    });
  });
}

// Test the backend in the form (unsaved values included). Must be called from a click handler,
// or with interactive=false where no permission prompt can be shown.
async function testSettingsForm(form, { interactive = true } = {}) {
  const { backendUrl, apiToken } = readSettingsForm(form);
  if (interactive && !(await requestBackendPermission(backendUrl))) {
    return { success: false, error: `Access to ${backendUrl} was not granted` };
  }
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: "TEST_CONNECTION", backendUrl, apiToken }, (res) => {
      resolve(res || { success: false, error: "Cannot reach server" });
    });
  });
}

function settingsFormFromPage() {
  const $ = (id) => document.getElementById(id);
  return {
    backendUrl: $("backendUrl"),
    apiToken: $("apiToken"),
    toggleTracking: $("toggleTracking"),
    toggleDisclosure: $("toggleDisclosure"),
    disclosureText: $("disclosureText"),
    disclosureGroup: $("disclosureGroup"),
    localhostNotice: $("localhostNotice")
  };
}
