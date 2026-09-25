import unittest
import app

class TestEmailTrackerBackend(unittest.TestCase):
    def setUp(self):
        app.init_db()
        self.client = app.app.test_client()

    def test_cors_headers(self):
        # Test OPTIONS preflight from mail.google.com
        res = self.client.options(
            "/new",
            headers={"Origin": "https://mail.google.com", "Access-Control-Request-Method": "POST"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(res.headers.get("Access-Control-Allow-Origin"), ("*", "https://mail.google.com"))

    def test_new_endpoint_get_and_post(self):
        # GET
        res = self.client.get("/new?label=Unit+Test+Subject")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("tracking_id", data)
        self.assertIn("pixel_url", data)
        self.assertIn("disclosure_html", data)
        self.assertIn("", data["disclosure_html"])
        self.assertEqual(data["label"], "Unit Test Subject")

        # POST with JSON
        res2 = self.client.post("/new", json={"label": "Meeting Notes -> alice@example.com"})
        self.assertEqual(res2.status_code, 200)
        data2 = res2.get_json()
        self.assertEqual(data2["label"], "Meeting Notes -> alice@example.com")
        self.assertIn("tracking_id", data2)

    def test_self_open_filtering(self):
        # Create tracker
        res = self.client.post("/new", json={"label": "Self Open Test"})
        tid = res.get_json()["tracking_id"]

        # Add 198.51.100.42 as ignored IP
        self.client.post("/api/ignored-ips", json={"ip": "198.51.100.42", "action": "add", "label": "Home"})

        # Simulate self open from 198.51.100.42
        res_pixel = self.client.get(
            f"/track/{tid}.png",
            environ_overrides={"REMOTE_ADDR": "198.51.100.42"}
        )
        self.assertEqual(res_pixel.status_code, 200)
        self.assertEqual(res_pixel.mimetype, "image/png")

        # Status check - open_count should be 0 because it was a self open, but self_open_count should be 1
        res_status = self.client.get(f"/api/status/{tid}")
        status_data = res_status.get_json()
        self.assertFalse(status_data["opened"])
        self.assertEqual(status_data["open_count"], 0)
        self.assertEqual(status_data["self_open_count"], 1)

        # Simulate genuine open from recipient at 203.0.113.99
        res_pixel2 = self.client.get(
            f"/track/{tid}.png",
            environ_overrides={"REMOTE_ADDR": "203.0.113.99"}
        )
        self.assertEqual(res_pixel2.status_code, 200)

        # Status check - genuine open count should now be 1
        res_status2 = self.client.get(f"/api/status/{tid}")
        status_data2 = res_status2.get_json()
        self.assertTrue(status_data2["opened"])
        self.assertEqual(status_data2["open_count"], 1)
        self.assertEqual(status_data2["self_open_count"], 1)

    def test_dashboard_and_ip_api(self):
        res_my_ip = self.client.get("/api/my-ip")
        self.assertEqual(res_my_ip.status_code, 200)
        self.assertIn("ip", res_my_ip.get_json())

        res_dash = self.client.get("/dashboard")
        self.assertEqual(res_dash.status_code, 200)
        self.assertIn(b"Email Tracker", res_dash.data)

    def test_delete_email_and_clear_all(self):
        # Create an email
        res = self.client.post("/new", json={"label": "Delete Test"})
        tid = res.get_json()["tracking_id"]

        # Delete it
        del_res = self.client.post("/api/delete-email", json={"tracking_id": tid})
        self.assertEqual(del_res.status_code, 200)
        self.assertTrue(del_res.get_json()["success"])

        # Check status shows 0 opens and email no longer exists
        stat = self.client.get(f"/api/status/{tid}")
        self.assertEqual(stat.get_json()["open_count"], 0)

    def test_client_device_parsing(self):
        gmail_ua = "Mozilla/5.0 (Windows NT 5.1; rv:11.0) Gecko Firefox/11.0 (via ggpht.com GoogleImageProxy)"
        self.assertEqual(app.parse_client_device(gmail_ua), "Gmail (Google Image Proxy)")

        ios_ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"
        self.assertEqual(app.parse_client_device(ios_ua), "Apple Mail (iOS)")


if __name__ == "__main__":
    unittest.main()

