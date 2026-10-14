import io
from contextlib import closing
from datetime import timedelta
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

# Must be set before importing app: CORS and the data directory are configured at import time,
# and app.py runs init_db() on import. Pointing it at a temp dir keeps the real tracker.db untouched.
EXTENSION_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
IMPORT_DATA_DIR = tempfile.mkdtemp()
os.environ["MAILSEEN_CORS_ORIGINS"] = EXTENSION_ORIGIN
os.environ["MAILSEEN_DATA_DIR"] = IMPORT_DATA_DIR
os.environ.pop("MAILSEEN_DB_PATH", None)
os.environ.pop("MAILSEEN_UPLOAD_DIR", None)

import app  # noqa: E402

IMPORT_CONFIG = {"DB_PATH": app.app.config["DB_PATH"], "UPLOAD_DIR": app.app.config["UPLOAD_DIR"]}

API_TOKEN = "test-api-token"
PASSWORD = "test-dashboard-password"
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}

CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
GMAIL_PROXY_UA = "Mozilla/5.0 (Windows NT 5.1; rv:11.0) Gecko Firefox/11.0 (via ggpht.com GoogleImageProxy)"
APPLE_MPP_UA = "Mozilla/5.0"

PDF_BYTES =b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
PNG_BYTES = app.PIXEL_BYTES


class BackendTestCase(unittest.TestCase):
    """Runs every test against a throwaway database and upload directory."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        app.app.config.update(
            TESTING=True,
            DB_PATH=self.tmpdir / "tracker.db",
            UPLOAD_DIR=self.tmpdir / "uploads",
            API_TOKEN=API_TOKEN,
            DASHBOARD_PASSWORD=PASSWORD,
            SESSION_COOKIE_SECURE=False,
        )
        app.init_db()
        self.client = app.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @property
    def upload_dir(self):
        return app.upload_dir()

    # Helpers
    def new_tracker(self, label="Test", age_seconds=60, **kwargs):
        """Create a tracker, backdated so opens aren't treated as delivery-time scanner fetches."""
        res = self.client.post("/new", json={"label": label, **kwargs}, headers=AUTH)
        self.assertEqual(res.status_code, 200, res.data)
        data = res.get_json()
        self.backdate(data["tracking_id"], age_seconds)
        return data

    def backdate(self, tracking_id, seconds):
        created = (app.utcnow() - timedelta(seconds=seconds)).isoformat()
        with closing(app.connect()) as conn:
            conn.execute("UPDATE emails SET created_at = ? WHERE id = ?", (created, tracking_id))
            conn.commit()

    def open_pixel(self, tracking_id, ip="203.0.113.99", ua=CHROME_UA, method="GET", headers=None):
        return self.client.open(
            f"/track/{tracking_id}.png", method=method,
            environ_overrides={"REMOTE_ADDR": ip}, headers={"User-Agent": ua, **(headers or {})},
        )

    def status(self, tracking_id):
        return self.client.get(f"/api/status/{tracking_id}", headers=AUTH).get_json()

    def upload(self, name, data, content_type="application/octet-stream", headers=AUTH):
        return self.client.post(
            "/api/upload-attachment",
            data={"file": (io.BytesIO(data), name, content_type)},
            content_type="multipart/form-data",
            headers=headers,
        )

    def get_file(self, url):
        res = self.client.get(url)
        res.close()  # release the streamed file handle; headers stay readable
        return res

    def login(self, password=PASSWORD, next_path=None):
        url = "/login" + (f"?next={next_path}" if next_path else "")
        return self.client.post(url, data={"password": password})

    def csrf_token(self):
        with self.client.session_transaction() as sess:
            return sess.get("csrf_token")


class TestCoreBehaviour(BackendTestCase):
    def test_new_endpoint_post(self):
        data = self.new_tracker("Meeting Notes -> alice@example.com")
        self.assertEqual(data["label"], "Meeting Notes -> alice@example.com")
        self.assertIn("tracking_id", data)
        self.assertIn("pixel_url", data)
        self.assertIn("disclosure_html", data)

        # Form-encoded POST is still accepted
        res = self.client.post("/new", data={"label": "Form label"}, headers=AUTH)
        self.assertEqual(res.get_json()["label"], "Form label")

    def test_self_open_filtering(self):
        tid = self.new_tracker("Self Open Test")["tracking_id"]
        self.client.post("/api/ignored-ips", json={"ip": "198.51.100.42", "action": "add"}, headers=AUTH)

        res_pixel = self.open_pixel(tid, ip="198.51.100.42")
        self.assertEqual(res_pixel.status_code, 200)
        self.assertEqual(res_pixel.mimetype, "image/png")

        status = self.client.get(f"/api/status/{tid}", headers=AUTH).get_json()
        self.assertFalse(status["opened"])
        self.assertEqual(status["open_count"], 0)
        self.assertEqual(status["self_open_count"], 1)

        self.open_pixel(tid, ip="203.0.113.99")
        status = self.client.get(f"/api/status/{tid}", headers=AUTH).get_json()
        self.assertTrue(status["opened"])
        self.assertEqual(status["open_count"], 1)
        self.assertEqual(status["self_open_count"], 1)

    def test_dashboard_renders_when_logged_in(self):
        self.login()
        res = self.client.get("/dashboard")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"MailSeen", res.data)

    def test_delete_email(self):
        tid = self.new_tracker("Delete Test")["tracking_id"]
        res = self.client.post("/api/delete-email", json={"tracking_id": tid}, headers=AUTH)
        self.assertTrue(res.get_json()["success"])
        stat = self.client.get(f"/api/status/{tid}", headers=AUTH)
        self.assertEqual(stat.get_json()["open_count"], 0)

    def test_client_device_parsing(self):
        gmail_ua = "Mozilla/5.0 (Windows NT 5.1; rv:11.0) Gecko Firefox/11.0 (via ggpht.com GoogleImageProxy)"
        self.assertEqual(app.parse_client_device(gmail_ua), "Gmail (Google Image Proxy)")
        ios_ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"
        self.assertEqual(app.parse_client_device(ios_ua), "Apple Mail (iOS)")


