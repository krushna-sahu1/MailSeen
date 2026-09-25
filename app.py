"""
Email Open Tracker
-------------------
A minimal, self-hosted email tracking pixel server for personal use.

Endpoints:
  GET/POST /new?label=<name>         -> creates a new tracking id, returns pixel URL, snippets & disclosure
  GET      /track/<tracking_id>.png  -> logs an "open" event (filters self-opens), returns 1x1 transparent PNG
  GET      /dashboard                -> view all tracked emails and their open history
  GET      /api/status/<tracking_id> -> JSON status for a tracking ID
  GET      /api/my-ip                -> returns detected client IP and filter status
  GET/POST /api/ignored-ips          -> manage self-open filtered IP addresses
  GET      /                         -> redirects to dashboard

Storage: SQLite (tracker.db), created automatically on first run.
"""

import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, request, Response, render_template, redirect, url_for, jsonify
from flask_cors import CORS

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "tracker.db"

app = Flask(__name__)
# Enable CORS for Chrome Extension and Gmail
CORS(app, resources={r"/*": {"origins": "*"}})

# 1x1 fully transparent PNG, hardcoded bytes (verified valid)
PIXEL_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082"
)

DISCLOSURE_LINE = "⚡ "
DISCLOSURE_HTML = (
    '<div style="color: #888888; font-size: 11px; margin-top: 16px; padding-top: 6px; '
    'border-top: 1px solid #eeeeee; font-family: -apple-system, BlinkMacSystemFont, '
    '\'Segoe UI\', Roboto, sans-serif;">'
    f'{DISCLOSURE_LINE}</div>'
)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS emails (
            id TEXT PRIMARY KEY,
            label TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS opens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracking_id TEXT,
            opened_at TEXT,
            ip TEXT,
            user_agent TEXT,
            is_self INTEGER DEFAULT 0,
            FOREIGN KEY(tracking_id) REFERENCES emails(id)
        )
        """
    )
    # Check if is_self column exists in opens (for backward compatibility)
    cursor = conn.execute("PRAGMA table_info(opens)")
    columns = [row[1] for row in cursor.fetchall()]
    if "is_self" not in columns:
        conn.execute("ALTER TABLE opens ADD COLUMN is_self INTEGER DEFAULT 0")

    # Check if sender_ip column exists in emails
    cursor = conn.execute("PRAGMA table_info(emails)")
    email_columns = [row[1] for row in cursor.fetchall()]
    if "sender_ip" not in email_columns:
        conn.execute("ALTER TABLE emails ADD COLUMN sender_ip TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ignored_ips (
            ip TEXT PRIMARY KEY,
            label TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


# Ensure database tables exist on Gunicorn startup
init_db()


def get_client_ip():
    """Extract real client IP, respecting reverse proxies like Render/Cloudflare/Nginx."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return request.remote_addr or "127.0.0.1"


def get_ignored_ips_list():
    """Return set of normalized ignored IPs from DB and environment variable."""
    ignored = set()

    # 1. From environment variable IGNORED_IPS (e.g. "127.0.0.1, 1.2.3.4")
    env_ips = os.getenv("IGNORED_IPS", "")
    if env_ips:
        for ip in env_ips.split(","):
            cleaned = ip.strip()
            if cleaned:
                ignored.add(cleaned)

    # 2. From database
    try:
        conn = get_db()
        rows = conn.execute("SELECT ip FROM ignored_ips").fetchall()
        for r in rows:
            ignored.add(r["ip"].strip())
        conn.close()
    except Exception:
        pass

    return ignored


def is_self_ip(client_ip):
    """Check if the provided IP matches any ignored IP."""
    if not client_ip:
        return False
    client_ip = client_ip.strip()
    ignored = get_ignored_ips_list()
    if client_ip in ignored:
        return True
    # Localhost alias check
    if client_ip in ("127.0.0.1", "::1", "localhost") and any(
        x in ("127.0.0.1", "::1", "localhost") for x in ignored
    ):
        return True
    return False


def parse_client_device(ua_string):
    """Parse User-Agent string to provide a human-friendly client name."""
    if not ua_string:
        return "Unknown Client"
    ua = ua_string.lower()
    if "googleimageproxy" in ua or "ggpht.com" in ua:
        return "Gmail (Google Image Proxy)"
    if "iphone" in ua or "ipad" in ua:
        return "Apple Mail (iOS)"
    if "macintosh" in ua and "applewebkit" in ua and "chrome" not in ua:
        return "Apple Mail (macOS)"
    if "outlook" in ua or "office" in ua:
        return "Microsoft Outlook"
    if "thunderbird" in ua:
        return "Mozilla Thunderbird"
    if "edg" in ua:
        return "Microsoft Edge"
    if "chrome" in ua and "edg" not in ua:
        return "Google Chrome"
    if "firefox" in ua:
        return "Mozilla Firefox"
    if "safari" in ua and "chrome" not in ua:
        return "Apple Safari"
    return "Mail / Web Client"


