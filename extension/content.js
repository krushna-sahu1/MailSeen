/**
 * Gmail Email Open Tracker - Content Script
 * Injects tracking controls into Gmail compose window,
 * intercepts Send, requests tracking ID from backend,
 * and appends 1x1 tracking pixel and transparency disclosure.
 *
 * Also provides "Track Attachment" button for PDF/document tracking.
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

  // ─── Compose Container Detection ───────────────────────────────────

  // Helper to find the compose or inline reply container
  function findComposeContainer(el) {
    if (!el) return null;
    return (
      el.closest('div[role="dialog"]') ||
      el.closest('.M9') ||
      el.closest('.AD') ||
      el.closest('.ip.adp') ||
      el.closest('.adn.ads') ||
      el.closest('.gA.gt') ||
      el.closest('table.cf.An') ||
      el.closest('.aoP') ||
      el.closest('.I5') ||
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

  // ─── Tracked Attachment State ───────────────────────────────────────

  // Each compose box maintains its own list of tracked attachments
  function getTrackedAttachments(composeBox) {
    if (!composeBox._trackedAttachments) {
      composeBox._trackedAttachments = [];
    }
    return composeBox._trackedAttachments;
  }

  // ─── Send Interception ─────────────────────────────────────────────

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

    // Collect attachment IDs for linking
    const attachmentIds = getTrackedAttachments(composeBox).map(a => a.attachment_id);

    try {
      const response = await new Promise((resolve) => {
        chrome.runtime.sendMessage(
          { type: "CREATE_TRACKER", label: label, attachment_ids: attachmentIds },
          (res) => {
            resolve(res || { success: false, error: "No response from extension background" });
          }
        );
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

  // ─── Attachment Tracking UI ─────────────────────────────────────────

  function createAttachmentCard(body, attachmentData) {
    /**
     * Insert a styled "View Document" card into the email body.
     * Uses all inline styles for maximum email client compatibility.
     */
    const existingCard = body.querySelector(`[data-tracked-attachment="${attachmentData.attachment_id}"]`);
    if (existingCard) return;

    const card = document.createElement("div");
    card.setAttribute("data-tracked-attachment", attachmentData.attachment_id);
    card.style.cssText =
      "margin: 16px 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;";

    card.innerHTML = `
      <table cellpadding="0" cellspacing="0" border="0" style="border: 1px solid #e0e0e0; border-radius: 10px; overflow: hidden; width: 100%; max-width: 420px; background: #ffffff; box-shadow: 0 1px 4px rgba(0,0,0,0.06);">
        <tr>
          <td style="padding: 16px 18px; vertical-align: middle;">
            <table cellpadding="0" cellspacing="0" border="0" style="width: 100%;">
              <tr>
                <td style="width: 40px; vertical-align: top;">
                  <div style="width: 38px; height: 38px; background: linear-gradient(135deg, #ef4444, #f97316); border-radius: 8px; text-align: center; line-height: 38px; font-size: 18px;">📄</div>
                </td>
                <td style="padding-left: 12px; vertical-align: middle;">
                  <div style="font-size: 14px; font-weight: 600; color: #1a1a1a; line-height: 1.3; margin-bottom: 2px;">${attachmentData.filename}</div>
                  <div style="font-size: 12px; color: #888888;">${attachmentData.file_size_display}</div>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding: 0 18px 16px;">
            <a href="${attachmentData.view_url}" target="_blank" style="display: inline-block; padding: 9px 22px; background: linear-gradient(135deg, #4f46e5, #6366f1); color: #ffffff; text-decoration: none; border-radius: 7px; font-size: 13px; font-weight: 600; letter-spacing: 0.01em;">📥 View Document</a>
          </td>
        </tr>
        <tr>
          <td style="padding: 8px 18px; background: #f9fafb; border-top: 1px solid #f0f0f0;">
            <div style="font-size: 10px; color: #b0b0b0; letter-spacing: 0.02em;">Shared securely via MailSeen</div>
          </td>
        </tr>
      </table>
    `;

    // Insert before the tracking pixel container if it exists, otherwise at the end
    const trackerContainer = body.querySelector(".email-tracker-container");
    if (trackerContainer) {
      body.insertBefore(card, trackerContainer);
    } else {
      body.appendChild(card);
    }

    // Trigger Gmail's draft sync
    body.dispatchEvent(new Event("input", { bubbles: true }));
    body.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function handleAttachmentUpload(composeBox) {
    const body = findMessageBody(composeBox);
    if (!body) {
      console.warn("[EmailTracker] Cannot find message body for attachment insertion");
      return;
    }

    // Create a hidden file input
    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.accept = ".pdf,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.txt,.csv,.png,.jpg,.jpeg,.gif,.svg,.zip";
    fileInput.style.display = "none";
    document.body.appendChild(fileInput);

    fileInput.addEventListener("change", async (e) => {
      const file = fileInput.files[0];
      if (!file) {
        fileInput.remove();
        return;
      }

      // Show upload progress indicator in compose box
      const progressEl = showUploadProgress(composeBox, file.name);

      try {
        // Read file as base64
        const base64Data = await readFileAsBase64(file);

        const subject = extractSubject(composeBox);
        const recipients = extractRecipients(composeBox);
        const label = `${file.name} (${subject} -> ${recipients})`;

        // Send to background script for upload
        const response = await new Promise((resolve) => {
          chrome.runtime.sendMessage(
            {
              type: "UPLOAD_ATTACHMENT",
              fileData: base64Data,
              fileName: file.name,
              fileType: file.type || "application/octet-stream",
              label: label,
            },
            (res) => {
              resolve(res || { success: false, error: "No response from background" });
            }
          );
        });

        if (response && response.success) {
          // Store attachment info for linking on send
          getTrackedAttachments(composeBox).push({
            attachment_id: response.attachment_id,
            filename: response.filename,
            file_size_display: response.file_size_display,
            view_url: response.view_url,
          });

          // Insert styled card into email body
          createAttachmentCard(body, response);

          // Update progress to success
          updateUploadProgress(progressEl, "success", `✓ ${response.filename} tracked`);

          // Update the attachment count badge
          updateAttachmentBadge(composeBox);
        } else {
          updateUploadProgress(progressEl, "error", `✗ Upload failed: ${response ? response.error : "Unknown"}`);
        }
      } catch (err) {
        console.error("[EmailTracker] Attachment upload error:", err);
        updateUploadProgress(progressEl, "error", `✗ Error: ${err.message}`);
      }

      fileInput.remove();
    });

    fileInput.click();
  }

  function readFileAsBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        // result is "data:<mime>;base64,<data>" — extract just the base64 part
        const base64 = reader.result.split(",")[1];
        resolve(base64);
      };
      reader.onerror = () => reject(new Error("Failed to read file"));
      reader.readAsDataURL(file);
    });
  }

  function showUploadProgress(composeBox, fileName) {
    // Remove existing progress bars
    const existing = composeBox.querySelector(".tracker-upload-progress");
    if (existing) existing.remove();

    const el = document.createElement("div");
    el.className = "tracker-upload-progress";
    el.innerHTML = `
      <div class="tracker-upload-spinner"></div>
      <span class="tracker-upload-text">Uploading ${fileName}...</span>
    `;

    // Place it near the bottom of the compose box
    const body = findMessageBody(composeBox);
    if (body && body.parentElement) {
      body.parentElement.insertBefore(el, body.nextSibling);
    } else {
      composeBox.appendChild(el);
    }

    return el;
  }

  function updateUploadProgress(el, status, message) {
    if (!el) return;
    el.className = `tracker-upload-progress tracker-upload-${status}`;
    el.innerHTML = `<span class="tracker-upload-text">${message}</span>`;

    // Auto-remove after a delay
    setTimeout(() => {
      if (el.parentNode) {
        el.style.opacity = "0";
        el.style.transform = "translateY(-4px)";
        setTimeout(() => el.remove(), 200);
      }
    }, 3000);
  }

  // ─── Compose Toolbar Injection ─────────────────────────────────────

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

  // Inject Mailtrack-style tracking toggle button AND attachment button into compose toolbar
  function setupComposeToolbar(composeBox) {
    if (!composeBox || composeBox.querySelector(".tracker-compose-toggle")) return;

    // Find the Send button in this compose/reply box
    const sendButton = findSendButtonInCompose(composeBox);
    if (!sendButton) return;

    // Find the Send button's wrapper (including the schedule send arrow)
    const sendGroup =
      sendButton.closest('.Up, .dC, tr.btC, div.btC')?.querySelector('.T-I-atl, .aoO')?.parentElement ||
      sendButton.parentElement;

    // ─── Tracking Toggle Button ────────────────────
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

    // ─── Attach Tracked File Button ────────────────
    const attachBtn = document.createElement("button");
    attachBtn.type = "button";
    attachBtn.className = "tracker-compose-attach";
    attachBtn.title = "Attach a tracked document (PDF, etc.) — recipient views will be tracked";
    attachBtn.innerHTML = `<span class="tracker-icon">📎</span><span class="tracker-label">Track PDF</span>`;

    attachBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      handleAttachmentUpload(composeBox);
    });

    // ─── Attachment count badge ────────────────────
    const badge = document.createElement("span");
    badge.className = "tracker-attach-badge";
    badge.style.display = "none";
    attachBtn.appendChild(badge);

    // Place buttons next to the Send button group
    const targetParent = sendGroup && sendGroup.parentNode ? sendGroup.parentNode : null;
    if (targetParent) {
      const insertAfter = sendGroup.nextSibling;
      if (insertAfter) {
        targetParent.insertBefore(attachBtn, insertAfter);
        targetParent.insertBefore(toggleBtn, insertAfter);
      } else {
        targetParent.appendChild(toggleBtn);
        targetParent.appendChild(attachBtn);
      }
    } else {
      // Fallback: look for action bar
      const toolbar = composeBox.querySelector('tr.btC, div.btC, td.gU.Up, .dC, [role="toolbar"]');
      if (toolbar) {
        if (toolbar.firstChild) {
          toolbar.insertBefore(attachBtn, toolbar.firstChild);
          toolbar.insertBefore(toggleBtn, toolbar.firstChild);
        } else {
          toolbar.appendChild(toggleBtn);
          toolbar.appendChild(attachBtn);
        }
      }
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

  function updateAttachmentBadge(composeBox) {
    const badge = composeBox.querySelector(".tracker-attach-badge");
    if (!badge) return;
    const count = getTrackedAttachments(composeBox).length;
    if (count > 0) {
      badge.textContent = count;
      badge.style.display = "inline-flex";
    } else {
      badge.style.display = "none";
    }
  }

  // ─── Mutation Observer & Scanning ───────────────────────────────────

  // Scan for all existing compose and inline reply boxes
  function scanComposeWindows() {
    // 1. Scan via all message bodies (covers inline replies, popup compose, full-screen)
    const messageBodies = document.querySelectorAll(
      'div[role="textbox"][contenteditable="true"], div.Am.Al.editable, div[aria-label*="Message Body"]'
    );
    messageBodies.forEach((body) => {
      const box = findComposeContainer(body);
      if (box) {
        setupComposeToolbar(box);
      }
    });

    // 2. Scan via send buttons (in case message body wasn't rendered yet)
    const sendButtons = document.querySelectorAll(
      'div.aoO, div.T-I-atl, div[role="button"][data-tooltip*="Send"]'
    );
    sendButtons.forEach((btn) => {
      if (isSendButton(btn)) {
        const box = findComposeContainer(btn);
        if (box) {
          setupComposeToolbar(box);
        }
      }
    });
  }

  // Listen for clicks or focus into any reply/compose box to immediately inject toggle
  document.addEventListener(
    "focusin",
    (e) => {
      if (
        e.target &&
        e.target.matches &&
        e.target.matches('div[role="textbox"], div.editable, [contenteditable="true"]')
      ) {
        const box = findComposeContainer(e.target);
        if (box) setupComposeToolbar(box);
      }
    },
    true
  );

  const observer = new MutationObserver(() => {
    scanComposeWindows();
  });

  observer.observe(document.body, { childList: true, subtree: true });
  scanComposeWindows();

  console.log("[EmailTracker] Gmail Email Open Tracker content script loaded (with attachment tracking).");
})();