class TestStorageConfig(BackendTestCase):
    def test_data_dir_env_controls_import_time_paths(self):
        self.assertEqual(IMPORT_CONFIG["DB_PATH"], Path(IMPORT_DATA_DIR) / "tracker.db")
        self.assertEqual(IMPORT_CONFIG["UPLOAD_DIR"], Path(IMPORT_DATA_DIR) / "uploads")
        # init_db() ran on import against the env-configured location, not the repo's tracker.db
        self.assertTrue((Path(IMPORT_DATA_DIR) / "tracker.db").exists())

    def test_requests_use_configured_paths(self):
        self.new_tracker()
        aid = self.upload("doc.pdf", PDF_BYTES).get_json()["attachment_id"]
        self.assertTrue((self.tmpdir / "tracker.db").exists())
        self.assertTrue(any(p.name.startswith(aid) for p in (self.tmpdir / "uploads").iterdir()))

    def test_init_db_creates_missing_directories(self):
        nested = self.tmpdir / "a" / "b"
        app.app.config.update(DB_PATH=nested / "tracker.db", UPLOAD_DIR=nested / "files")
        app.init_db()
        self.assertTrue((nested / "tracker.db").exists())
        self.assertTrue((nested / "files").is_dir())


class TestAuth(BackendTestCase):
    PROTECTED = [
        ("POST", "/new"),
        ("POST", "/api/upload-attachment"),
        ("GET", "/api/status/abc"),
        ("GET", "/api/attachment-status/abc"),
        ("GET", "/api/my-ip"),
        ("GET", "/api/ignored-ips"),
        ("POST", "/api/ignored-ips"),
        ("POST", "/api/delete-attachment"),
        ("POST", "/api/delete-email"),
        ("POST", "/api/clear-all"),
    ]

    def test_protected_endpoints_reject_anonymous_requests(self):
        for method, path in self.PROTECTED:
            with self.subTest(method=method, path=path):
                res = self.client.open(path, method=method, json={})
                self.assertEqual(res.status_code, 401)

    def test_protected_endpoints_reject_wrong_token(self):
        for method, path in self.PROTECTED:
            with self.subTest(method=method, path=path):
                res = self.client.open(path, method=method, json={}, headers={"Authorization": "Bearer nope"})
                self.assertEqual(res.status_code, 401)

    def test_no_token_configured_means_no_access(self):
        app.app.config["API_TOKEN"] = ""
        res = self.client.post("/new", json={}, headers={"Authorization": "Bearer "})
        self.assertEqual(res.status_code, 401)

    def test_clear_all_unauthenticated_does_not_delete(self):
        tid = self.new_tracker()["tracking_id"]
        self.assertEqual(self.client.post("/api/clear-all").status_code, 401)
        conn = app.connect()
        self.assertIsNotNone(conn.execute("SELECT 1 FROM emails WHERE id = ?", (tid,)).fetchone())
        conn.close()

    def test_new_is_post_only(self):
        res = self.client.get("/new?label=x", headers=AUTH)
        self.assertEqual(res.status_code, 405)

    def test_public_endpoints_stay_public(self):
        tid = self.new_tracker()["tracking_id"]
        self.assertEqual(self.client.get(f"/track/{tid}.png").status_code, 200)
        self.assertEqual(self.client.get("/login").status_code, 200)

    def test_login_returns_to_the_same_page_and_filters(self):
        res = self.client.get("/dashboard?page=2&status=opened")
        self.assertEqual(self.client.post(res.headers["Location"], data={"password": PASSWORD}).headers["Location"],
                         "/dashboard?page=2&status=opened")

    def test_dashboard_redirects_to_login(self):
        res = self.client.get("/dashboard")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/login?next=/dashboard", res.headers["Location"])

    def test_login_wrong_password(self):
        res = self.login("wrong")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(self.client.get("/dashboard").status_code, 302)

    def test_login_fails_closed_when_no_password_configured(self):
        app.app.config["DASHBOARD_PASSWORD"] = ""
        res = self.login("")
        self.assertEqual(res.status_code, 401)
        self.assertIn(b"not configured", res.data)

    def test_login_redirect_is_local_only(self):
        res = self.login(next_path="//evil.example.com/")
        self.assertEqual(res.headers["Location"], "/dashboard")
        self.client.post("/logout", data={"csrf_token": self.csrf_token()})
        res = self.login(next_path="https://evil.example.com/")
        self.assertEqual(res.headers["Location"], "/dashboard")

    def test_session_writes_require_csrf(self):
        self.login()
        # Reads work with the session alone
        self.assertEqual(self.client.get("/api/ignored-ips").status_code, 200)
        # Writes need the CSRF header
        res = self.client.post("/api/ignored-ips", json={"ip": "192.0.2.1"})
        self.assertEqual(res.status_code, 403)
        res = self.client.post(
            "/api/ignored-ips", json={"ip": "192.0.2.1"}, headers={"X-CSRF-Token": self.csrf_token()}
        )
        self.assertEqual(res.status_code, 200)

    def test_logout(self):
        self.login()
        self.assertEqual(self.client.post("/logout").status_code, 403)  # no CSRF token
        res = self.client.post("/logout", data={"csrf_token": self.csrf_token()})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(self.client.get("/dashboard").status_code, 302)


class TestCors(BackendTestCase):
    def preflight(self, path, origin):
        return self.client.options(
            path, headers={"Origin": origin, "Access-Control-Request-Method": "POST"}
        )

    def test_configured_origin_allowed_on_api_routes(self):
        for path in ("/new", "/api/status/abc"):
            with self.subTest(path=path):
                res = self.preflight(path, EXTENSION_ORIGIN)
                self.assertEqual(res.headers.get("Access-Control-Allow-Origin"), EXTENSION_ORIGIN)

    def test_other_origins_rejected(self):
        for origin in ("https://mail.google.com", "https://evil.example.com"):
            with self.subTest(origin=origin):
                res = self.preflight("/api/clear-all", origin)
                self.assertIsNone(res.headers.get("Access-Control-Allow-Origin"))

    def test_no_cors_on_non_api_routes(self):
        res = self.client.get("/login", headers={"Origin": EXTENSION_ORIGIN})
        self.assertIsNone(res.headers.get("Access-Control-Allow-Origin"))


