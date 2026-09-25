/**
 * Gmail Email Open Tracker - Content Script
 * Injects tracking controls into Gmail compose window,
 * intercepts Send, requests tracking ID from backend,
 * and appends 1x1 tracking pixel and transparency disclosure.
 */

(function () {
  let globalSettings = {
    trackingEnabled: true,
    includeDisclosure: true,
    disclosureText: "⚡ "
  };

  // Sync settings from storage/background
  function refreshSettings() {
    chrome.runtime.sendMessage({ type: "GET_SETTINGS" }, (response) => {
      if (response && response.success && response.settings) {
        globalSettings = { ...globalSettings, ...response.settings };
      }
    });
  }
  refreshSettings();

  // Re-check settings periodically
  setInterval(refreshSettings, 30000);

  // Helper to find the compose container
  function findComposeContainer(el) {
    if (!el) return null;
    return (
      el.closest('div[role="dialog"]') ||
      el.closest('.M9') ||
      el.closest('.AD') ||
      el.closest('.inboxsdk__compose') ||
      el.closest('table.iN') ||
      el.closest('div[aria-label*="Compose"]') ||
      el.closest('form') ||
      findEnclosingCompose(el)
    );
  }

  function findEnclosingCompose(el) {
    let curr = el;
    while (curr && curr !== document.body) {
      if (
        curr.querySelector &&
        curr.querySelector('div[role="textbox"]') &&
        (curr.querySelector('.aoO') || curr.querySelector('[data-tooltip*="Send"]'))
      ) {
        return curr;
      }
      curr = curr.parentElement;
    }
    return null;
  }

  function isSendButton(target) {
    if (!target) return false;
    // Exclude the "Schedule send" dropdown arrow
    if (target.closest('[aria-label*="Schedule send"], [data-tooltip*="Schedule send"], .G-as')) {
      return false;
    }

    const btn = target.closest('div[role="button"], button');
    if (!btn) return false;

    if (btn.classList.contains("aoO") || btn.classList.contains("T-I-atl")) {
      return true;
    }

    const tooltip = btn.getAttribute("data-tooltip") || "";
    const aria = btn.getAttribute("aria-label") || "";
    const text = (btn.textContent || "").trim();

    if (/^Send/i.test(tooltip) || /^Send/i.test(aria) || /^Send/i.test(text)) {
      return true;
    }

    return false;
  }

  function findSendButtonInCompose(composeBox) {
    return composeBox.querySelector(
      'div.aoO, div.T-I-atl, div[role="button"][data-tooltip*="Send"], div[role="button"][aria-label*="Send"]'
    );
  }

  function findMessageBody(composeBox) {
    return composeBox.querySelector(
      'div[role="textbox"][aria-label*="Message Body"], div[aria-label="Message Body"], div.Am.Al.editable, div[contenteditable="true"][role="textbox"]'
    );
  }

  function extractSubject(composeBox) {
    const input = composeBox.querySelector('input[name="subjectbox"], input[name="subject"]');
    if (input && input.value.trim()) {
      return input.value.trim();
    }
    // Inline replies may not have an input; search for thread title
    const threadTitle =
      document.querySelector("h2.hP") || document.querySelector("h2[data-thread-perm-id]");
    if (threadTitle && threadTitle.textContent.trim()) {
      return threadTitle.textContent.trim();
    }
    return "(no subject)";
  }

  function extractRecipients(composeBox) {
    const recipients = new Set();
    const chips = composeBox.querySelectorAll("span[email], div[data-hovercard-id], [peoplekit-id]");
    chips.forEach((chip) => {
      const email = chip.getAttribute("email") || chip.getAttribute("data-hovercard-id");
      if (email && email.includes("@")) {
        recipients.add(email);
      } else if (chip.textContent && chip.textContent.includes("@")) {
        recipients.add(chip.textContent.trim());
      }
    });

    const toInputs = composeBox.querySelectorAll('input[name="to"], div[name="to"]');
    toInputs.forEach((inp) => {
      const val = inp.value || inp.textContent;
      if (val && val.includes("@")) {
        recipients.add(val.trim());
      }
    });

    if (recipients.size === 0) {
      return "recipient";
    }
    return Array.from(recipients).join(", ");
  }

  // Intercept and inject tracking
  async function handleSendInterception(composeBox, sendButton) {
    if (!composeBox || !sendButton) return;

    // Already injected or deliberately bypassed
    if (
      composeBox.dataset.trackerInjected === "true" ||
      composeBox.dataset.trackerBypassed === "true"
    ) {
      return;
    }

    // Already processing
    if (composeBox.dataset.trackerProcessing === "true") {
      return;
    }

    // Check if tracking is disabled either for this draft or globally
    const draftTrackingEnabled =
      composeBox.dataset.trackingActive !== "false" && globalSettings.trackingEnabled !== false;

    if (!draftTrackingEnabled) {
      composeBox.dataset.trackerBypassed = "true";
      sendButton.click();
      return;
    }

    composeBox.dataset.trackerProcessing = "true";

    // Visual feedback on Send button
    const origTooltip = sendButton.getAttribute("data-tooltip") || "";
    sendButton.setAttribute("data-tooltip", "Adding tracking pixel...");
    sendButton.classList.add("tracker-btn-loading");

    const subject = extractSubject(composeBox);
    const recipients = extractRecipients(composeBox);
    const label = `${subject} -> ${recipients}`;

    try {
      const response = await new Promise((resolve) => {
        chrome.runtime.sendMessage({ type: "CREATE_TRACKER", label: label }, (res) => {
          resolve(res || { success: false, error: "No response from extension background" });
        });
      });

      if (!response || !response.success) {
        console.warn("[EmailTracker] Failed to create tracker:", response ? response.error : "Unknown error");
        showServerFallbackBanner(composeBox, sendButton, response ? response.error : "Server unreachable");
        return;
      }

      const body = findMessageBody(composeBox);
      if (!body) {
        console.warn("[EmailTracker] Message body element not found, sending directly");
        composeBox.dataset.trackerBypassed = "true";
        sendButton.click();
        return;
      }

      injectTrackingContent(body, response);

      // Successfully injected
      composeBox.dataset.trackerInjected = "true";
      composeBox.dataset.trackerProcessing = "false";
      sendButton.classList.remove("tracker-btn-loading");
      if (origTooltip) sendButton.setAttribute("data-tooltip", origTooltip);

      // Re-trigger the native Send click after allowing Gmail's draft serializer to sync the DOM
      setTimeout(() => {
        sendButton.click();
      }, 200);
    } catch (err) {
      console.error("[EmailTracker] Exception during tracking injection:", err);
      showServerFallbackBanner(composeBox, sendButton, err.message);
    }
  }

  function injectTrackingContent(body, data) {
    if (body.querySelector(`[data-email-tracker="${data.tracking_id}"]`)) {
      return;
    }

    const container = document.createElement("div");
    container.setAttribute("data-email-tracker", data.tracking_id);
    container.className = "email-tracker-container";
    container.style.cssText = "margin: 0; padding: 0; line-height: 1; font-size: 0;";

    // Invisible 1x1 tracking pixel - NEVER use display:none !important because
    // email clients and Google Image Proxy skip loading display:none images!
    const img = document.createElement("img");
    img.src = data.pixel_url;
    img.alt = "";
    img.width = 1;
    img.height = 1;
    img.setAttribute("border", "0");
    img.style.cssText =
      "width:1px !important; min-width:1px !important; max-width:1px !important; height:1px !important; min-height:1px !important; max-height:1px !important; border:0 !important; outline:none !important; margin:0 !important; padding:0 !important; opacity:0 !important; pointer-events:none !important; position:absolute !important; left:-9999px !important;";
    container.appendChild(img);

    // Visible transparency footer if configured
    if (data.include_disclosure) {
      const footer = document.createElement("div");
      footer.className = "email-tracker-disclosure";
      footer.style.cssText =
        "color: #888888; font-size: 11px; margin-top: 16px; padding-top: 6px; border-top: 1px solid #eeeeee; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.4;";
      footer.textContent = data.disclosure_text || "⚡ ";
      container.appendChild(footer);
    }

    body.appendChild(container);

    // Trigger input and blur/focus events so Gmail synchronizes its internal draft state
    body.dispatchEvent(new Event("input", { bubbles: true }));
    body.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function showServerFallbackBanner(composeBox, sendButton, errorMsg) {
    composeBox.dataset.trackerProcessing = "false";
    sendButton.classList.remove("tracker-btn-loading");

    // Remove existing banner if any
    const existing = composeBox.querySelector(".tracker-fallback-banner");
    if (existing) existing.remove();

    const banner = document.createElement("div");
    banner.className = "tracker-fallback-banner";
    banner.innerHTML = `
      <div class="tracker-banner-text">⚠️ <strong>Tracker offline:</strong> ${errorMsg}</div>
      <div class="tracker-banner-actions">
        <button type="button" class="tracker-banner-btn tracker-btn-send-now">Send Untracked</button>
        <button type="button" class="tracker-banner-btn tracker-btn-cancel">Cancel</button>
      </div>
    `;

    banner.querySelector(".tracker-btn-send-now").onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      banner.remove();
      composeBox.dataset.trackerBypassed = "true";
      sendButton.click();
    };

    banner.querySelector(".tracker-btn-cancel").onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      banner.remove();
    };

    composeBox.appendChild(banner);
  }

  // Intercept Click events in capture phase
  document.addEventListener(
    "click",
    (e) => {
      if (!isSendButton(e.target)) return;

      const composeBox = findComposeContainer(e.target);
      if (!composeBox) return;

      if (
        composeBox.dataset.trackerInjected === "true" ||
        composeBox.dataset.trackerBypassed === "true"
      ) {
        return; // Allow native send to continue
      }

      // Stop native send until tracker is inserted
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();

      const sendButton = e.target.closest('div[role="button"], button') || e.target;
      handleSendInterception(composeBox, sendButton);
    },
    true
  );

  // Intercept Ctrl+Enter / Cmd+Enter keyboard shortcut
  document.addEventListener(
    "keydown",
    (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        const composeBox = findComposeContainer(e.target);
        if (!composeBox) return;

        if (
          composeBox.dataset.trackerInjected === "true" ||
          composeBox.dataset.trackerBypassed === "true"
        ) {
          return;
        }

        const sendButton = findSendButtonInCompose(composeBox);
        if (!sendButton) return;

        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation();

        handleSendInterception(composeBox, sendButton);
      }
    },
    true
  );

  // Inject Mailtrack-style tracking toggle button into compose toolbar
  function setupComposeToolbar(composeBox) {
    if (composeBox.querySelector(".tracker-compose-toggle")) return;

    // Look for Gmail's compose bottom action bar
    const toolbar =
      composeBox.querySelector("tr.btC, div.btC, td.gU.Up, .dC, [role=\"toolbar\"]");
    if (!toolbar) return;

    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "tracker-compose-toggle is-active";
    toggleBtn.setAttribute("data-tooltip", "Email Open Tracking: ACTIVE (Click to toggle)");

    const isTracking = composeBox.dataset.trackingActive !== "false";
    updateToggleAppearance(toggleBtn, isTracking);

    toggleBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const current = composeBox.dataset.trackingActive !== "false";
      const next = !current;
      composeBox.dataset.trackingActive = next ? "true" : "false";
      updateToggleAppearance(toggleBtn, next);
    });

    // Insert near the toolbar
    if (toolbar.firstChild) {
      toolbar.insertBefore(toggleBtn, toolbar.firstChild);
    } else {
      toolbar.appendChild(toggleBtn);
    }
  }

  function updateToggleAppearance(btn, isActive) {
    if (isActive) {
      btn.className = "tracker-compose-toggle is-active";
      btn.innerHTML = `<span class="tracker-icon">👁️</span><span class="tracker-label">Tracked</span>`;
      btn.title = "Email Open Tracking is ON. Click to disable for this email.";
    } else {
      btn.className = "tracker-compose-toggle is-inactive";
      btn.innerHTML = `<span class="tracker-icon">🚫</span><span class="tracker-label">Untracked</span>`;
      btn.title = "Email Open Tracking is OFF. Click to enable for this email.";
    }
  }

  // Scan for existing compose boxes and observe new ones
  function scanComposeWindows() {
    const composeBoxes = document.querySelectorAll(
      'div[role="dialog"], .M9, .AD, div[aria-label*="Compose"]'
    );
    composeBoxes.forEach((box) => {
      if (findMessageBody(box)) {
        setupComposeToolbar(box);
      }
    });
  }

  const observer = new MutationObserver(() => {
    scanComposeWindows();
  });

  observer.observe(document.body, { childList: true, subtree: true });
  scanComposeWindows();

  console.log("[EmailTracker] Gmail Email Open Tracker content script loaded.");
})();
