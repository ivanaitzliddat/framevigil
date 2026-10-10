import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from server import COOLDOWN_SECONDS, MonitorServer, MonitorState


class FakeOCR:
    available = True

    def extract(self, image):
        return "A connection\n LOST warning appeared"


class MonitorStateTests(unittest.TestCase):
    def test_same_match_repeats_until_acknowledged_then_rearms_after_duration(self):
        class ChangingOCR(FakeOCR):
            text = "A connection\n LOST warning appeared"

            def extract(self, image):
                return self.text

        ocr = ChangingOCR()
        state = MonitorState(ocr)
        state.set_rules(["connection lost", "CONNECTION LOST"])
        first = state.scan(b"image", now=100)
        second = state.scan(b"image", now=100 + COOLDOWN_SECONDS - 1)
        third = state.scan(b"image", now=100 + COOLDOWN_SECONDS)
        state.set_ack_duration(60)
        state.acknowledge(third["events"][0]["id"], now=116)
        quiet = state.scan(b"image", now=160)
        after_expiry = state.scan(b"image", now=176)
        ocr.text = "All clear"
        state.scan(b"image", now=177)
        ocr.text = "A connection\n LOST warning appeared"
        fourth = state.scan(b"image", now=192)
        self.assertEqual(len(first["events"]), 1)
        self.assertEqual(second["events"], [])
        self.assertEqual(len(third["events"]), 1)
        self.assertEqual(quiet["events"], [])
        self.assertEqual(len(after_expiry["events"]), 1)
        self.assertEqual(len(fourth["events"]), 1)
        self.assertEqual(len(state.recent_events()), 4)

    def test_acknowledgement_scope_uses_device_trigger_and_sentence(self):
        class LineOCR:
            available = True
            text = "SYSTEM ERROR"
            def extract_lines(self, image):
                return [{"text": self.text, "box": [10, 10, 200, 40]}]

        ocr = LineOCR()
        state = MonitorState(ocr)
        kwargs = {"rules": ["ERROR"], "source_id": "camera-one"}
        first = state.scan(b"image", now=100, **kwargs)["events"][0]
        state.acknowledge(first["id"], now=101)
        self.assertEqual(state.scan(b"another frame", now=200, **kwargs)["events"], [])
        self.assertEqual(len(state.scan(b"image", now=200, rules=["ERROR"], source_id="camera-two")["events"]), 1)
        ocr.text = "SECOND ERROR"
        self.assertEqual(len(state.scan(b"image", now=200, **kwargs)["events"]), 1)

    def test_rejects_invalid_rule_and_does_not_store_ocr_text(self):
        state = MonitorState(FakeOCR())
        with self.assertRaises(ValueError):
            state.set_rules(["x"])
        state.set_rules(["warning"])
        state.scan(b"image", now=100)
        self.assertNotIn("connection", json.dumps(state.recent_events()))


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.server = MonitorServer(0, FakeOCR(), source_config_path=Path(cls.directory.name) / "sources.json")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.directory.cleanup()

    def request(self, method, path, body=None, origin=None, content_type=None, extra_headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {}
        if origin:
            headers["Origin"] = origin
        if content_type:
            headers["Content-Type"] = content_type
        headers.update(extra_headers or {})
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def test_local_scan_and_foreign_origin_rejection(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        status, _ = self.request("POST", "/api/scan", b"\xff\xd8image", "https://another.example", "image/jpeg")
        self.assertEqual(status, 403)
        status, body = self.request("POST", "/api/rules", b'{"rules":["warning"]}', base, "application/json")
        self.assertEqual(status, 200)
        status, body = self.request("POST", "/api/scan", b"\xff\xd8image", base, "image/jpeg")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["events"][0]["term"], "warning")

    def test_browser_camera_has_a_distinct_source_identity(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        self.request("POST", "/api/rules", b'{"rules":["warning"]}', base, "application/json")
        identifier = "browser:" + "a" * 32 + ":" + "b" * 16
        status, body = self.request("POST", "/api/scan", b"\xff\xd8image", base, "image/jpeg", {"X-FrameVigil-Source": identifier, "X-FrameVigil-Name": "Browser camera"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["events"][0]["source_id"], identifier)
        self.assertEqual(self.request("POST", "/api/scan", b"\xff\xd8image", base, "image/jpeg", {"X-FrameVigil-Source": "invalid"})[0], 400)

    def test_alert_image_endpoint_requires_known_event(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        self.request("POST", "/api/rules", b'{"rules":["warning"]}', base, "application/json")
        status, body = self.request("POST", "/api/scan", b"\xff\xd8image", base, "image/jpeg")
        self.assertEqual(status, 200)
        identifier = json.loads(body)["events"][0]["id"]
        status, image = self.request("GET", f"/api/evidence/{identifier}")
        self.assertEqual((status, image), (200, b"\xff\xd8image"))
        self.assertEqual(self.request("GET", "/api/evidence/000000000000000000000000")[0], 404)

    def test_acknowledgement_api_and_saved_duration(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        status, body = self.request("POST", "/api/settings", b'{"ack_duration_seconds":300}', base, "application/json")
        self.assertEqual((status, json.loads(body)["ack_duration_seconds"]), (200, 300))
        self.assertEqual(json.loads(self.server.settings_path.read_text())["ack_duration_seconds"], 300)
        self.request("POST", "/api/rules", b'{"rules":["warning"]}', base, "application/json")
        self.server.state.last_fired.clear()
        status, body = self.request("POST", "/api/scan", b"\xff\xd8image", base, "image/jpeg")
        identifier = json.loads(body)["events"][0]["id"]
        status, body = self.request("POST", f"/api/events/{identifier}/ack", b"", base)
        self.assertEqual(status, 200)
        self.assertIsNotNone(json.loads(body)["event"]["acknowledged_until"])
        self.assertTrue(json.loads(self.server.settings_path.read_text())["acknowledged"])
        self.assertEqual(json.loads(self.request("GET", "/api/events")[1])["events"][0]["acknowledged_until"], json.loads(body)["event"]["acknowledged_until"])
        self.assertEqual(self.request("POST", f"/api/events/{identifier}/unack", b"", base)[0], 200)
        self.assertFalse(json.loads(self.server.settings_path.read_text())["acknowledged"])

    def test_saved_acknowledgements_load_after_server_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            first = MonitorServer(0, FakeOCR(), source_config_path=path / "sources.json")
            first.state.set_rules(["warning"])
            event = first.state.scan(b"image")["events"][0]
            first.state.acknowledge(event["id"])
            first.state.set_ack_duration(300)
            first.save_settings()
            first.server_close()
            second = MonitorServer(0, FakeOCR(), source_config_path=path / "sources.json")
            self.assertEqual(second.state.status()["ack_duration_seconds"], 300)
            self.assertEqual(second.state.scan(b"image", rules=["warning"])["events"], [])
            second.server_close()


class ShutdownTests(unittest.TestCase):
    def test_shutdown_requires_origin_and_access_code_then_stops_server(self):
        with tempfile.TemporaryDirectory() as directory:
            with MonitorServer(0, FakeOCR(), access_token="a" * 32, source_config_path=Path(directory) / "sources.json") as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = f"http://127.0.0.1:{server.server_port}"

                def request(origin, token):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    connection.request("POST", "/api/shutdown", body=b"", headers={"Origin": origin, "Authorization": f"Bearer {token}"})
                    response = connection.getresponse()
                    result = response.status, json.loads(response.read())
                    connection.close()
                    return result

                self.assertEqual(request(base, "wrong")[0], 401)
                self.assertEqual(request("https://foreign.example", "a" * 32)[0], 403)
                self.assertTrue(thread.is_alive())
                status, body = request(base, "a" * 32)
                self.assertEqual(status, 200)
                self.assertIn("shutting down", body["message"])
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