class TestUploads(BackendTestCase):
    def test_oversized_upload_returns_json_error(self):
        original = app.app.config["MAX_CONTENT_LENGTH"]
        app.app.config["MAX_CONTENT_LENGTH"] = 512
        try:
            res = self.upload("big.pdf", PDF_BYTES + b"0" * 2048)
        finally:
            app.app.config["MAX_CONTENT_LENGTH"] = original
        self.assertEqual(res.status_code, 413)
        self.assertIn("too large", res.get_json()["error"])

    def test_svg_rejected(self):
        res = self.upload("evil.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
                          "image/svg+xml")
        self.assertEqual(res.status_code, 400)

    def test_mismatched_signature_rejected(self):
        res = self.upload("evil.png", b"<html><script>alert(1)</script></html>", "image/png")
        self.assertEqual(res.status_code, 400)
        res = self.upload("evil.pdf", b"<html><script>alert(1)</script></html>", "application/pdf")
        self.assertEqual(res.status_code, 400)

    def test_client_content_type_ignored(self):
        res = self.upload("doc.pdf", PDF_BYTES, "text/html")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["content_type"], "application/pdf")

        served = self.get_file(f"/file/{data['attachment_id']}")
        self.assertEqual(served.mimetype, "application/pdf")

    def test_served_files_are_sandboxed(self):
        aid = self.upload("doc.pdf", PDF_BYTES).get_json()["attachment_id"]
        res = self.get_file(f"/file/{aid}")
        self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", res.headers["Content-Security-Policy"])
        self.assertIn("default-src 'none'", res.headers["Content-Security-Policy"])

    def test_pdf_and_images_inline_others_download(self):
        cases = [
            ("doc.pdf", PDF_BYTES, "inline"),
            ("pic.png", PNG_BYTES, "inline"),
            ("notes.txt", b"<script>alert(1)</script>", "attachment"),
            ("data.csv", b"a,b\n1,2\n", "attachment"),
            ("sheet.zip", b"PK\x03\x04", "attachment"),
        ]
        for name, body, disposition in cases:
            with self.subTest(name=name):
                aid = self.upload(name, body).get_json()["attachment_id"]
                res = self.get_file(f"/file/{aid}")
                self.assertTrue(res.headers["Content-Disposition"].startswith(disposition))

    def test_dl_param_forces_download(self):
        aid = self.upload("doc.pdf", PDF_BYTES).get_json()["attachment_id"]
        res = self.get_file(f"/file/{aid}?dl=1")
        self.assertTrue(res.headers["Content-Disposition"].startswith("attachment"))

    def test_legacy_rows_use_server_side_type(self):
        # Rows written before this fix stored the client's Content-Type, and SVG was allowed.
        (self.upload_dir / "legacy.svg").write_bytes(b"<svg><script>alert(1)</script></svg>")
        conn = app.connect()
        conn.execute(
            "INSERT INTO attachments (id, filename, content_type, file_size, file_path, label, created_at) "
            "VALUES ('legacy', 'x.svg', 'text/html', 10, 'legacy.svg', 'x', '2026-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()

        res = self.get_file("/file/legacy")
        self.assertEqual(res.mimetype, "application/octet-stream")
        self.assertTrue(res.headers["Content-Disposition"].startswith("attachment"))
        self.assertIn("sandbox", res.headers["Content-Security-Policy"])


class TestProxyFix(BackendTestCase):
    def test_spoofed_forwarded_for_ignored(self):
        # The proxy appends the real peer address; anything to its left is client-supplied.
        res = self.client.get(
            "/api/my-ip",
            headers={**AUTH, "X-Forwarded-For": "6.6.6.6, 203.0.113.5"},
        )
        self.assertEqual(res.get_json()["ip"], "203.0.113.5")

    def test_spoofed_ip_cannot_fake_self_open(self):
        tid = self.new_tracker()["tracking_id"]
        self.client.post("/api/ignored-ips", json={"ip": "198.51.100.42"}, headers=AUTH)
        self.open_pixel(tid, ip="127.0.0.1", headers={"X-Forwarded-For": "198.51.100.42, 203.0.113.77"})
        status = self.client.get(f"/api/status/{tid}", headers=AUTH).get_json()
        self.assertEqual(status["open_count"], 1)
        # The real peer (203.0.113.77, stored truncated) was used, not the spoofed 198.51.100.42
        self.assertEqual(status["opens"][0]["ip"], "203.0.113.0")


class TestAttachmentOwnership(BackendTestCase):
    def test_linked_attachment_cannot_be_reassigned(self):
        aid = self.upload("doc.pdf", PDF_BYTES).get_json()["attachment_id"]

        first = self.new_tracker("First", attachment_ids=[aid])
        self.assertEqual(first["linked_attachment_ids"], [aid])

        second = self.new_tracker("Second", attachment_ids=[aid, "does-not-exist", 123])
        self.assertEqual(second["linked_attachment_ids"], [])

        conn = app.connect()
        row = conn.execute("SELECT email_tracking_id FROM attachments WHERE id = ?", (aid,)).fetchone()
        conn.close()
        self.assertEqual(row["email_tracking_id"], first["tracking_id"])

    def test_attachment_ids_must_be_a_list(self):
        res = self.client.post("/new", json={"attachment_ids": "abc"}, headers=AUTH)
        self.assertEqual(res.status_code, 400)


class TestOpenClassification(BackendTestCase):
    def test_classify_open(self):
        cases = [
            ("203.0.113.9", CHROME_UA, 60, "GET", "human"),
            ("66.249.84.1", GMAIL_PROXY_UA, 60, "GET", "proxy"),
            ("203.0.113.9", APPLE_MPP_UA, 60, "GET", "apple_mpp"),
            ("17.58.63.10", CHROME_UA, 60, "GET", "apple_mpp"),
            ("203.0.113.9", "curl/8.4.0", 60, "GET", "scanner"),
            ("203.0.113.9", "Mozilla/5.0 (compatible; Barracuda Sentinel)", 60, "GET", "scanner"),
            ("203.0.113.9", "", 60, "GET", "scanner"),
            ("203.0.113.9", CHROME_UA, 60, "HEAD", "scanner"),
            ("203.0.113.9", CHROME_UA, 1, "GET", "scanner"),  # delivery-time fetch
            ("not-an-ip", CHROME_UA, None, "GET", "human"),
        ]
        for ip, ua, seconds, method, expected in cases:
            with self.subTest(ip=ip, ua=ua, seconds=seconds, method=method):
                self.assertEqual(app.classify_open(ip, ua, seconds, method), expected)

    def test_only_human_and_proxy_count_as_opened(self):
        tid = self.new_tracker()["tracking_id"]
        self.open_pixel(tid, ua=APPLE_MPP_UA)
        self.open_pixel(tid, ua="python-requests/2.31")
        self.open_pixel(tid, method="HEAD")
        status = self.status(tid)
        self.assertFalse(status["opened"])
        self.assertEqual(status["automated_open_count"], 3)
        self.assertEqual({o["open_kind"] for o in status["automated_opens"]}, {"apple_mpp", "scanner"})

        self.open_pixel(tid, ip="66.249.84.1", ua=GMAIL_PROXY_UA)
        status = self.status(tid)
        self.assertTrue(status["opened"])
        self.assertEqual(status["opens"][0]["open_kind"], "proxy")

    def test_open_right_after_send_is_not_counted(self):
        tid = self.new_tracker(age_seconds=0)["tracking_id"]
        self.open_pixel(tid)
        self.assertEqual(self.status(tid)["automated_opens"][0]["open_kind"], "scanner")

    def test_dashboard_shows_counts_by_kind(self):
        tid = self.new_tracker("Kinds -> bob@example.com")["tracking_id"]
        self.open_pixel(tid, ua=APPLE_MPP_UA)
        self.open_pixel(tid)
        self.login()
        res = self.client.get("/dashboard")
        self.assertIn(b"1 automated", res.data)


class TestSenderIpTtl(BackendTestCase):
    def test_new_no_longer_grows_global_ignore_list(self):
        self.client.post("/new", json={"label": "x"}, headers=AUTH, environ_overrides={"REMOTE_ADDR": "198.51.100.7"})
        rows = self.client.get("/api/ignored-ips", headers=AUTH).get_json()["ignored_ips"]
        self.assertEqual(rows, [])

    def test_sender_ip_is_self_for_its_email_within_ttl(self):
        sender = "198.51.100.7"
        res = self.client.post("/new", json={"label": "a"}, headers=AUTH, environ_overrides={"REMOTE_ADDR": sender})
        tid = res.get_json()["tracking_id"]
        self.backdate(tid, 60)
        self.open_pixel(tid, ip=sender)
        self.assertEqual(self.status(tid)["self_open_count"], 1)


class TestSenderDevices(BackendTestCase):
    """Every network the sender uses the extension or dashboard from is "you" for SELF_DEVICE_TTL."""

    SENDER = "198.51.100.7"

    def authed(self, path, ip=SENDER, method="POST", **kwargs):
        return self.client.open(path, method=method, headers=AUTH, environ_overrides={"REMOTE_ADDR": ip}, **kwargs)

    def test_sender_network_is_self_on_every_email_after_the_ttl(self):
        self.authed("/new", json={"label": "first"})
        tid = self.new_tracker("later", age_seconds=app.SENDER_IP_TTL.total_seconds() + 60)["tracking_id"]
        self.open_pixel(tid, ip=self.SENDER)
        self.open_pixel(tid, ip="203.0.113.60")  # a real recipient
        status = self.status(tid)
        self.assertEqual(status["self_open_count"], 1)
        self.assertEqual(status["open_count"], 1)

    def test_gmail_proxy_fetch_is_never_matched_to_a_device(self):
        self.authed("/api/heartbeat", ip="66.249.84.1")
        tid = self.new_tracker()["tracking_id"]
        self.open_pixel(tid, ip="66.249.84.1", ua=GMAIL_PROXY_UA)
        self.assertEqual(self.status(tid)["open_count"], 1)

    def test_heartbeat_records_the_network_and_requires_auth(self):
        self.assertEqual(self.client.post("/api/heartbeat").status_code, 401)
        res = self.authed("/api/heartbeat").get_json()
        self.assertEqual(res["ip"], self.SENDER)
        self.assertTrue(res["known_device"])
        tid = self.new_tracker()["tracking_id"]
        self.open_pixel(tid, ip=self.SENDER)
        self.assertEqual(self.status(tid)["self_open_count"], 1)

    def test_dashboard_visit_records_the_network(self):
        self.login()
        self.client.get("/dashboard", environ_overrides={"REMOTE_ADDR": "198.51.100.20"})
        tid = self.new_tracker()["tracking_id"]
        self.open_pixel(tid, ip="198.51.100.20")
        self.assertEqual(self.status(tid)["self_open_count"], 1)

    def test_ipv6_devices_match_on_their_64(self):
        self.authed("/api/heartbeat", ip="2001:db8:1:2::10")
        tid = self.new_tracker()["tracking_id"]
        self.open_pixel(tid, ip="2001:db8:1:2:aaaa:bbbb:cccc:1")  # same /64, new privacy address
        self.open_pixel(tid, ip="2001:db8:1:3::1")
        status = self.status(tid)
        self.assertEqual(status["self_open_count"], 1)
        self.assertEqual(status["open_count"], 1)

    def test_device_expires(self):
        self.authed("/api/heartbeat")
        stale = (app.utcnow() - app.SELF_DEVICE_TTL - timedelta(days=1)).isoformat()
        with closing(app.connect()) as conn:
            conn.execute("UPDATE sender_devices SET last_seen = ?", (stale,))
            conn.commit()
        tid = self.new_tracker()["tracking_id"]
        self.open_pixel(tid, ip=self.SENDER)
        self.assertEqual(self.status(tid)["open_count"], 1)
        app.purge_expired()
        with closing(app.connect()) as conn:
            remaining = conn.execute("SELECT network FROM sender_devices WHERE network = ?", (self.SENDER,)).fetchall()
        self.assertEqual(remaining, [])

    def test_known_device_writes_are_throttled(self):
        self.authed("/api/heartbeat")
        earlier = (app.utcnow() - timedelta(minutes=1)).isoformat()
        with closing(app.connect()) as conn:
            conn.execute("UPDATE sender_devices SET last_seen = ?", (earlier,))
            conn.commit()
        self.authed("/api/heartbeat")
        with closing(app.connect()) as conn:
            last_seen = conn.execute("SELECT last_seen FROM sender_devices WHERE network = ?", (self.SENDER,)).fetchone()[0]
        self.assertEqual(last_seen, earlier)

    def test_document_views_from_a_device_are_self(self):
        self.authed("/api/heartbeat")
        aid = self.upload("doc.pdf", PDF_BYTES).get_json()["attachment_id"]
        page = self.client.get(f"/view/{aid}").get_data(as_text=True)
        marker = 'data-beacon-token="'
        token = page[page.index(marker) + len(marker):].split('"', 1)[0]
        self.client.post(f"/view/{aid}/seen", data={"token": token},
                         environ_overrides={"REMOTE_ADDR": self.SENDER}, headers={"User-Agent": CHROME_UA})
        views = self.client.get(f"/api/attachment-status/{aid}", headers=AUTH).get_json()
        self.assertEqual((views["view_count"], views["self_view_count"]), (0, 1))

    def test_list_and_forget_devices(self):
        self.authed("/api/heartbeat")
        listing = self.authed("/api/devices", method="GET").get_json()
        self.assertEqual(listing["current_network"], self.SENDER)
        self.assertIn(self.SENDER, [d["network"] for d in listing["devices"]])
        self.assertEqual(self.authed("/api/devices", json={"action": "bogus"}).status_code, 400)
        self.authed("/api/devices", ip="203.0.113.1", json={"action": "remove", "network": self.SENDER})
        networks = [d["network"] for d in self.authed("/api/devices", ip="203.0.113.1", method="GET").get_json()["devices"]]
        self.assertNotIn(self.SENDER, networks)

    def test_disabled_with_zero_days(self):
        original = app.SELF_DEVICE_TTL
        app.SELF_DEVICE_TTL = timedelta(0)
        try:
            self.authed("/api/heartbeat")
            tid = self.new_tracker()["tracking_id"]
            self.open_pixel(tid, ip=self.SENDER)
            self.assertEqual(self.status(tid)["open_count"], 1)
        finally:
            app.SELF_DEVICE_TTL = original

    def test_migration_seeds_devices_from_untruncated_sender_ips(self):
        legacy = self.tmpdir / "legacy.db"
        recent = app.utcnow_iso()
        with closing(sqlite3.connect(legacy)) as conn:
            conn.executescript(f"""
                CREATE TABLE emails (id TEXT PRIMARY KEY, label TEXT, created_at TEXT, sender_ip TEXT);
                INSERT INTO emails VALUES ('e1', 'a', '{recent}', '198.51.100.7');
                INSERT INTO emails VALUES ('e2', 'b', '{recent}', '198.51.100.0');
            """)
        app.app.config["DB_PATH"] = legacy
        app.init_db()
        with closing(app.connect()) as conn:
            networks = {r[0] for r in conn.execute("SELECT network FROM sender_devices")}
        self.assertEqual(networks, {"198.51.100.7"})


class TestMarkSelf(BackendTestCase):
    def mark(self, payload):
        return self.client.post("/api/mark-self", json=payload, headers=AUTH)

    def test_mark_open_as_self_and_back(self):
        tid = self.new_tracker()["tracking_id"]
        self.open_pixel(tid)
        open_id = self.status(tid)["opens"][0]["id"]
        self.assertEqual(self.mark({"type": "open", "id": open_id}).status_code, 200)
        self.assertEqual((self.status(tid)["open_count"], self.status(tid)["self_open_count"]), (0, 1))
        self.mark({"type": "open", "id": open_id, "is_self": False})
        self.assertEqual(self.status(tid)["open_count"], 1)

    def test_validation(self):
        self.assertEqual(self.mark({"type": "open", "id": "1"}).status_code, 400)
        self.assertEqual(self.mark({"type": "nope", "id": 1}).status_code, 400)
        self.assertEqual(self.mark({"type": "open", "id": True}).status_code, 400)
        self.assertEqual(self.mark({"type": "view", "id": 999}).status_code, 404)
        self.assertEqual(self.client.post("/api/mark-self", json={"type": "open", "id": 1}).status_code, 401)

    def test_classification_is_not_rewritten_retroactively(self):
        tid = self.new_tracker()["tracking_id"]
        self.open_pixel(tid, ip="203.0.113.50")
        self.client.post("/api/ignored-ips", json={"ip": "203.0.113.50"}, headers=AUTH)
        # The dashboard and the API agree: the earlier open still counts
        self.assertEqual(self.status(tid)["open_count"], 1)
        self.login()
        self.assertIn(b'data-open-count="1"', self.client.get("/dashboard").data)


class TestSelfViewSuppression(BackendTestCase):
    def test_recent_proxy_opens_marked_self(self):
        tid = self.new_tracker()["tracking_id"]
        self.open_pixel(tid, ip="66.249.84.1", ua=GMAIL_PROXY_UA)
        self.assertEqual(self.status(tid)["open_count"], 1)

        res = self.client.post("/api/self-view", json={"tracking_ids": [tid]}, headers=AUTH)
        self.assertEqual(res.get_json()["marked_opens"], 1)
        status = self.status(tid)
        self.assertEqual(status["open_count"], 0)
        self.assertEqual(status["self_open_count"], 1)

    def test_proxy_opens_after_report_are_self_but_other_kinds_are_not(self):
        tid = self.new_tracker()["tracking_id"]
        self.client.post("/api/self-view", json={"tracking_ids": [tid]}, headers=AUTH)
        self.open_pixel(tid, ip="66.249.84.1", ua=GMAIL_PROXY_UA)
        self.open_pixel(tid, ip="203.0.113.60")  # a recipient opening in Outlook at the same moment
        status = self.status(tid)
        self.assertEqual(status["self_open_count"], 1)
        self.assertEqual(status["open_count"], 1)
        self.assertEqual(status["opens"][0]["open_kind"], "human")

    def test_old_proxy_opens_untouched(self):
        tid = self.new_tracker(age_seconds=3600)["tracking_id"]
        self.open_pixel(tid, ip="66.249.84.1", ua=GMAIL_PROXY_UA)
        old = (app.utcnow() - timedelta(minutes=10)).isoformat()
        with closing(app.connect()) as conn:
            conn.execute("UPDATE opens SET opened_at = ? WHERE tracking_id = ?", (old, tid))
            conn.commit()
        self.client.post("/api/self-view", json={"tracking_ids": [tid]}, headers=AUTH)
        self.assertEqual(self.status(tid)["open_count"], 1)

    def test_requires_auth_and_list(self):
        self.assertEqual(self.client.post("/api/self-view", json={"tracking_ids": []}).status_code, 401)
        res = self.client.post("/api/self-view", json={"tracking_ids": "abc"}, headers=AUTH)
        self.assertEqual(res.status_code, 400)


class TestViewBeacon(BackendTestCase):
    def setUp(self):
        super().setUp()
        self.aid = self.upload("doc.pdf", PDF_BYTES).get_json()["attachment_id"]

    def view_token(self, aid=None):
        page = self.client.get(f"/view/{aid or self.aid}").get_data(as_text=True)
        marker = 'data-beacon-token="'
        start = page.index(marker) + len(marker)
        return page[start:page.index('"', start)]

    def views(self):
        return self.client.get(f"/api/attachment-status/{self.aid}", headers=AUTH).get_json()

    def beacon(self, token, aid=None, ua=CHROME_UA, ip="203.0.113.99"):
        return self.client.post(
            f"/view/{aid or self.aid}/seen", data={"token": token},
            headers={"User-Agent": ua}, environ_overrides={"REMOTE_ADDR": ip},
        )

    def test_page_load_alone_is_not_a_view(self):
        self.client.get(f"/view/{self.aid}", headers={"User-Agent": CHROME_UA})
        self.assertEqual(self.views()["view_count"], 0)

    def test_beacon_records_view(self):
        res = self.beacon(self.view_token())
        self.assertEqual(res.status_code, 204)
        self.assertEqual(self.views()["view_count"], 1)

    def test_beacon_rejects_bad_tokens(self):
        other = self.upload("other.pdf", PDF_BYTES).get_json()["attachment_id"]
        for token, aid in (("garbage", None), (self.view_token(other), None), ("", None)):
            with self.subTest(token=token[:12]):
                self.assertEqual(self.beacon(token, aid).status_code, 400)
        self.assertEqual(self.views()["view_count"], 0)

    def test_beacon_token_expires(self):
        token = self.view_token()
        original = app.VIEW_BEACON_MAX_AGE
        app.VIEW_BEACON_MAX_AGE = -1
        try:
            self.assertEqual(self.beacon(token).status_code, 400)
        finally:
            app.VIEW_BEACON_MAX_AGE = original

    def test_scanner_beacon_not_counted(self):
        self.beacon(self.view_token(), ua="Mozilla/5.0 (compatible; Mimecast)")
        status = self.views()
        self.assertEqual(status["view_count"], 0)
        self.assertEqual(status["automated_view_count"], 1)

    def test_sender_view_is_self(self):
        # Uploads come from the test client's default 127.0.0.1
        self.beacon(self.view_token(), ip="127.0.0.1")
        self.assertEqual(self.views()["self_view_count"], 1)


class TestMigrations(BackendTestCase):
    def test_upgrade_from_unversioned_database(self):
        """A database created before migrations existed: no user_version, no open_kind, auto-ignored IPs."""
        legacy = self.tmpdir / "legacy.db"
        with closing(sqlite3.connect(legacy)) as conn:
            conn.executescript("""
                CREATE TABLE emails (id TEXT PRIMARY KEY, label TEXT, created_at TEXT, sender_ip TEXT);
                CREATE TABLE opens (id INTEGER PRIMARY KEY AUTOINCREMENT, tracking_id TEXT, opened_at TEXT,
                                    ip TEXT, user_agent TEXT, is_self INTEGER DEFAULT 0);
                CREATE TABLE ignored_ips (ip TEXT PRIMARY KEY, label TEXT, created_at TEXT);
                INSERT INTO emails VALUES ('e1', 'Old', '2026-01-01T00:00:00', '198.51.100.7');
                INSERT INTO opens (tracking_id, opened_at, ip, user_agent) VALUES
                    ('e1', '2026-01-01T00:00:01', '17.1.2.3', 'Mozilla/5.0'),
                    ('e1', '2026-01-01T02:00:00', '203.0.113.9', 'Thunderbird/115');
                INSERT INTO ignored_ips VALUES ('198.51.100.7', 'Sender IP (Auto)', '2026-01-01T00:00:00');
                INSERT INTO ignored_ips VALUES ('192.0.2.1', 'Home', '2026-01-01T00:00:00');
            """)

        app.app.config["DB_PATH"] = legacy
        app.init_db()
        app.init_db()  # idempotent

        with closing(app.connect()) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(app.MIGRATIONS))
            kinds = [r[0] for r in conn.execute("SELECT open_kind FROM opens ORDER BY id")]
            self.assertEqual(kinds, ["apple_mpp", "human"])
            ips = [r[0] for r in conn.execute("SELECT ip FROM opens ORDER BY id")]
            self.assertEqual(ips, ["17.1.2.0", "203.0.113.0"])
            ignored = [r[0] for r in conn.execute("SELECT ip FROM ignored_ips")]
            self.assertEqual(ignored, ["192.0.2.1"])
            # Tables added in later versions exist too
            conn.execute("SELECT * FROM attachment_views").fetchall()