@app.route("/")
def home():
    return redirect(url_for("dashboard"))


@app.route("/new", methods=["GET", "POST"])
def new_tracker():
    """Create a new tracking pixel.
    Accepts GET ?label=... or POST with JSON { "label": "..." } or form data.
    """
    label = None
    if request.is_json:
        data = request.get_json(silent=True) or {}
        label = data.get("label")
    if not label:
        label = request.values.get("label", "Untitled email")

    tracking_id = uuid.uuid4().hex
    sender_ip = get_client_ip()

    conn = get_db()
    # Auto-filter the sender's current IP so their own browser never registers as a recipient
    if sender_ip and sender_ip not in ("127.0.0.1", "::1", "localhost"):
        try:
            conn.execute(
                "INSERT OR IGNORE INTO ignored_ips (ip, label, created_at) VALUES (?, ?, ?)",
                (sender_ip, "Sender IP (Auto)", datetime.utcnow().isoformat())
            )
        except Exception:
            pass

    conn.execute(
        "INSERT INTO emails (id, label, created_at, sender_ip) VALUES (?, ?, ?, ?)",
        (tracking_id, label, datetime.utcnow().isoformat(), sender_ip),
    )
    conn.commit()
    conn.close()

    base_url = request.url_root.rstrip("/")
    pixel_url = f"{base_url}/track/{tracking_id}.png"
    # Never use display:none !important as email clients & Google Image Proxy will prune/skip loading it!
    html_snippet = (
        f'<img src="{pixel_url}" width="1" height="1" alt="" border="0" '
        'style="width:1px !important; min-width:1px !important; max-width:1px !important; '
        'height:1px !important; min-height:1px !important; max-height:1px !important; '
        'border:0 !important; outline:none !important; margin:0 !important; padding:0 !important; '
        'opacity:0 !important; pointer-events:none !important; position:absolute !important; left:-9999px !important;" />'
    )
    combined_snippet = f"{html_snippet}{DISCLOSURE_HTML}"

    return jsonify(
        {
            "tracking_id": tracking_id,
            "label": label,
            "pixel_url": pixel_url,
            "html_snippet": html_snippet,
            "disclosure_html": DISCLOSURE_HTML,
            "disclosure_text": DISCLOSURE_LINE,
            "combined_snippet": combined_snippet,
            "instructions": "Paste html_snippet or combined_snippet into the HTML body of your email, near the end.",
        }
    )


@app.route("/track/<tracking_id>.png")
def track(tracking_id):
    """Serve the pixel and log the open event, identifying self-opens."""
    client_ip = get_client_ip()
    user_agent = request.headers.get("User-Agent", "")

    conn = get_db()
    email = conn.execute("SELECT id, sender_ip FROM emails WHERE id = ?", (tracking_id,)).fetchone()
    if email:
        sender_ip = (email["sender_ip"] or "").strip()
        self_open = is_self_ip(client_ip) or (sender_ip and client_ip == sender_ip)
        conn.execute(
            "INSERT INTO opens (tracking_id, opened_at, ip, user_agent, is_self) VALUES (?, ?, ?, ?, ?)",
            (
                tracking_id,
                datetime.utcnow().isoformat(),
                client_ip,
                user_agent,
                1 if self_open else 0,
            ),
        )
        conn.commit()
    conn.close()

    resp = Response(PIXEL_BYTES, mimetype="image/png")
    # Comprehensive anti-caching headers for Google Image Proxy & mail clients
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0, private, post-check=0, pre-check=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["Surrogate-Control"] = "no-store"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/dashboard")
def dashboard():
    conn = get_db()
    emails = conn.execute("SELECT * FROM emails ORDER BY created_at DESC").fetchall()
    ignored_ips_set = get_ignored_ips_list()

    results = []
    total_genuine_opens = 0
    total_self_opens = 0

    for e in emails:
        opens = conn.execute(
            "SELECT * FROM opens WHERE tracking_id = ? ORDER BY opened_at DESC",
            (e["id"],),
        ).fetchall()
        parsed_opens = []
        sender_ip = (e["sender_ip"] or "").strip() if "sender_ip" in e.keys() else ""

        for o in opens:
            o_dict = dict(o)
            open_ip = (o_dict.get("ip") or "").strip()
            # If IP is in ignored IPs or matches sender IP, mark as self open
            if not o_dict["is_self"]:
                if open_ip in ignored_ips_set or (sender_ip and open_ip == sender_ip):
                    o_dict["is_self"] = 1

            o_dict["client_name"] = parse_client_device(o_dict.get("user_agent", ""))
            parsed_opens.append(o_dict)

        genuine_opens = [o for o in parsed_opens if not o["is_self"]]
        self_opens = [o for o in parsed_opens if o["is_self"]]

        total_genuine_opens += len(genuine_opens)
        total_self_opens += len(self_opens)

        results.append(
            {
                "id": e["id"],
                "label": e["label"],
                "created_at": e["created_at"],
                "open_count": len(genuine_opens),
                "self_open_count": len(self_opens),
                "last_opened_at": genuine_opens[0]["opened_at"] if genuine_opens else None,
                "last_client": genuine_opens[0]["client_name"] if genuine_opens else None,
                "opens": genuine_opens,  # Only genuine recipient opens shown in main timeline
                "all_opens": parsed_opens,
            }
        )

    ignored_rows = conn.execute("SELECT * FROM ignored_ips ORDER BY created_at DESC").fetchall()
    conn.close()

    client_ip = get_client_ip()
    is_client_ignored = is_self_ip(client_ip)
    env_ips = [x.strip() for x in os.getenv("IGNORED_IPS", "").split(",") if x.strip()]

    total_emails = len(results)
    opened_emails = sum(1 for e in results if e["open_count"] > 0)
    open_rate = round((opened_emails / total_emails) * 100) if total_emails > 0 else 0

    stats = {
        "total_emails": total_emails,
        "opened_emails": opened_emails,
        "unopened_emails": total_emails - opened_emails,
        "total_opens": total_genuine_opens,
        "total_self_opens": total_self_opens,
        "open_rate": open_rate,
    }

    # Detect if running on localhost/loopback
    is_localhost = client_ip in ("127.0.0.1", "::1", "localhost") or "localhost" in request.host or "127.0.0.1" in request.host

    return render_template(
        "dashboard.html",
        emails=results,
        client_ip=client_ip,
        is_client_ignored=is_client_ignored,
        ignored_ips=[dict(r) for r in ignored_rows],
        env_ips=env_ips,
        stats=stats,
        is_localhost=is_localhost,
        host_url=request.host_url.rstrip("/"),
    )


