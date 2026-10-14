"""
Email Open Tracker
-------------------
A minimal, self-hosted email tracking pixel server for personal use.

Endpoints (* = requires the API token or a dashboard login):
  GET/POST /login, POST /logout      -> dashboard session login / logout
  POST     /new                      -> * creates a new tracking id, returns pixel URL, snippets & disclosure
  GET      /track/<tracking_id>.png  -> logs an "open" event (filters self-opens), returns 1x1 transparent PNG
  GET      /dashboard                -> * view all tracked emails and their open history
  GET      /api/status/<tracking_id> -> * JSON status for a tracking ID
  GET      /api/my-ip                -> * returns detected client IP and filter status
  GET/POST /api/ignored-ips          -> * manage self-open filtered IP addresses
  POST     /api/upload-attachment    -> * upload a file for tracked sharing, returns view URL
  GET      /view/<attachment_id>     -> branded viewer page for tracked attachment (logs view event)
  GET      /file/<attachment_id>     -> raw file download/inline serve (sandboxed)
  GET      /api/attachment-status/<id> -> * JSON view stats for an attachment
  POST     /api/delete-attachment    -> * delete a tracked attachment
  POST     /api/delete-email, /api/clear-all -> * delete records
  GET      /                         -> redirects to dashboard

Storage: SQLite (tracker.db), created automatically on first run.
         Uploaded files stored in uploads/ directory.

Configuration (environment variables):
  MAILSEEN_DATA_DIR            directory holding tracker.db and uploads/ (default: next to app.py)
  MAILSEEN_DB_PATH             override the SQLite file path (default: $MAILSEEN_DATA_DIR/tracker.db)
  MAILSEEN_UPLOAD_DIR          override the uploads directory (default: $MAILSEEN_DATA_DIR/uploads)
  MAILSEEN_API_TOKEN           bearer token the Chrome extension sends (required for API calls)
  MAILSEEN_DASHBOARD_PASSWORD  password for the /dashboard login (required for the dashboard)
  SECRET_KEY                   Flask session signing key (random per process if unset)
  MAILSEEN_CORS_ORIGINS        comma-separated origins allowed to call /api/* and /new cross-origin
                               (the extension doesn't need this: its service worker bypasses CORS)
  MAILSEEN_PROXY_HOPS          number of trusted reverse proxies in front of the app (default 1)
  MAILSEEN_SECURE_COOKIES      "0" to allow the session cookie over plain http (default "1")
  MAILSEEN_SENDER_IP_TTL_HOURS how long the sender's IP counts as "self" for a new email (default 6)
  MAILSEEN_MIN_HUMAN_SECONDS   fetches sooner than this after sending count as scanners (default 5)
  MAILSEEN_RETENTION_DAYS      delete tracking data older than this many days; 0 keeps it (default 90)
"""

import hmac
import ipaddress
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from itsdangerous import BadSignature, URLSafeTimedSerializer

from flask import (
    Flask, g, request, Response, render_template, redirect, url_for, jsonify, send_from_directory, session,
)
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

APP_DIR = Path(__file__).parent
# On Render, point MAILSEEN_DATA_DIR at the persistent disk mount; the default keeps local dev unchanged.
DATA_DIR = Path(os.getenv("MAILSEEN_DATA_DIR", APP_DIR))

MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB

# MIME type is derived server-side from the extension; the client-supplied Content-Type is never trusted.
# SVG is deliberately excluded: it can carry script that would run on this origin.
MIME_TYPES = {
    '.pdf': 'application/pdf',
    '.doc': 'application/msword',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.ppt': 'application/vnd.ms-powerpoint',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.xls': 'application/vnd.ms-excel',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.txt': 'text/plain',
    '.csv': 'text/csv',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.zip': 'application/zip',
}
ALLOWED_EXTENSIONS = set(MIME_TYPES)

# Only these types are rendered inline; everything else is forced to download.
# Leading bytes must match, so e.g. an HTML file renamed to .png is rejected at upload.
INLINE_SIGNATURES = {
    'image/png': (b'\x89PNG\r\n\x1a\n',),
    'image/jpeg': (b'\xff\xd8\xff',),
    'image/gif': (b'GIF87a', b'GIF89a'),
}
INLINE_TYPES = {'application/pdf', *INLINE_SIGNATURES}

# Applied to every served upload: no script, no subresources, opaque origin.
FILE_CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'; sandbox"

# ─── Open accuracy ─────────────────────────────────────────────────────
# How long after sending the sender's own IP is treated as "self" (e.g. their browser loading the
# pixel while composing, or their desktop client syncing Sent mail).
SENDER_IP_TTL = timedelta(hours=float(os.getenv("MAILSEEN_SENDER_IP_TTL_HOURS", "6")))
# When the extension sees the sender viewing their own message in Gmail, proxy fetches within this
# window either side of the report are marked as self.
SELF_VIEW_WINDOW = timedelta(seconds=60)
# Fetches sooner than this after sending come from delivery-time scanners, not people.
MIN_HUMAN_SECONDS = float(os.getenv("MAILSEEN_MIN_HUMAN_SECONDS", "5"))

OPEN_KINDS = ("human", "proxy", "apple_mpp", "scanner")
# Gmail/Yahoo proxies only fetch when the recipient actually opens the message, so they count.
COUNTED_OPEN_KINDS = {"human", "proxy"}
IMAGE_PROXY_UA_MARKERS = ("googleimageproxy", "ggpht.com", "yahoomailproxy")
SCANNER_UA_MARKERS = (
    "bot/", "crawler", "spider", "headlesschrome", "python-requests", "curl/", "wget/", "go-http-client",
    "barracuda", "mimecast", "proofpoint", "bingpreview", "safelinks", "trendmicro", "symantec",
    "forcepoint", "sophos", "zscaler", "cisco",
)
# Apple Mail Privacy Protection prefetches from Apple-owned addresses with a bare "Mozilla/5.0" UA.
APPLE_MPP_NETWORKS = [ipaddress.ip_network("17.0.0.0/8")]
# A document viewer page must report its view within this many seconds of being rendered.
VIEW_BEACON_MAX_AGE = 3600