class TestDisclosure(BackendTestCase):
    def test_default_disclosure_tells_recipients_about_tracking(self):
        data = self.new_tracker()
        self.assertIn("notified when this email is opened", data["disclosure_text"])
        self.assertIn(data["disclosure_text"], data["disclosure_html"])
        self.assertIn(data["disclosure_html"], data["combined_snippet"])
        # More than an emoji: at least a few real words
        self.assertGreaterEqual(len(data["disclosure_text"].split()), 5)


class TestIpTruncation(BackendTestCase):
    def test_truncate_ip(self):
        self.assertEqual(app.truncate_ip("203.0.113.77"), "203.0.113.0")
        self.assertEqual(app.truncate_ip("2001:db8:1234:5678::1"), "2001:db8:1234::")
        self.assertIsNone(app.truncate_ip("localhost"))
        self.assertIsNone(app.truncate_ip(None))

    def test_open_and_view_ips_stored_truncated(self):
        tid = self.new_tracker()["tracking_id"]
        self.open_pixel(tid, ip="203.0.113.77")
        self.open_pixel(tid, ip="2001:db8:1234:5678::1")
        self.assertEqual({o["ip"] for o in self.status(tid)["opens"]}, {"203.0.113.0", "2001:db8:1234::"})

        aid = self.upload("doc.pdf", PDF_BYTES).get_json()["attachment_id"]
        page = self.client.get(f"/view/{aid}").get_data(as_text=True)
        token = page.split('data-beacon-token="')[1].split('"')[0]
        self.client.post(f"/view/{aid}/seen", data={"token": token},
                         headers={"User-Agent": CHROME_UA}, environ_overrides={"REMOTE_ADDR": "198.51.100.99"})
        views = self.client.get(f"/api/attachment-status/{aid}", headers=AUTH).get_json()["views"]
        self.assertEqual(views[0]["ip"], "198.51.100.0")

    def test_self_detection_uses_full_ip_before_truncating(self):
        tid = self.new_tracker()["tracking_id"]
        self.client.post("/api/ignored-ips", json={"ip": "203.0.113.77"}, headers=AUTH)
        self.open_pixel(tid, ip="203.0.113.77")   # me
        self.open_pixel(tid, ip="203.0.113.78")   # a neighbour in the same /24
        status = self.status(tid)
        self.assertEqual(status["self_open_count"], 1)
        self.assertEqual(status["open_count"], 1)


