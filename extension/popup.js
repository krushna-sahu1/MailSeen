/**
 * Gmail Email Open Tracker - Popup Logic (shared form helpers live in common.js)
 */

document.addEventListener("DOMContentLoaded", () => {
  const form = settingsFormFromPage();
  const elBtnTest = document.getElementById("btnTest");
  const elTestMsg = document.getElementById("testMessage");
  const elStatusPill = document.getElementById("connectionStatus");
  const elCurrentIp = document.getElementById("currentIp");
  const elIpFilterStatus = document.getElementById("ipFilterStatus");
  const elBtnToggleIp = document.getElementById("btnToggleIp");
  const elBtnSave = document.getElementById("btnSave");
  const elBtnOpenDashboard = document.getElementById("btnOpenDashboard");
  const elSaveFeedback = document.getElementById("saveFeedback");

  let detectedIp = "";
  let isIpFiltered = false;

  // On open, check the saved backend without prompting (no user gesture yet)
  form.onLoaded = () => checkConnection({ interactive: false });
  bindSettingsForm(form);

  async function checkConnection(options) {
    setStatus("checking", "Checking...");
    elTestMsg.textContent = "";
    elTestMsg.className = "helper-text";

    const res = await testSettingsForm(form, options);
    if (res.success && res.data) {
      setStatus("connected", "Connected");
      elTestMsg.textContent = "Backend reachable!";
      elTestMsg.className = "helper-text success";

      detectedIp = res.data.ip || "--";
      isIpFiltered = !!res.data.is_ignored;
      updateIpFilterUI();
    } else {
      setStatus("disconnected", "Offline");
      elTestMsg.textContent = res.error || "Cannot reach server";
      elTestMsg.className = "helper-text error";
      elCurrentIp.textContent = "--";
      elIpFilterStatus.textContent = "Unavailable";
      elIpFilterStatus.className = "ip-filter-badge";
    }
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

  elBtnTest.addEventListener("click", () => checkConnection({ interactive: true }));

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

  elBtnSave.addEventListener("click", async () => {
    const res = await saveSettingsForm(form);
    elSaveFeedback.textContent = res.success ? "✓ Settings saved!" : `✗ ${res.error}`;
    setTimeout(() => {
      elSaveFeedback.textContent = "";
    }, 2500);
    if (res.success) checkConnection({ interactive: false });
  });

  elBtnOpenDashboard.addEventListener("click", () => {
    chrome.tabs.create({ url: normalizeBackendUrl(form.backendUrl.value) + "/dashboard" });
  });
});