# ─── Privacy ───────────────────────────────────────────────────────────
# Tracked emails, attachments and every open/view older than this are deleted. 0 disables the purge.
RETENTION_DAYS = int(os.getenv("MAILSEEN_RETENTION_DAYS", "90"))
PURGE_INTERVAL = timedelta(hours=1)
# Recipient IPs are stored truncated to these prefixes (classification uses the full IP first).
IPV4_KEEP_PREFIX = 24
IPV6_KEEP_PREFIX = 48

app = Flask(__name__)
app.config.update(
    DB_PATH=Path(os.getenv("MAILSEEN_DB_PATH", DATA_DIR / "tracker.db")),
    UPLOAD_DIR=Path(os.getenv("MAILSEEN_UPLOAD_DIR", DATA_DIR / "uploads")),
    MAX_CONTENT_LENGTH=MAX_FILE_SIZE,
    SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
    API_TOKEN=os.getenv("MAILSEEN_API_TOKEN", ""),
    DASHBOARD_PASSWORD=os.getenv("MAILSEEN_DASHBOARD_PASSWORD", ""),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("MAILSEEN_SECURE_COOKIES", "1") != "0",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

# Trust exactly N proxy hops for the client IP and scheme; entries further left in
# X-Forwarded-For are client-controlled and ignored.
PROXY_HOPS = int(os.getenv("MAILSEEN_PROXY_HOPS", "1"))
if PROXY_HOPS > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=PROXY_HOPS, x_proto=PROXY_HOPS)

# CORS only on API routes and only for explicitly configured origins. The Chrome extension
# calls the backend from its service worker under host_permissions, which bypasses CORS.
CORS_ORIGINS = [o.strip() for o in os.getenv("MAILSEEN_CORS_ORIGINS", "").split(",") if o.strip()]
if CORS_ORIGINS:
    CORS(
        app,
        resources={r"/api/*": {"origins": CORS_ORIGINS}, r"/new": {"origins": CORS_ORIGINS}},
        allow_headers=["Authorization", "Content-Type"],
    )

# 1x1 fully transparent PNG, hardcoded bytes (verified valid)
PIXEL_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082"
)

DISCLOSURE_LINE = "⚡ The sender is notified when this email is opened."
DISCLOSURE_HTML = (
    '<div style="color: #888888; font-size: 11px; margin-top: 16px; padding-top: 6px; '
    'border-top: 1px solid #eeeeee; font-family: -apple-system, BlinkMacSystemFont, '
    '\'Segoe UI\', Roboto, sans-serif;">'
    f'{DISCLOSURE_LINE}</div>'
)


def upload_dir():
    return Path(app.config["UPLOAD_DIR"])


SQLITE_BUSY_TIMEOUT_MS = 5000


def connect():
    """A new connection. Callers own it and must close it (migrations, the purge job, tests)."""
    conn = sqlite3.connect(app.config["DB_PATH"], timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    # Wait for a concurrent writer instead of failing with "database is locked"
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return conn


def get_db():
    """The current request's connection, closed by close_db() even if the view raised.
    Uncommitted work is rolled back on close, so a failed request leaves no partial writes."""
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def utcnow():
    """Naive UTC datetime; all timestamps are stored as naive-UTC ISO strings so they compare as text."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_iso():
    return utcnow().isoformat()


def parse_ts(value):
    return datetime.fromisoformat(value) if value else None


# ─── Schema migrations ─────────────────────────────────────────────────
# PRAGMA user_version records the last applied migration. Each migration runs once, in order.

def _migrate_v1_baseline(conn):
    """Original schema. Uses IF NOT EXISTS / column checks so it is safe on pre-versioning databases."""
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

    # Attachment tracking tables
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY,
            email_tracking_id TEXT,
            filename TEXT,
            content_type TEXT,
            file_size INTEGER,
            file_path TEXT,
            label TEXT,
            created_at TEXT,
            sender_ip TEXT,
            FOREIGN KEY(email_tracking_id) REFERENCES emails(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attachment_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attachment_id TEXT,
            viewed_at TEXT,
            ip TEXT,
            user_agent TEXT,
            is_self INTEGER DEFAULT 0,
            FOREIGN KEY(attachment_id) REFERENCES attachments(id)
        )
        """
    )


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_v2_accuracy(conn):
    """open_kind classification, self-view suppression window, and no more auto-ignored sender IPs."""
    if "open_kind" not in _columns(conn, "opens"):
        conn.execute("ALTER TABLE opens ADD COLUMN open_kind TEXT")
    if "open_kind" not in _columns(conn, "attachment_views"):
        conn.execute("ALTER TABLE attachment_views ADD COLUMN open_kind TEXT")
    if "suppress_until" not in _columns(conn, "emails"):
        conn.execute("ALTER TABLE emails ADD COLUMN suppress_until TEXT")

    # Sender IPs used to be added to the global ignore list forever, hiding real opens from anyone
    # sharing that IP later. Sender IPs now apply per email for SENDER_IP_TTL only.
    conn.execute("DELETE FROM ignored_ips WHERE label = 'Sender IP (Auto)'")

    rows = conn.execute(
        "SELECT o.id, o.ip, o.user_agent, o.opened_at, e.created_at FROM opens o "
        "LEFT JOIN emails e ON e.id = o.tracking_id WHERE o.open_kind IS NULL"
    ).fetchall()
    for r in rows:
        opened, created = parse_ts(r["opened_at"]), parse_ts(r["created_at"])
        seconds = (opened - created).total_seconds() if opened and created else None
        conn.execute("UPDATE opens SET open_kind = ? WHERE id = ?",
                     (classify_open(r["ip"], r["user_agent"], seconds), r["id"]))

    rows = conn.execute("SELECT id, ip, user_agent FROM attachment_views WHERE open_kind IS NULL").fetchall()
    for r in rows:
        conn.execute("UPDATE attachment_views SET open_kind = ? WHERE id = ?",
                     (classify_open(r["ip"], r["user_agent"]), r["id"]))