class TestRetention(BackendTestCase):
    def age(self, table, row_id, days, column="created_at"):
        ts = (app.utcnow() - timedelta(days=days)).isoformat()
        with closing(app.connect()) as conn:
            conn.execute(f"UPDATE {table} SET {column} = ? WHERE id = ?", (ts, row_id))
            conn.commit()

    def exists(self, table, row_id):
        with closing(app.connect()) as conn:
            return conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,)).fetchone() is not None

    def test_purges_expired_emails_attachments_and_files(self):
        old_aid = self.upload("old.pdf", PDF_BYTES).get_json()["attachment_id"]
        old = self.new_tracker("old", attachment_ids=[old_aid])["tracking_id"]
        self.open_pixel(old)
        recent = self.new_tracker("recent")["tracking_id"]
        stray_aid = self.upload("stray.pdf", PDF_BYTES).get_json()["attachment_id"]

        self.age("emails", old, app.RETENTION_DAYS + 1)
        self.age("attachments", old_aid, app.RETENTION_DAYS + 1)
        self.age("attachments", stray_aid, app.RETENTION_DAYS + 1)
        app.purge_expired()

        self.assertFalse(self.exists("emails", old))
        self.assertFalse(self.exists("attachments", old_aid))
        self.assertFalse(self.exists("attachments", stray_aid))
        self.assertEqual(self.status(old)["all_opens"], [])
        self.assertEqual(list(self.upload_dir.iterdir()), [])
        self.assertTrue(self.exists("emails", recent))

    def test_retention_zero_keeps_everything(self):
        tid = self.new_tracker()["tracking_id"]
        self.age("emails", tid, 10_000)
        original = app.RETENTION_DAYS
        app.RETENTION_DAYS = 0
        try:
            app.purge_expired()
        finally:
            app.RETENTION_DAYS = original
        self.assertTrue(self.exists("emails", tid))

    def test_sender_ip_truncated_after_ttl(self):
        res = self.client.post("/new", json={"label": "x"}, headers=AUTH, environ_overrides={"REMOTE_ADDR": "198.51.100.7"})
        fresh = res.get_json()["tracking_id"]
        res = self.client.post("/new", json={"label": "y"}, headers=AUTH, environ_overrides={"REMOTE_ADDR": "198.51.100.8"})
        stale = res.get_json()["tracking_id"]
        self.backdate(stale, app.SENDER_IP_TTL.total_seconds() + 60)
        app.purge_expired()
        with closing(app.connect()) as conn:
            ips = dict(conn.execute("SELECT id, sender_ip FROM emails").fetchall())
        self.assertEqual(ips[fresh], "198.51.100.7")  # still needed for self-open detection
        self.assertEqual(ips[stale], "198.51.100.0")

    def test_purge_runs_on_requests_at_most_hourly(self):
        tid = self.new_tracker()["tracking_id"]
        self.age("emails", tid, app.RETENTION_DAYS + 1)
        app._last_purge = app.utcnow()  # purged moments ago: nothing happens yet
        self.client.get("/login")
        self.assertTrue(self.exists("emails", tid))
        app._last_purge = app.utcnow() - app.PURGE_INTERVAL
        self.client.get("/login")
        self.assertFalse(self.exists("emails", tid))

    def test_clear_all_removes_files(self):
        aid = self.upload("doc.pdf", PDF_BYTES).get_json()["attachment_id"]
        self.new_tracker("x", attachment_ids=[aid])
        self.upload("stray.pdf", PDF_BYTES)
        self.assertEqual(self.client.post("/api/clear-all", headers=AUTH).status_code, 200)
        self.assertEqual(list(self.upload_dir.iterdir()), [])
        self.assertFalse(self.exists("attachments", aid))