@app.route("/api/status/<tracking_id>")
def status(tracking_id):
    """JSON status for a single tracking id - ignores self-opens in count."""
    conn = get_db()
    opens = conn.execute(
        "SELECT * FROM opens WHERE tracking_id = ? ORDER BY opened_at DESC",
        (tracking_id,),
    ).fetchall()
    conn.close()

    parsed_opens = []
    for o in opens:
        o_dict = dict(o)
        o_dict["client_name"] = parse_client_device(o_dict.get("user_agent", ""))
        parsed_opens.append(o_dict)

    genuine_opens = [o for o in parsed_opens if not o["is_self"]]
    self_opens = [o for o in parsed_opens if o["is_self"]]

    return jsonify(
        {
            "tracking_id": tracking_id,
            "opened": len(genuine_opens) > 0,
            "open_count": len(genuine_opens),
            "self_open_count": len(self_opens),
            "opens": genuine_opens,
            "all_opens": parsed_opens,
        }
    )


@app.route("/api/my-ip")
def api_my_ip():
    """Return client's detected IP and whether it is currently filtered."""
    ip = get_client_ip()
    return jsonify(
        {
            "ip": ip,
            "is_ignored": is_self_ip(ip),
        }
    )


@app.route("/api/ignored-ips", methods=["GET", "POST"])
def api_ignored_ips():
    """List, add, or remove ignored IPs for self-open filtering."""
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form
        ip = (data.get("ip") or "").strip()
        action = data.get("action", "add")
        label = data.get("label", "My IP")

        if not ip:
            return jsonify({"error": "IP address is required"}), 400

        conn = get_db()
        if action == "remove":
            conn.execute("DELETE FROM ignored_ips WHERE ip = ?", (ip,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO ignored_ips (ip, label, created_at) VALUES (?, ?, ?)",
                (ip, label, datetime.utcnow().isoformat()),
            )
        conn.commit()
        conn.close()

        return jsonify({"success": True, "ip": ip, "action": action})

    # GET
    conn = get_db()
    rows = conn.execute("SELECT * FROM ignored_ips ORDER BY created_at DESC").fetchall()
    conn.close()

    env_ips = [x.strip() for x in os.getenv("IGNORED_IPS", "").split(",") if x.strip()]

    return jsonify(
        {
            "current_ip": get_client_ip(),
            "ignored_ips": [dict(r) for r in rows],
            "env_ignored_ips": env_ips,
        }
    )


@app.route("/api/delete-email", methods=["POST"])
def api_delete_email():
    """Delete a tracked email and all associated open events."""
    data = request.get_json(silent=True) or request.form
    tracking_id = (data.get("tracking_id") or "").strip()
    if not tracking_id:
        return jsonify({"error": "Tracking ID is required"}), 400

    conn = get_db()
    conn.execute("DELETE FROM opens WHERE tracking_id = ?", (tracking_id,))
    conn.execute("DELETE FROM emails WHERE id = ?", (tracking_id,))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "tracking_id": tracking_id})


@app.route("/api/clear-all", methods=["POST"])
def api_clear_all():
    """Clear all tracked emails and opens history."""
    conn = get_db()
    conn.execute("DELETE FROM opens")
    conn.execute("DELETE FROM emails")
    conn.commit()
    conn.close()
    return jsonify({"success": True})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)