def _migrate_v3_truncate_ips(conn):
    """Recipient IPs were stored in full; keep only the network prefix."""
    for table in ("opens", "attachment_views"):
        for row in conn.execute(f"SELECT id, ip FROM {table} WHERE ip IS NOT NULL").fetchall():
            conn.execute(f"UPDATE {table} SET ip = ? WHERE id = ?", (truncate_ip(row["ip"]), row["id"]))


def _migrate_v4_indexes(conn):
    """Every dashboard/status lookup and the retention purge filter on these columns."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_opens_tracking ON opens(tracking_id, opened_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_opens_opened_at ON opens(opened_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_views_attachment ON attachment_views(attachment_id, viewed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_views_viewed_at ON attachment_views(viewed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attachments_email ON attachments(email_tracking_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_created ON emails(created_at)")


def split_label(label):
    """Legacy "Subject -> recipients" label into (subject, recipients). Splits on the last arrow, since
    subjects may contain "->" but email addresses never do."""
    label = label or ""
    if "->" not in label:
        return label.strip(), ""
    subject, recipients = label.rsplit("->", 1)
    return subject.strip(), recipients.strip()


def _migrate_v5_subject_recipients(conn):
    """Store subject and recipients separately instead of re-parsing the label on every render."""
    for column in ("subject", "recipients"):
        if column not in _columns(conn, "emails"):
            conn.execute(f"ALTER TABLE emails ADD COLUMN {column} TEXT")
    for row in conn.execute("SELECT id, label FROM emails WHERE subject IS NULL").fetchall():
        conn.execute("UPDATE emails SET subject = ?, recipients = ? WHERE id = ?", (*split_label(row["label"]), row["id"]))


MIGRATIONS = [
    _migrate_v1_baseline,
    _migrate_v2_accuracy,
    _migrate_v3_truncate_ips,
    _migrate_v4_indexes,
    _migrate_v5_subject_recipients,
]


def init_db():
    Path(app.config["DB_PATH"]).parent.mkdir(parents=True, exist_ok=True)
    upload_dir().mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        # WAL lets the dashboard read while pixel hits write; the setting persists in the file.
        conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, migration in enumerate(MIGRATIONS[version:], start=version + 1):
            migration(conn)
            conn.execute(f"PRAGMA user_version = {number}")
            conn.commit()
    finally:
        conn.close()


def get_client_ip():
    """Client IP as resolved by ProxyFix from the trusted proxy hops."""
    return request.remote_addr or "127.0.0.1"


# ─── Authentication ────────────────────────────────────────────────────

def _bearer_token_valid():
    expected = app.config.get("API_TOKEN") or ""
    header = request.headers.get("Authorization", "")
    if not expected or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer "):].strip().encode(), expected.encode())


def _session_valid():
    return session.get("authenticated") is True


def _csrf_valid():
    expected = session.get("csrf_token") or ""
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    return bool(expected) and hmac.compare_digest(supplied.encode(), expected.encode())


def require_auth(view):
    """API routes: accept the extension's bearer token, or a dashboard session (plus CSRF token for writes)."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if _bearer_token_valid():
            return view(*args, **kwargs)
        if _session_valid():
            if request.method in ("GET", "HEAD") or _csrf_valid():
                return view(*args, **kwargs)
            return jsonify({"error": "CSRF token missing or invalid"}), 403
        return jsonify({"error": "Authentication required"}), 401
    return wrapper


def login_required(view):
    """HTML pages: redirect to the login form when there is no dashboard session."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _session_valid():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def _safe_next(target):
    """Only allow local paths like "/dashboard" as a post-login redirect, never "//evil.com"."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("dashboard")


LOCALHOST_ALIASES = ("127.0.0.1", "::1", "localhost")


def env_ignored_ips():
    """IGNORED_IPS environment variable, e.g. "127.0.0.1, 1.2.3.4"."""
    return [ip.strip() for ip in os.getenv("IGNORED_IPS", "").split(",") if ip.strip()]


def is_ignored_ip(conn, client_ip):
    """True if client_ip is on the manual ignore list (env or DB). One indexed lookup, no full scan."""
    if not client_ip:
        return False
    client_ip = client_ip.strip()
    candidates = LOCALHOST_ALIASES if client_ip in LOCALHOST_ALIASES else (client_ip,)
    if any(ip in env_ignored_ips() for ip in candidates):
        return True
    placeholders = ",".join("?" * len(candidates))
    return conn.execute(f"SELECT 1 FROM ignored_ips WHERE ip IN ({placeholders})", candidates).fetchone() is not None


def is_sender_ip(record, client_ip, now):
    """The sender's own IP only counts as "self" for SENDER_IP_TTL after the email/upload was created."""
    sender_ip = (record["sender_ip"] or "").strip()
    created = parse_ts(record["created_at"])
    return bool(sender_ip) and client_ip == sender_ip and created is not None and now - created <= SENDER_IP_TTL


