/**
 * Gmail Email Open Tracker - Options Page (shared form helpers live in common.js)
 */

document.addEventListener("DOMContentLoaded", () => {
  const form = settingsFormFromPage();
  const elBtnTest = document.getElementById("btnTest");
  const elTestResult = document.getElementById("testResult");
  const elBtnSave = document.getElementById("btnSave");
  const elSaveStatus = document.getElementById("saveStatus");

  bindSettingsForm(form);

  elBtnTest.addEventListener("click", async () => {
    elTestResult.textContent = "Testing connection...";
    elTestResult.style.color = "var(--muted)";

    const res = await testSettingsForm(form);
    if (res.success && res.data) {
      elTestResult.textContent = `✓ Connected! Detected IP: ${res.data.ip} (${res.data.is_ignored ? "Self-open filter active" : "Not filtered"})`;
      elTestResult.style.color = "var(--green)";
    } else {
      elTestResult.textContent = `✗ Connection failed: ${res.error || "Unknown error"}`;
      elTestResult.style.color = "var(--red)";
    }
  });

  elBtnSave.addEventListener("click", async () => {
    const res = await saveSettingsForm(form);
    elSaveStatus.textContent = res.success ? "✓ Settings saved successfully!" : `✗ ${res.error}`;
    setTimeout(() => {
      elSaveStatus.textContent = "";
    }, 3000);
  });
});
