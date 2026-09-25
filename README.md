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
  - **Transparency Disclosure**: Includes a clean, discreet footer line (`⚡ `) for transparency.
  - SQLite storage (`tracker.db`) with automatic schema setup and migrations.
  - Web dashboard (`/dashboard`) with live status badges, self-open filters, and detailed open history.

---

## Quick Start

### 1. Run the Flask Backend

Install dependencies:
```bash
pip install -r requirements.txt
```

Run locally:
```bash
python app.py
```
Your backend will run at `http://localhost:5000`.

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
   - Click **Test** to verify connection.
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
     ⚡ 
     ```
   - Sends the email immediately.
5. Open your dashboard at `http://localhost:5000/dashboard` (or click **Open Dashboard ↗** in the extension popup) to see open events in real time.

---

## Self-Open Filtering

When you send an email, opening your Sent folder can inadvertently load the pixel. The tracker prevents false opens:
- **Automatic IP Detection**: Visit `/dashboard` or click the extension popup to view your current IP.
- **One-Click Filtering**: Click **Filter My Current IP** to store it in the database.
- **Environment Variable**: Set `IGNORED_IPS="ip1,ip2"` for persistent static IP filtering.
- Filtered opens are marked with a yellow **Self-Open** tag and excluded from the main recipient open count.

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
4. Set optional environment variable: `IGNORED_IPS` (your home/office public IP).
5. Once deployed, copy your Render URL (e.g. `https://my-email-tracker.onrender.com`).
6. In the Chrome Extension popup, set **Backend Server URL** to your Render URL and click **Save Settings**.

---

## Backend API Endpoints

- `GET/POST /new?label=<name>`: Creates a new tracking ID. Returns JSON with `tracking_id`, `pixel_url`, `html_snippet`, and `disclosure_html`.
- `GET /track/<tracking_id>.png`: Serves the 1x1 transparent PNG and logs the open event.
- `GET /dashboard`: Web dashboard to view all emails, open counts, and manage filtered IPs.
- `GET /api/status/<tracking_id>`: JSON status of a specific email's opens.
- `GET /api/my-ip`: Returns detected client IP and filter status.
- `GET/POST /api/ignored-ips`: Lists or adds/removes IPs from the self-open filter.

---

## Running Tests

Run the backend test suite:
```bash
python test_backend.py
```