def _ip_in(ip, networks):
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    return any(addr in net for net in networks)


def truncate_ip(ip):
    """Drop the host part of an IP (IPv4 /24, IPv6 /48) so stored addresses no longer identify a device."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return None
    prefix = IPV4_KEEP_PREFIX if addr.version == 4 else IPV6_KEEP_PREFIX
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False).network_address)


def classify_open(ip, user_agent, seconds_since_send=None, method="GET"):
    """Classify a pixel fetch or document view as one of OPEN_KINDS.

    human      a mail client loading images on open
    proxy      Gmail/Yahoo image proxy: fetches when the recipient opens, but hides their device and IP
    apple_mpp  Apple Mail Privacy Protection prefetch: happens on delivery, says nothing about reading
    scanner    security scanners, link checkers, scripts, HEAD probes, and fetches too soon after send
    """
    ua = (user_agent or "").strip()
    ua_lower = ua.lower()
    if method == "HEAD" or not ua or any(marker in ua_lower for marker in SCANNER_UA_MARKERS):
        return "scanner"
    if ua == "Mozilla/5.0" or _ip_in(ip, APPLE_MPP_NETWORKS):
        return "apple_mpp"
    if seconds_since_send is not None and seconds_since_send < MIN_HUMAN_SECONDS:
        return "scanner"
    if any(marker in ua_lower for marker in IMAGE_PROXY_UA_MARKERS):
        return "proxy"
    return "human"


def split_events(rows):
    """Partition open/view rows into (counted, automated, own) and add a readable client name."""
    counted, automated, own = [], [], []
    for row in rows:
        event = dict(row)
        event["client_name"] = parse_client_device(event.get("user_agent", ""))
        if event["is_self"]:
            own.append(event)
        elif (event.get("open_kind") or "human") in COUNTED_OPEN_KINDS:
            counted.append(event)
        else:
            automated.append(event)
    return counted, automated, own


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


def format_file_size(size_bytes):
    """Format file size in human-readable form."""
    if size_bytes is None:
        return "Unknown"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def is_allowed_file(filename):
    """Check if the file extension is in the allowed list."""
    ext = Path(filename).suffix.lower()
    return ext in ALLOWED_EXTENSIONS


def mime_type_for(filename):
    """Server-side MIME type from the file extension; unknown or legacy types fall back to octet-stream."""
    return MIME_TYPES.get(Path(filename or "").suffix.lower(), "application/octet-stream")


def has_valid_signature(head, mimetype):
    """Check the leading bytes of types we render inline."""
    if mimetype == "application/pdf":
        return b"%PDF-" in head[:1024]  # the PDF spec tolerates leading bytes before the header
    signatures = INLINE_SIGNATURES.get(mimetype)
    return head.startswith(signatures) if signatures else True


@app.after_request
def security_headers(response):
    """Baseline headers for every response. Routes that set their own (e.g. /file's sandbox CSP) win."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # Dashboard and viewer URLs contain tracking/attachment IDs; don't leak them to other sites
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if response.mimetype == "text/html":
        # No page here is meant to be framed (clickjacking on the dashboard's delete buttons)
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        response.headers.setdefault("X-Frame-Options", "DENY")
    return response


@app.errorhandler(413)
def request_too_large(_error):
    # JSON so the extension can show the reason instead of a generic HTTP error
    return jsonify({"error": f"File is too large (max {MAX_FILE_SIZE // (1024 * 1024)} MB)"}), 413


@app.route("/")
def home():
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        expected = app.config.get("DASHBOARD_PASSWORD") or ""
        supplied = request.form.get("password", "")
        if not expected:
            error = "Dashboard password is not configured on the server (set MAILSEEN_DASHBOARD_PASSWORD)."
        elif hmac.compare_digest(supplied.encode(), expected.encode()):
            session.clear()
            session.permanent = True
            session["authenticated"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            return redirect(_safe_next(request.args.get("next")))
        else:
            error = "Incorrect password."
    return render_template("login.html", error=error), (401 if error else 200)


@app.route("/logout", methods=["POST"])
def logout():
    if _session_valid() and not _csrf_valid():
        return jsonify({"error": "CSRF token missing or invalid"}), 403
    session.clear()
    return redirect(url_for("login"))


@app.route("/new", methods=["POST"])
@require_auth
def new_tracker():
    """Create a new tracking pixel.
    Accepts POST with JSON { "label": "...", "attachment_ids": [...] } or form data.
    """
    data = (request.get_json(silent=True) if request.is_json else None) or {}
    label = data.get("label") or request.form.get("label") or "Untitled email"
    # Current extensions send subject/recipients; older clients only send the combined label.
    subject, recipients = data.get("subject"), data.get("recipients")
    if not isinstance(subject, str) or not isinstance(recipients, str):
        subject, recipients = split_label(label)
    attachment_ids = data.get("attachment_ids") or []
    if not isinstance(attachment_ids, list):
        return jsonify({"error": "attachment_ids must be a list"}), 400

    tracking_id = uuid.uuid4().hex
    sender_ip = get_client_ip()

    conn = get_db()
    # The sender IP is stored per email and only treated as "self" for SENDER_IP_TTL.
    conn.execute(
        "INSERT INTO emails (id, label, subject, recipients, created_at, sender_ip) VALUES (?, ?, ?, ?, ?, ?)",
        (tracking_id, label, subject.strip(), recipients.strip(), utcnow_iso(), sender_ip),
    )

    # Link pending attachments to this email. Only unlinked uploads can be claimed, so an
    # attachment already sent with another email is never re-assigned.
    linked_attachment_ids = []
    for aid in attachment_ids:
        if not isinstance(aid, str):
            continue
        cur = conn.execute(
            "UPDATE attachments SET email_tracking_id = ? WHERE id = ? AND email_tracking_id IS NULL",
            (tracking_id, aid),
        )
        if cur.rowcount:
            linked_attachment_ids.append(aid)

    conn.commit()

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
            "subject": subject.strip(),
            "recipients": recipients.strip(),
            "pixel_url": pixel_url,
            "html_snippet": html_snippet,
            "disclosure_html": DISCLOSURE_HTML,
            "disclosure_text": DISCLOSURE_LINE,
            "combined_snippet": combined_snippet,
            "linked_attachment_ids": linked_attachment_ids,
            "instructions": "Paste html_snippet or combined_snippet into the HTML body of your email, near the end.",
        }
    )


