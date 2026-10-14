# Email Open Tracker & Gmail Chrome Extension

A minimal, self-hosted tool to see whether an email you sent has been opened.
Tested and working — single-user, private, no third parties involved.

Now includes an **automatic Chrome Extension (Manifest V3)** for Gmail (like Mailtrack), self-open filtering, and CORS support.

---

## Features

- **Chrome Extension (Manifest V3)**:
  - Automatically intercepts the Send button and `Ctrl+Enter` / `Cmd+Enter` in Gmail's compose window.
  - Generates a tracking ID using your email's Subject and Recipient as the label.
  - Automatically injects the hidden 1x1 tracking pixel and a visible transparency footer into the email before sending.
  - Adds a Mailtrack-style **👁️ Tracked** toggle button to the compose toolbar so you can toggle tracking on or off for individual drafts.
  - Extension popup and options pages to configure your backend URL, test connectivity, and toggle settings.
- **Flask Backend (`app.py`)**:
  - Full **CORS support** allowing requests from `mail.google.com` and custom clients.
  - **Self-Open Filtering**: Exclude opens originating from your own IP address so inspecting your sent emails doesn't register false positives.
  - **Transparency Disclosure**: Includes a clean, discreet footer line (`⚡ The sender is notified when this email is opened.`) so recipients know the email is tracked.
  - SQLite storage (`tracker.db`) with automatic schema setup and migrations.
  - Web dashboard (`/dashboard`) with live status badges, self-open filters, and detailed open history.

---

## Quick Start

### 1. Run the Flask Backend

Install dependencies:
```bash
pip install -r requirements.txt
```

Configure authentication. Every API call and the dashboard require it, and the server refuses access when these are unset:
```bash
# Windows PowerShell
$env:MAILSEEN_API_TOKEN="<long random string>"           # pasted into the extension popup
$env:MAILSEEN_DASHBOARD_PASSWORD="<dashboard password>"  # used at /login
$env:SECRET_KEY="<long random string>"                   # signs dashboard sessions
$env:MAILSEEN_PROXY_HOPS="0"                             # no reverse proxy when running locally
```
Generate random values with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

Run locally:
```bash
python app.py
```
Your backend will run at `http://localhost:5000`.