class TestDatabaseReliability(BackendTestCase):
    def test_wal_busy_timeout_and_indexes(self):
        with closing(app.connect()) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], app.SQLITE_BUSY_TIMEOUT_MS)
            indexes = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        for name in ("idx_opens_tracking", "idx_views_attachment", "idx_attachments_email", "idx_emails_created"):
            self.assertIn(name, indexes)

    def test_request_connection_closed_after_request(self):
        opened = []
        original = app.connect

        def tracking_connect():
            conn = original()
            opened.append(conn)
            return conn

        app.connect = tracking_connect
        try:
            self.client.get("/api/my-ip", headers=AUTH)
        finally:
            app.connect = original
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):  # closed
            opened[0].execute("SELECT 1")

    def test_uncommitted_work_rolled_back_when_request_ends(self):
        with app.app.test_request_context():
            app.get_db().execute("INSERT INTO emails (id, label, created_at) VALUES ('ghost', 'x', '2026-01-01')")
            # the request ends (e.g. the view raised) before commit
        with closing(app.connect()) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM emails WHERE id = 'ghost'").fetchone())


class TestDashboardPerformance(BackendTestCase):
    def make_emails(self, n, with_attachment=True):
        ids = []
        for i in range(n):
            aids = [self.upload(f"d{i}.pdf", PDF_BYTES).get_json()["attachment_id"]] if with_attachment else []
            tid = self.new_tracker(f"Subject {i} -> r{i}@example.com", attachment_ids=aids)["tracking_id"]
            self.open_pixel(tid)
            ids.append(tid)
        return ids

    def count_dashboard_queries(self):
        statements = []
        original = app.connect

        def tracing_connect():
            conn = original()
            conn.set_trace_callback(statements.append)
            return conn

        app.connect = tracing_connect
        try:
            self.assertEqual(self.client.get("/dashboard").status_code, 200)
        finally:
            app.connect = original
        return len([s for s in statements if s.lstrip().upper().startswith("SELECT")])

    def test_query_count_does_not_grow_with_emails(self):
        self.login()
        self.make_emails(2)
        few = self.count_dashboard_queries()
        self.make_emails(8)
        many = self.count_dashboard_queries()
        self.assertEqual(few, many)

    def test_pagination(self):
        self.login()
        app_page_size = app.DASHBOARD_PAGE_SIZE
        app.DASHBOARD_PAGE_SIZE = 3
        try:
            self.make_emails(7, with_attachment=False)
            page1 = self.client.get("/dashboard").get_data(as_text=True)
            page3 = self.client.get("/dashboard?page=3").get_data(as_text=True)
            beyond = self.client.get("/dashboard?page=99").get_data(as_text=True)
        finally:
            app.DASHBOARD_PAGE_SIZE = app_page_size
        self.assertEqual(page1.count('class="email-card"'), 3)
        self.assertIn("Page 1 of 3", page1)
        self.assertIn("Subject 6", page1)  # newest first
        self.assertEqual(page3.count('class="email-card"'), 1)
        self.assertIn("Subject 0", page3)
        self.assertIn("Page 3 of 3", beyond)  # clamped
        # Stats are global, not per page
        self.assertIn('data-tab="all" data-count="7"', page3)

    def test_status_filter_and_search(self):
        self.login()
        opened = self.new_tracker("Budget 100% final -> a@example.com")["tracking_id"]
        self.open_pixel(opened)
        self.new_tracker("Budget draft -> b@example.com")
        self.new_tracker("Lunch -> c@example.com")

        def subjects(url):
            html = self.client.get(url).get_data(as_text=True)
            return {s for s in ("100% final", "Budget draft", "Lunch") if s in html}

        self.assertEqual(subjects("/dashboard?status=opened"), {"100% final"})
        self.assertEqual(subjects("/dashboard?status=waiting"), {"Budget draft", "Lunch"})
        self.assertEqual(subjects("/dashboard?q=budget"), {"100% final", "Budget draft"})
        self.assertEqual(subjects("/dashboard?q=100%25"), {"100% final"})  # % is literal, not a wildcard
        self.assertEqual(subjects("/dashboard?q=budget&status=waiting"), {"Budget draft"})
        self.assertIn("No emails match", self.client.get("/dashboard?q=zzz").get_data(as_text=True))
        self.assertEqual(self.client.get("/dashboard?status=bogus").status_code, 200)