@app.route("/track/<tracking_id>.png")
def track(tracking_id):
    """Serve the pixel and log the open event, classified by kind and with self-opens flagged."""
    client_ip = get_client_ip()
    user_agent = request.headers.get("User-Agent", "")
    now = utcnow()

    conn = get_db()
    email = conn.execute(
        "SELECT id, sender_ip, created_at, suppress_until FROM emails WHERE id = ?", (tracking_id,)
    ).fetchone()
    if email:
        created = parse_ts(email["created_at"])
        seconds = (now - created).total_seconds() if created else None
        kind = classify_open(client_ip, user_agent, seconds, request.method)
        suppress_until = parse_ts(email["suppress_until"])
        self_open = (
            is_ignored_ip(conn, client_ip)
            or is_sender_ip(email, client_ip, now)
            # The sender is viewing this message in Gmail right now (reported by the extension)
            or (kind == "proxy" and suppress_until is not None and now <= suppress_until)
        )
        conn.execute(
            "INSERT INTO opens (tracking_id, opened_at, ip, user_agent, is_self, open_kind) VALUES (?, ?, ?, ?, ?, ?)",
            (tracking_id, now.isoformat(), truncate_ip(client_ip), user_agent, 1 if self_open else 0, kind),
        )
        conn.commit()

    resp = Response(PIXEL_BYTES, mimetype="image/png")
    # Comprehensive anti-caching headers for Google Image Proxy & mail clients
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0, private, post-check=0, pre-check=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["Surrogate-Control"] = "no-store"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


# ─── Attachment Tracking ───────────────────────────────────────────────

@app.route("/api/upload-attachment", methods=["POST"])
@require_auth
def upload_attachment():
    """Upload a file for tracked sharing. Returns attachment_id and view URL."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    filename = secure_filename(file.filename)
    if not filename:
        return jsonify({"error": "Invalid filename"}), 400

    if not is_allowed_file(filename):
        return jsonify({"error": f"File type not allowed. Supported: {', '.join(sorted(ALLOWED_EXTENSIONS))}"}), 400

    content_type = mime_type_for(filename)
    head = file.stream.read(1024)
    file.stream.seek(0)
    if not has_valid_signature(head, content_type):
        return jsonify({"error": "File contents do not match its extension"}), 400

    attachment_id = uuid.uuid4().hex
    ext = Path(filename).suffix.lower()
    stored_name = f"{attachment_id}{ext}"
    file_path = upload_dir() / stored_name

    file.save(str(file_path))
    file_size = file_path.stat().st_size

    label = request.form.get("label", filename)
    sender_ip = get_client_ip()

    conn = get_db()
    conn.execute(
        """INSERT INTO attachments (id, email_tracking_id, filename, content_type, file_size, file_path, label, created_at, sender_ip)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (attachment_id, None, filename, content_type, file_size, stored_name, label, utcnow_iso(), sender_ip),
    )
    conn.commit()

    base_url = request.url_root.rstrip("/")
    view_url = f"{base_url}/view/{attachment_id}"
    file_url = f"{base_url}/file/{attachment_id}"

    return jsonify({
        "success": True,
        "attachment_id": attachment_id,
        "filename": filename,
        "file_size": file_size,
        "file_size_display": format_file_size(file_size),
        "content_type": content_type,
        "view_url": view_url,
        "file_url": file_url,
    })


@app.route("/view/<attachment_id>")
def view_attachment(attachment_id):
    """Branded viewer page for a tracked attachment.

    Loading the page does not count as a view: link scanners and preview bots fetch it too.
    The page's script posts to view_beacon once it has been visible for a moment.
    """
    conn = get_db()
    attachment = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    if not attachment:
        return "<h1>File not found</h1><p>This attachment may have been deleted.</p>", 404

    att_dict = dict(attachment)
    att_dict["file_size_display"] = format_file_size(att_dict.get("file_size"))
    mimetype = mime_type_for(att_dict.get("filename"))
    is_pdf = mimetype == "application/pdf"
    is_image = mimetype in INLINE_TYPES and mimetype.startswith("image/")

    base_url = request.url_root.rstrip("/")
    file_url = f"{base_url}/file/{attachment_id}"

    return render_template(
        "view_attachment.html",
        attachment=att_dict,
        file_url=file_url,
        is_pdf=is_pdf,
        is_image=is_image,
        beacon_url=url_for("view_beacon", attachment_id=attachment_id),
        beacon_token=_beacon_serializer().dumps(attachment_id),
    )


def _beacon_serializer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="view-beacon")


