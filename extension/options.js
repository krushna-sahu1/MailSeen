document.addEventListener("DOMContentLoaded", () => {
  const elBackendUrl = document.getElementById("backendUrl");
  const elBtnTest = document.getElementById("btnTest");
  const elTestResult = document.getElementById("testResult");
  const elToggleTracking = document.getElementById("toggleTracking");
  const elToggleDisclosure = document.getElementById("toggleDisclosure");
  const elDisclosureText = document.getElementById("disclosureText");
  const elDisclosureGroup = document.getElementById("disclosureGroup");
  const elBtnSave = document.getElementById("btnSave");
  const elSaveStatus = document.getElementById("saveStatus");

  // Load settings
  chrome.runtime.sendMessage({ type: "GET_SETTINGS" }, (res) => {
    if (res && res.success && res.settings) {
      const s = res.settings;
      elBackendUrl.value = s.backendUrl || "https://mailseen.onrender.com";
      elToggleTracking.checked = s.trackingEnabled !== false;
      elToggleDisclosure.checked = s.includeDisclosure !== false;
      elDisclosureText.value = s.disclosureText || "⚡ ";
      elDisclosureGroup.style.display = elToggleDisclosure.checked ? "block" : "none";
      checkLocalhostNotice(elBackendUrl.value);
    }
  });

  function checkLocalhostNotice(url) {
    const elNotice = document.getElementById("localhostNotice");
    if (!elNotice) return;
    const clean = (url || "").toLowerCase();
    const isLocal = !clean || clean.includes("localhost") || clean.includes("127.0.0.1") || clean.includes("0.0.0.0");
    elNotice.style.display = isLocal ? "block" : "none";
  }

  elBackendUrl.addEventListener("input", () => {
    checkLocalhostNotice(elBackendUrl.value);
  });

  elToggleDisclosure.addEventListener("change", () => {
    elDisclosureGroup.style.display = elToggleDisclosure.checked ? "block" : "none";
  });

  elBtnTest.addEventListener("click", () => {
    const url = elBackendUrl.value.trim().replace(/\/+$/, "");
    elTestResult.textContent = "Testing connection...";
    elTestResult.style.color = "var(--muted)";

    chrome.runtime.sendMessage({ type: "TEST_CONNECTION", backendUrl: url }, (res) => {
      if (res && res.success && res.data) {
        elTestResult.textContent = `✓ Connected! Detected IP: ${res.data.ip} (${res.data.is_ignored ? "Self-open filter active" : "Not filtered"})`;
        elTestResult.style.color = "var(--green)";
      } else {
        elTestResult.textContent = `✗ Connection failed: ${res ? res.error : "Unknown error"}`;
        elTestResult.style.color = "var(--red)";
      }
    });
  });

  elBtnSave.addEventListener("click", () => {
    const newSettings = {
      backendUrl: elBackendUrl.value.trim().replace(/\/+$/, ""),
      trackingEnabled: elToggleTracking.checked,
      includeDisclosure: elToggleDisclosure.checked,
      disclosureText: elDisclosureText.value.trim()
    };

    chrome.runtime.sendMessage({ type: "SAVE_SETTINGS", settings: newSettings }, (res) => {
      if (res && res.success) {
        elSaveStatus.textContent = "✓ Settings saved successfully!";
        setTimeout(() => {
          elSaveStatus.textContent = "";
        }, 3000);
      }
    });
  });
});