class TestSubjectRecipients(BackendTestCase):
    def test_split_label(self):
        self.assertEqual(app.split_label("Plan -> bob@example.com"), ("Plan", "bob@example.com"))
        self.assertEqual(app.split_label("A->B migration -> a@x.com, b@y.com"), ("A->B migration", "a@x.com, b@y.com"))
        self.assertEqual(app.split_label("No recipients"), ("No recipients", ""))

    def test_explicit_fields_win_over_label(self):
        data = self.new_tracker("ignored -> ignored", subject="Q3 -> Q4 plan", recipients="bob@example.com")
        self.assertEqual((data["subject"], data["recipients"]), ("Q3 -> Q4 plan", "bob@example.com"))

    def test_old_clients_label_is_parsed(self):
        data = self.new_tracker("A->B migration -> carol@example.com")
        self.assertEqual((data["subject"], data["recipients"]), ("A->B migration", "carol@example.com"))

    def test_dashboard_renders_subject_and_recipients(self):
        self.new_tracker("x", subject="Q3 -> Q4 plan", recipients="bob@example.com")
        self.login()
        html = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn('<span class="email-subject">Q3 -&gt; Q4 plan</span>', html)
        self.assertIn('<span class="email-recipient-tag">bob@example.com</span>', html)

    def test_migration_backfills_existing_rows(self):
        legacy = self.tmpdir / "legacy.db"
        with closing(sqlite3.connect(legacy)) as conn:
            conn.executescript("""
                CREATE TABLE emails (id TEXT PRIMARY KEY, label TEXT, created_at TEXT, sender_ip TEXT);
                INSERT INTO emails VALUES ('e1', 'A->B migration -> dan@example.com', '2026-01-01T00:00:00', NULL);
            """)
        app.app.config["DB_PATH"] = legacy
        app.init_db()
        with closing(app.connect()) as conn:
            row = conn.execute("SELECT subject, recipients FROM emails WHERE id = 'e1'").fetchone()
        self.assertEqual(tuple(row), ("A->B migration", "dan@example.com"))