@app.route("/view/<attachment_id>/seen", methods=["POST"])
def view_beacon(attachment_id):
    """Record a document view. Requires the signed token embedded in a recently rendered viewer page."""
    try:
        token_id = _beacon_serializer().loads(request.form.get("token", ""), max_age=VIEW_BEACON_MAX_AGE)
    except BadSignature:
        return jsonify({"error": "Invalid or expired view token"}), 400
    if token_id != attachment_id:
        return jsonify({"error": "Invalid or expired view token"}), 400

    client_ip = get_client_ip()
    user_agent = request.headers.get("User-Agent", "")
    now = utcnow()

    conn = get_db()
    attachment = conn.execute(
        "SELECT id, sender_ip, created_at FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()
    if attachment:
        self_view = is_ignored_ip(conn, client_ip) or is_sender_ip(attachment, client_ip, now)
        conn.execute(
            "INSERT INTO attachment_views (attachment_id, viewed_at, ip, user_agent, is_self, open_kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (attachment_id, now.isoformat(), truncate_ip(client_ip), user_agent, 1 if self_view else 0,
             classify_open(client_ip, user_agent)),
        )
        conn.commit()
    return "", 204


@app.route("/file/<attachment_id>")
def serve_file(attachment_id):
    """Serve the raw file for download or inline viewing."""
    conn = get_db()
    attachment = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()

    if not attachment:
        return "File not found", 404

    file_path = upload_dir() / attachment["file_path"]
    if not file_path.exists():
        return "File missing from server", 404

    # Recompute the type: rows uploaded before server-side detection hold a client-supplied value.
    mimetype = mime_type_for(attachment["filename"])
    inline = mimetype in INLINE_TYPES and request.args.get("dl", "") != "1"

    resp = send_from_directory(
        str(upload_dir()),
        attachment["file_path"],
        mimetype=mimetype,
        as_attachment=not inline,
        download_name=attachment["filename"],
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = FILE_CSP
    return resp


@app.route("/api/attachment-status/<attachment_id>")
@require_auth
def attachment_status(attachment_id):
    """JSON view stats for a tracked attachment."""
    conn = get_db()
    attachment = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    if not attachment:
        return jsonify({"error": "Attachment not found"}), 404
    views = conn.execute(
        "SELECT * FROM attachment_views WHERE attachment_id = ? ORDER BY viewed_at DESC",
        (attachment_id,),
    ).fetchall()

    summary = summarize_attachment(attachment, views)
    return jsonify({
        "attachment_id": attachment_id,
        "filename": attachment["filename"],
        "viewed": summary["view_count"] > 0,
        "view_count": summary["view_count"],
        "automated_view_count": summary["automated_view_count"],
        "self_view_count": summary["self_view_count"],
        "views": summary["views"],
    })


def delete_attachments(conn, attachment_rows):
    """Delete attachment rows (needing id and file_path), their views, and their files on disk."""
    for att in attachment_rows:
        path = upload_dir() / att["file_path"]
        try:
            path.unlink(missing_ok=True)
        except OSError:
            app.logger.warning("Could not delete upload %s", path, exc_info=True)
        conn.execute("DELETE FROM attachment_views WHERE attachment_id = ?", (att["id"],))
        conn.execute("DELETE FROM attachments WHERE id = ?", (att["id"],))


def delete_emails(conn, tracking_ids):
    """Delete emails with their opens and linked attachments."""
    for tid in tracking_ids:
        delete_attachments(conn, conn.execute(
            "SELECT id, file_path FROM attachments WHERE email_tracking_id = ?", (tid,)
        ).fetchall())
        conn.execute("DELETE FROM opens WHERE tracking_id = ?", (tid,))
        conn.execute("DELETE FROM emails WHERE id = ?", (tid,))


@app.route("/api/delete-attachment", methods=["POST"])
@require_auth
def api_delete_attachment():
    """Delete a tracked attachment, its file, and all view records."""
    data = request.get_json(silent=True) or request.form
    attachment_id = (data.get("attachment_id") or "").strip()
    if not attachment_id:
        return jsonify({"error": "Attachment ID is required"}), 400

    conn = get_db()
    delete_attachments(conn, conn.execute(
        "SELECT id, file_path FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchall())
    conn.commit()

    return jsonify({"success": True, "attachment_id": attachment_id})


# ─── Dashboard ─────────────────────────────────────────────────────────

def summarize_attachment(attachment, view_rows):
    """Attachment row plus its views, split into counted / automated / self."""
    summary = dict(attachment)
    counted, automated, own = split_events(view_rows)
    summary["file_size_display"] = format_file_size(summary.get("file_size"))
    summary["views"] = counted
    summary["view_count"] = len(counted)
    summary["automated_view_count"] = len(automated)
    summary["self_view_count"] = len(own)
    summary["last_viewed_at"] = counted[0]["viewed_at"] if counted else None
    return summary


def summarize_email(email, open_rows, attachments):
    """Email row plus its opens (newest first) and already-summarized attachments."""
    counted, automated, own = split_events(open_rows)
    return {
        "id": email["id"],
        "label": email["label"],
        "subject": email["subject"] if email["subject"] is not None else split_label(email["label"])[0],
        "recipients": email["recipients"] if email["recipients"] is not None else split_label(email["label"])[1],
        "created_at": email["created_at"],
        "open_count": len(counted),
        "automated_open_count": len(automated),
        "self_open_count": len(own),
        "last_opened_at": counted[0]["opened_at"] if counted else None,
        "last_client": counted[0]["client_name"] if counted else None,
        "opens": counted,  # Only recipient opens appear in the main timeline
        "automated_opens": automated,
        "attachments": attachments,
        "attachment_count": len(attachments),
        "attachment_views": sum(a["view_count"] for a in attachments),
    }


DASHBOARD_PAGE_SIZE = 25
STANDALONE_ATTACHMENT_LIMIT = 50
DASHBOARD_STATUSES = ("all", "opened", "waiting")

# SQL equivalent of split_events(): a recipient open/view that counts.
COUNTED_EVENT_SQL = "is_self = 0 AND COALESCE(open_kind, 'human') IN ({})".format(
    ", ".join(f"'{kind}'" for kind in sorted(COUNTED_OPEN_KINDS))
)
EMAIL_OPENED_SQL = f"EXISTS (SELECT 1 FROM opens o WHERE o.tracking_id = e.id AND {COUNTED_EVENT_SQL})"


def _in_clause(values):
    return ",".join("?" * len(values))


def _group_by(rows, key):
    grouped = {}
    for row in rows:
        grouped.setdefault(row[key], []).append(row)
    return grouped


def _summarize_attachments(conn, attachment_rows):
    """Summaries for many attachments with a single query for all of their views."""
    ids = [a["id"] for a in attachment_rows]
    views = _group_by(conn.execute(
        f"SELECT * FROM attachment_views WHERE attachment_id IN ({_in_clause(ids)}) ORDER BY viewed_at DESC", ids
    ).fetchall(), "attachment_id") if ids else {}
    return [summarize_attachment(a, views.get(a["id"], [])) for a in attachment_rows]


def dashboard_stats(conn):
    """Totals across every tracked email, independent of the current page and filters."""
    count = lambda sql, *args: conn.execute(sql, args).fetchone()[0]  # noqa: E731
    total_emails = count("SELECT COUNT(*) FROM emails")
    opened_emails = count(f"SELECT COUNT(*) FROM emails e WHERE {EMAIL_OPENED_SQL}")
    return {
        "total_emails": total_emails,
        "opened_emails": opened_emails,
        "unopened_emails": total_emails - opened_emails,
        "total_opens": count(f"SELECT COUNT(*) FROM opens WHERE {COUNTED_EVENT_SQL}"),
        "total_automated_opens": count(f"SELECT COUNT(*) FROM opens WHERE is_self = 0 AND NOT ({COUNTED_EVENT_SQL})"),
        "total_self_opens": count("SELECT COUNT(*) FROM opens WHERE is_self = 1"),
        "open_rate": round(opened_emails / total_emails * 100) if total_emails else 0,
        "total_attachments": count("SELECT COUNT(*) FROM attachments"),
        "total_attachment_views": count(f"SELECT COUNT(*) FROM attachment_views WHERE {COUNTED_EVENT_SQL}"),
    }


def dashboard_page(conn, page=1, status="all", query=""):
    """One page of emails (newest first) matching the filters, with opens and attachments loaded in bulk."""
    where, params = [], []
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("e.label LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")
    if status == "opened":
        where.append(EMAIL_OPENED_SQL)
    elif status == "waiting":
        where.append(f"NOT {EMAIL_OPENED_SQL}")
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    total = conn.execute(f"SELECT COUNT(*) FROM emails e {where_sql}", params).fetchone()[0]
    page_count = max(1, -(-total // DASHBOARD_PAGE_SIZE))
    page = min(max(page, 1), page_count)
    emails = conn.execute(
        f"SELECT * FROM emails e {where_sql} ORDER BY e.created_at DESC LIMIT ? OFFSET ?",
        [*params, DASHBOARD_PAGE_SIZE, (page - 1) * DASHBOARD_PAGE_SIZE],
    ).fetchall()

    ids = [e["id"] for e in emails]
    opens, attachments = {}, {}
    if ids:
        opens = _group_by(conn.execute(
            f"SELECT * FROM opens WHERE tracking_id IN ({_in_clause(ids)}) ORDER BY opened_at DESC", ids
        ).fetchall(), "tracking_id")
        attachment_rows = conn.execute(
            f"SELECT * FROM attachments WHERE email_tracking_id IN ({_in_clause(ids)}) ORDER BY created_at DESC", ids
        ).fetchall()
        attachments = _group_by(_summarize_attachments(conn, attachment_rows), "email_tracking_id")

    results = [summarize_email(e, opens.get(e["id"], []), attachments.get(e["id"], [])) for e in emails]
    return results, {"page": page, "page_count": page_count, "total": total}


@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    status = request.args.get("status", "all")
    if status not in DASHBOARD_STATUSES:
        status = "all"
    query = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)

    results, pagination = dashboard_page(conn, page, status, query)
    standalone_attachments = _summarize_attachments(conn, conn.execute(
        "SELECT * FROM attachments WHERE email_tracking_id IS NULL ORDER BY created_at DESC LIMIT ?",
        (STANDALONE_ATTACHMENT_LIMIT,),
    ).fetchall())

    ignored_rows = conn.execute("SELECT * FROM ignored_ips ORDER BY created_at DESC").fetchall()
    client_ip = get_client_ip()

    # Detect if running on localhost/loopback
    is_localhost = client_ip in LOCALHOST_ALIASES or "localhost" in request.host or "127.0.0.1" in request.host

    return render_template(
        "dashboard.html",
        emails=results,
        standalone_attachments=standalone_attachments,
        client_ip=client_ip,
        is_client_ignored=is_ignored_ip(conn, client_ip),
        ignored_ips=[dict(r) for r in ignored_rows],
        env_ips=env_ignored_ips(),
        stats=dashboard_stats(conn),
        filters={"status": status, "q": query},
        pagination=pagination,
        is_localhost=is_localhost,
        host_url=request.host_url.rstrip("/"),
    )


# ─── Existing API endpoints ───────────────────────────────────────────

@app.route("/api/status/<tracking_id>")
@require_auth
def status(tracking_id):
    """JSON status for a single tracking id. Only recipient opens (COUNTED_OPEN_KINDS) count as opened."""
    conn = get_db()
    opens = conn.execute(
        "SELECT * FROM opens WHERE tracking_id = ? ORDER BY opened_at DESC",
        (tracking_id,),
    ).fetchall()

    counted, automated, own = split_events(opens)
    return jsonify(
        {
            "tracking_id": tracking_id,
            "opened": len(counted) > 0,
            "open_count": len(counted),
            "automated_open_count": len(automated),
            "self_open_count": len(own),
            "opens": counted,
            "automated_opens": automated,
            "all_opens": sorted(counted + automated + own, key=lambda o: o["opened_at"], reverse=True),
        }
    )


@app.route("/api/self-view", methods=["POST"])
@require_auth
def api_self_view():
    """The extension saw the sender viewing these messages in Gmail. Gmail-proxy fetches within
    SELF_VIEW_WINDOW of now (before or after this report arrives) are the sender's own."""
    data = request.get_json(silent=True) or {}
    tracking_ids = data.get("tracking_ids") or []
    if not isinstance(tracking_ids, list):
        return jsonify({"error": "tracking_ids must be a list"}), 400

    now = utcnow()
    conn = get_db()
    marked = 0
    for tid in tracking_ids[:50]:
        if not isinstance(tid, str):
            continue
        marked += conn.execute(
            "UPDATE opens SET is_self = 1 WHERE tracking_id = ? AND open_kind = 'proxy' AND opened_at >= ?",
            (tid, (now - SELF_VIEW_WINDOW).isoformat()),
        ).rowcount
        conn.execute(
            "UPDATE emails SET suppress_until = ? WHERE id = ?", ((now + SELF_VIEW_WINDOW).isoformat(), tid)
        )
    conn.commit()
    return jsonify({"success": True, "marked_opens": marked})


@app.route("/api/my-ip")
@require_auth
def api_my_ip():
    """Return client's detected IP and whether it is currently filtered."""
    ip = get_client_ip()
    conn = get_db()
    ignored = is_ignored_ip(conn, ip)
    return jsonify(
        {
            "ip": ip,
            "is_ignored": ignored,
        }
    )


@app.route("/api/ignored-ips", methods=["GET", "POST"])
@require_auth
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
                (ip, label, utcnow_iso()),
            )
        conn.commit()

        return jsonify({"success": True, "ip": ip, "action": action})

    # GET
    conn = get_db()
    rows = conn.execute("SELECT * FROM ignored_ips ORDER BY created_at DESC").fetchall()

    return jsonify(
        {
            "current_ip": get_client_ip(),
            "ignored_ips": [dict(r) for r in rows],
            "env_ignored_ips": env_ignored_ips(),
        }
    )


@app.route("/api/delete-email", methods=["POST"])
@require_auth
def api_delete_email():
    """Delete a tracked email and all associated open events and attachments."""
    data = request.get_json(silent=True) or request.form
    tracking_id = (data.get("tracking_id") or "").strip()
    if not tracking_id:
        return jsonify({"error": "Tracking ID is required"}), 400

    conn = get_db()
    delete_emails(conn, [tracking_id])
    conn.commit()

    return jsonify({"success": True, "tracking_id": tracking_id})


@app.route("/api/clear-all", methods=["POST"])
@require_auth
def api_clear_all():
    """Clear all tracked emails, opens, attachments and their files."""
    conn = get_db()
    delete_emails(conn, [r["id"] for r in conn.execute("SELECT id FROM emails").fetchall()])
    delete_attachments(conn, conn.execute("SELECT id, file_path FROM attachments").fetchall())
    conn.execute("DELETE FROM opens")
    conn.execute("DELETE FROM attachment_views")
    conn.commit()
    return jsonify({"success": True})


# ─── Retention ─────────────────────────────────────────────────────────

def purge_expired(now=None):
    """Apply the retention policy and finish truncating sender IPs whose self-open TTL has passed."""
    now = now or utcnow()
    conn = connect()
    try:
        if RETENTION_DAYS > 0:
            cutoff = (now - timedelta(days=RETENTION_DAYS)).isoformat()
            delete_emails(conn, [r["id"] for r in conn.execute(
                "SELECT id FROM emails WHERE created_at < ?", (cutoff,)
            ).fetchall()])
            delete_attachments(conn, conn.execute(
                "SELECT id, file_path FROM attachments WHERE created_at < ?", (cutoff,)
            ).fetchall())
            conn.execute("DELETE FROM opens WHERE opened_at < ?", (cutoff,))
            conn.execute("DELETE FROM attachment_views WHERE viewed_at < ?", (cutoff,))

        # The sender IP is only needed in full while it can still mark self-opens.
        ttl_cutoff = (now - SENDER_IP_TTL).isoformat()
        for table in ("emails", "attachments"):
            rows = conn.execute(
                f"SELECT id, sender_ip FROM {table} WHERE created_at < ? AND sender_ip IS NOT NULL "
                "AND sender_ip NOT LIKE '%.0' AND sender_ip NOT LIKE '%::'",
                (ttl_cutoff,),
            ).fetchall()
            for row in rows:
                conn.execute(f"UPDATE {table} SET sender_ip = ? WHERE id = ?", (truncate_ip(row["sender_ip"]), row["id"]))
        conn.commit()
    finally:
        conn.close()


_last_purge = None


@app.before_request
def _purge_periodically():
    global _last_purge
    now = utcnow()
    if _last_purge is None or now - _last_purge >= PURGE_INTERVAL:
        _last_purge = now
        try:
            purge_expired(now)
        except sqlite3.Error:
            app.logger.exception("Retention purge failed")


# Create/migrate the database on import (Gunicorn startup). Runs last so every helper exists.
init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