| Variable | Purpose |
|---|---|
| `MAILSEEN_API_TOKEN` | Bearer token the extension sends to `/new`, `/api/*` |
| `MAILSEEN_DASHBOARD_PASSWORD` | Password for the `/dashboard` login |
| `SECRET_KEY` | Session signing key (random per process if unset, which logs you out on restart) |
| `MAILSEEN_PROXY_HOPS` | Trusted reverse proxies in front of the app (default `1`, right for Render) |
| `MAILSEEN_CORS_ORIGINS` | Extra origins allowed to call the API cross-origin (the extension doesn't need this) |
| `MAILSEEN_SECURE_COOKIES` | Set to `0` to allow the session cookie over plain `http` (default `1`) |
| `MAILSEEN_DATA_DIR` | Directory for `tracker.db` and `uploads/` (default: next to `app.py`; `/var/data` on Render) |
| `MAILSEEN_DB_PATH`, `MAILSEEN_UPLOAD_DIR` | Override the database file / uploads directory individually |
| `MAILSEEN_RETENTION_DAYS` | Delete emails, documents and opens older than this (default `90`; `0` keeps everything) |
| `MAILSEEN_SENDER_IP_TTL_HOURS` | How long your IP counts as "self" for an email you just sent (default `6`) |
| `MAILSEEN_MIN_HUMAN_SECONDS` | Fetches sooner than this after sending count as scanners (default `5`) |

To filter your own IP via environment variable, you can optionally set:
```bash
# Windows PowerShell
$env:IGNORED_IPS="127.0.0.1,203.0.113.1"
python app.py
```
*(You can also filter your IP with 1 click directly inside the `/dashboard` or the Chrome extension popup!)*

### 2. Install the Chrome Extension

1. Open Chrome and go to `chrome://extensions/`.
2. Turn on **Developer mode** in the top right corner.
3. Click **Load unpacked**.
4. Select the `extension` folder inside this repository (`E:\email-tracker\extension`).
5. Click the extension icon in Chrome's toolbar to open the popup:
   - Verify or enter your backend URL (e.g. `http://localhost:5000` or your deployed HTTPS URL).
   - Paste your `MAILSEEN_API_TOKEN` into **API Token** (stored only on this device, never synced or shared with Gmail).
   - Click **Test** to verify connection and token.
   - Click **Filter My IP** to ensure your own opens are ignored.
   - Click **Save Settings**.

---

## Using with Gmail

1. Open [mail.google.com](https://mail.google.com) and click **Compose** (or reply to a thread).
2. You'll see a green **`👁️ Tracked`** badge in the bottom toolbar next to the Send button.
   - Click the badge at any time to toggle tracking ON or OFF for that specific draft.
3. Write your email and click **Send** (or press `Ctrl+Enter` / `Cmd+Enter`).
4. The extension automatically:
   - Labels the tracker as `Subject -> Recipient`.
   - Calls your backend to create a tracking ID.
   - Appends the invisible 1x1 image and disclosure footer:
     ```html
     ⚡ The sender is notified when this email is opened.
     ```
   - Sends the email immediately.
5. Open your dashboard at `http://localhost:5000/dashboard` (or click **Open Dashboard ↗** in the extension popup) to see open events in real time.

---

## Open Accuracy

An image load is not always a person reading your email. Every open is classified when it arrives, and that classification is never rewritten later, so the dashboard and the API always agree:

| Kind | What it is | Counts as opened? |
|---|---|---|
| `human` | A mail client loading images when the message is opened | ✅ |
| `proxy` | Gmail/Yahoo image proxy. These fetch only when the recipient opens the message, but hide their device and IP | ✅ |
| `apple_mpp` | Apple Mail Privacy Protection prefetch on delivery (bare `Mozilla/5.0` UA or Apple's `17.0.0.0/8`) | ❌ shown as "🤖 automated" |
| `scanner` | Security scanners and link checkers, scripts, `HEAD` probes, and anything within a few seconds of sending | ❌ shown as "🤖 automated" |

**Your own opens** are excluded in three ways:
- **Your sending IP**: for 6 hours after you send an email, opens from the IP you sent it from count as yours. This applies only to that email; it no longer adds your IP to a global ignore list forever.
- **Your Gmail**: when you view your own tracked message (Sent folder, a thread, or quoted text in a reply), the extension hides the pixel and tells the backend, so Gmail's proxy fetch is recorded as yours. Older pixels quoted into a reply are removed before sending, so the reply's recipient doesn't trigger opens on earlier emails.
- **Manual ignore list**: use **Filter My IP** in the popup, or set `IGNORED_IPS="ip1,ip2"`. This only affects opens from then on.

**Document views** are recorded by the viewer page only after it has been visible for 2 seconds. Link scanners that just fetch the URL don't count.

## Privacy

- Tracked emails get a visible footer by default: `⚡ The sender is notified when this email is opened.` You can change the text in the popup. Existing installs that still had the old emoji-only default are upgraded automatically.
- Recipient IPs are stored truncated (IPv4 `/24`, IPv6 `/48`), after self/scanner checks have used the full address. Your own sending IP is truncated once its 6-hour self-open window ends.
- Emails, documents (including their files) and opens older than `MAILSEEN_RETENTION_DAYS` (90) are deleted automatically, at most once an hour.

---

---

## ⚠️ Important: Tracking Real Emails over the Internet vs. Localhost

If your extension is configured with `http://localhost:5000`:
- **Why recipient opens don't register:** When you send an email to another person, the email contains an image URL pointing to `http://localhost:5000/track/<id>.png`. When the recipient opens the email on their device or in Gmail, their computer or Google's Image Proxy tries to reach `localhost:5000` on *their* own machine, which fails!
- **Google Image Proxy (`ggpht.com`):** Gmail routes all email images through Google's proxy servers. Google's servers strictly reject `localhost` and private IP addresses (`127.0.0.1`, `192.168.x.x`).
- **Solution:** To track emails opened by other people, your server must have a **public HTTPS URL**.

### Quick Option A: Free Local Tunnel (No deployment needed)
Run one of these in your terminal while `python app.py` is running:
```bash
# Using ngrok:
ngrok http 5000

# OR using localtunnel (no account needed):
npx localtunnel --port 5000
```
Copy the provided HTTPS URL (e.g. `https://xyz.ngrok-free.app`), open the extension popup, paste it into **Backend Server URL**, and click **Save Settings**.

### Quick Option B: Free Cloud Hosting (Render / Railway / Fly.io)
Deploy this repository to Render.com:
1. Push this folder to a GitHub repository.
2. Go to [render.com](https://render.com) and create a **Web Service**.
3. Set:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
4. `render.yaml` generates `SECRET_KEY` and `MAILSEEN_API_TOKEN` for you; set `MAILSEEN_DASHBOARD_PASSWORD` in the Render dashboard. Optionally set `IGNORED_IPS` (your home/office public IP).
5. `render.yaml` attaches a 1 GB persistent disk at `/var/data` (`MAILSEEN_DATA_DIR`). **Persistent disks need a paid instance (`plan: starter`)**. On the free plan the database and uploads are wiped on every deploy and restart.
6. Once deployed, copy your Render URL (e.g. `https://my-email-tracker.onrender.com`) and the generated `MAILSEEN_API_TOKEN` value.
7. In the Chrome Extension popup, set **Backend Server URL** and **API Token**, then click **Save Settings**. For any backend other than `https://mailseen.onrender.com`, Chrome will ask you to allow the extension to reach that site.

---

## Backend API Endpoints

🔒 = requires `Authorization: Bearer <MAILSEEN_API_TOKEN>`, or a dashboard session plus an `X-CSRF-Token` header for writes.

- 🔒 `POST /new`: Creates a new tracking ID from JSON `{"label": ..., "subject": ..., "recipients": ..., "attachment_ids": [...]}` (`subject`/`recipients` are optional; older clients' `"Subject -> recipients"` labels are parsed). Returns `tracking_id`, `pixel_url`, `html_snippet`, `disclosure_html` and `linked_attachment_ids` (only attachments not already linked to another email are linked).
- `GET /track/<tracking_id>.png`: Serves the 1x1 transparent PNG and logs the open event with its `open_kind`.
- 🔒 `GET /dashboard`: Web dashboard (redirects to `/login`). Supports `?q=` search, `?status=opened|waiting` and `?page=`.
- 🔒 `GET /api/status/<tracking_id>`: JSON status. `opened`/`open_count` include only `human` and `proxy` opens; `automated_opens` and `self_open_count` are reported separately.
- 🔒 `POST /api/self-view`: `{"tracking_ids": [...]}`. The extension reports that you are viewing these messages in Gmail.
- `POST /view/<id>/seen`: View beacon sent by the viewer page (requires the signed token embedded in the page).
- 🔒 `GET /api/my-ip`: Returns detected client IP and filter status.
- 🔒 `GET/POST /api/ignored-ips`: Lists or adds/removes IPs from the self-open filter.
- 🔒 `POST /api/upload-attachment`, `/api/delete-attachment`, `/api/delete-email`, `/api/clear-all`.
- `GET /view/<id>`, `GET /file/<id>`: Public attachment viewer and file. Files are served with a server-detected type, `nosniff` and a sandbox CSP; only PDFs and PNG/JPEG/GIF render inline, everything else downloads.

---

## Running Tests

Run the backend test suite (uses throwaway databases; never touches your `tracker.db`):
```bash
python -m unittest test_backend
```

The extension tests are dependency-free pages that load the real scripts with a stubbed `chrome` API. Serve the repository over HTTP (scripts are blocked on `file://`):
```bash
python -m http.server 5056 --bind 127.0.0.1
```
Then open `http://127.0.0.1:5056/tests/extension/content.test.html` and `http://127.0.0.1:5056/tests/extension/common.test.html`. The page title shows `PASS n/n` or the failures.