class TestDashboardContent(BackendTestCase):
    def test_activity_separates_recipient_own_and_automated_opens(self):
        self.client.post("/api/heartbeat", headers=AUTH, environ_overrides={"REMOTE_ADDR": "198.51.100.7"})
        tid = self.new_tracker("x", subject="Proposal", recipients="bob@example.com")["tracking_id"]
        self.open_pixel(tid, ip="203.0.113.60")
        self.open_pixel(tid, ip="198.51.100.7")
        self.open_pixel(tid, ua=APPLE_MPP_UA)
        self.login()
        html = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn('data-open-count="1"', html)
        self.assertIn("This was me", html)
        self.assertIn("1 open by you, hidden", html)
        self.assertIn("1 automated fetch, not counted", html)
        self.assertIn("203.0.113.x", html)  # truncated network, shown readably

    def test_settings_lists_recognised_networks_and_ignored_ips(self):
        self.client.post("/api/ignored-ips", json={"ip": "192.0.2.5"}, headers=AUTH)
        self.login()
        html = self.client.get("/dashboard", environ_overrides={"REMOTE_ADDR": "198.51.100.9"}).get_data(as_text=True)
        self.assertIn('data-network="198.51.100.9"', html)
        self.assertIn("This device", html)
        self.assertIn('data-ip="192.0.2.5"', html)

    def test_standalone_document_views(self):
        aid = self.upload("brief.pdf", PDF_BYTES).get_json()["attachment_id"]
        page = self.client.get(f"/view/{aid}").get_data(as_text=True)
        marker = 'data-beacon-token="'
        token = page[page.index(marker) + len(marker):].split('"', 1)[0]
        self.client.post(f"/view/{aid}/seen", data={"token": token},
                         environ_overrides={"REMOTE_ADDR": "203.0.113.60"}, headers={"User-Agent": CHROME_UA})
        self.login()
        html = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn(f'id="att-card-{aid}"', html)
        self.assertIn("Viewed 1×", html)
        self.assertIn("Viewed brief.pdf", html)

    def test_viewer_page_is_unbranded(self):
        aid = self.upload("brief.pdf", PDF_BYTES).get_json()["attachment_id"]
        html = self.client.get(f"/view/{aid}").get_data(as_text=True)
        self.assertNotIn("MailSeen", html)
        self.assertNotIn("Shared securely", html)


class TestSecurityHeaders(BackendTestCase):
    def test_html_pages_cannot_be_framed(self):
        self.login()
        for path in ("/login", "/dashboard"):
            with self.subTest(path=path):
                res = self.client.get(path)
                self.assertEqual(res.headers["X-Frame-Options"], "DENY")
                self.assertIn("frame-ancestors 'none'", res.headers["Content-Security-Policy"])
                self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(res.headers["Referrer-Policy"], "same-origin")

    def test_file_keeps_its_own_policy_so_the_viewer_can_frame_it(self):
        aid = self.upload("doc.pdf", PDF_BYTES).get_json()["attachment_id"]
        res = self.get_file(f"/file/{aid}")
        self.assertEqual(res.headers["Content-Security-Policy"], app.FILE_CSP)
        self.assertNotIn("X-Frame-Options", res.headers)

    def test_json_responses_get_nosniff(self):
        res = self.client.get("/api/my-ip", headers=AUTH)
        self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")


if __name__ == "__main__":
    unittest.main()
