/**
 * Gmail Email Open Tracker - Popup Logic
 */

document.addEventListener("DOMContentLoaded", async () => {
  const elBackendUrl = document.getElementById("backendUrl");
  const elBtnTest = document.getElementById("btnTest");
  const elTestMsg = document.getElementById("testMessage");
  const elToggleTracking = document.getElementById("toggleTracking");
  const elToggleDisclosure = document.getElementById("toggleDisclosure");
  const elDisclosureText = document.getElementById("disclosureText");
  const elDisclosureContainer = document.getElementById("disclosureTextContainer");
  const elStatusPill = document.getElementById("connectionStatus");
  const elCurrentIp = document.getElementById("currentIp");
  const elIpFilterStatus = document.getElementById("ipFilterStatus");
  const elBtnToggleIp = document.getElementById("btnToggleIp");
  const elBtnSave = document.getElementById("btnSave");
  const elBtnOpenDashboard = document.getElementById("btnOpenDashboard");
  const elSaveFeedback = document.getElementById("saveFeedback");

  let detectedIp = "";
  let isIpFiltered = false;

  // Load settings
  chrome.runtime.sendMessage({ type: "GET_SETTINGS" }, (res) => {
    if (res && res.success && res.settings) {
      const s = res.settings;
      elBackendUrl.value = s.backendUrl || "https://mailseen.onrender.com";
      elToggleTracking.checked = s.trackingEnabled !== false;
      elToggleDisclosure.checked = s.includeDisclosure !== false;
      elDisclosureText.value = s.disclosureText || "⚡ ";

      elDisclosureContainer.style.display = elToggleDisclosure.checked ? "block" : "none";

      checkLocalhostNotice(elBackendUrl.value);
      checkConnection(elBackendUrl.value);
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
    elDisclosureContainer.style.display = elToggleDisclosure.checked ? "block" : "none";
  });

  async function checkConnection(url) {
    checkLocalhostNotice(url);
    setStatus("checking", "Checking...");
    elTestMsg.textContent = "";
    elTestMsg.className = "helper-text";

    chrome.runtime.sendMessage({ type: "TEST_CONNECTION", backendUrl: url }, (res) => {
      if (res && res.success && res.data) {
        setStatus("connected", "Connected");
        elTestMsg.textContent = "Backend reachable!";
        elTestMsg.className = "helper-text success";

        detectedIp = res.data.ip || "--";
        isIpFiltered = !!res.data.is_ignored;
        updateIpFilterUI();
      } else {
        setStatus("disconnected", "Offline");
        elTestMsg.textContent = res ? res.error : "Cannot reach server";
        elTestMsg.className = "helper-text error";
        elCurrentIp.textContent = "--";
        elIpFilterStatus.textContent = "Unavailable";
        elIpFilterStatus.className = "ip-filter-badge";
      }
    });
  }

  function setStatus(type, text) {
    elStatusPill.className = `status-pill status-${type}`;
    elStatusPill.querySelector(".status-text").textContent = text;
  }

  function updateIpFilterUI() {
    elCurrentIp.textContent = detectedIp;
    if (isIpFiltered) {
      elIpFilterStatus.textContent = "🛡️ Filtered (Opens ignored)";
      elIpFilterStatus.className = "ip-filter-badge active";
      elBtnToggleIp.textContent = "Unfilter";
      elBtnToggleIp.className = "btn btn-sm btn-outline";
    } else {
      elIpFilterStatus.textContent = "⚠️ Not filtered (Will log your opens)";
      elIpFilterStatus.className = "ip-filter-badge inactive";
      elBtnToggleIp.textContent = "Filter My IP";
      elBtnToggleIp.className = "btn btn-sm btn-secondary";
    }
  }

  elBtnTest.addEventListener("click", () => {
    checkConnection(elBackendUrl.value.trim());
  });

  elBtnToggleIp.addEventListener("click", () => {
    if (!detectedIp || detectedIp === "--") return;

    const action = isIpFiltered ? "remove" : "add";
    chrome.runtime.sendMessage(
      { type: "TOGGLE_IGNORED_IP", ip: detectedIp, action: action, label: "Chrome Extension User" },
      (res) => {
        if (res && res.success) {
          isIpFiltered = !isIpFiltered;
          updateIpFilterUI();
        } else {
          alert("Could not update IP filter: " + (res ? res.error : "Unknown error"));
        }
      }
    );
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
        elSaveFeedback.textContent = "✓ Settings saved!";
        setTimeout(() => {
          elSaveFeedback.textContent = "";
        }, 2500);
        checkConnection(newSettings.backendUrl);
      }
    });
  });

  elBtnOpenDashboard.addEventListener("click", () => {
    const url = (elBackendUrl.value.trim() || "https://mailseen.onrender.com").replace(/\/+$/, "") + "/dashboard";
    chrome.tabs.create({ url: url });
  });
});
