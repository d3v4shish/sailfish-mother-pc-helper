import json
import logging
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sailfish_mother_pc_helper as helper  # noqa: E402


def payload():
    return helper.sample_payload()


class DiscoveryNormalizationTests(unittest.TestCase):
    def test_usable_payload_updates_current_and_latest_usable(self):
        config = helper.default_config()
        config["stale_after_seconds"] = 3600

        state = helper.normalize_payload(payload(), config, {}, "unit-test")

        self.assertEqual(state["last_ingest"]["status"], "usable")
        self.assertEqual(state["current"]["ssh"]["host"], "192.0.2.20")
        self.assertEqual(state["latest_usable"]["webcam"]["mjpeg_url"], "http://192.0.2.20:8090/stream.mjpeg")

    def test_partial_payload_keeps_previous_latest_usable(self):
        config = helper.default_config()
        config["stale_after_seconds"] = 3600
        first = helper.normalize_payload(payload(), config, {}, "first")

        partial = payload()
        partial["ssh"].pop("host")
        state = helper.normalize_payload(partial, config, first, "partial")

        self.assertEqual(state["last_ingest"]["status"], "partial")
        self.assertIsNone(state["current"]["ssh"]["host"])
        self.assertEqual(state["latest_usable"]["ssh"]["host"], "192.0.2.20")

    def test_trust_mismatch_does_not_replace_current_state(self):
        config = helper.default_config()
        config["trusted_device_id"] = "trusted-phone"
        first = helper.normalize_payload(payload(), helper.default_config(), {}, "first")

        untrusted = payload()
        untrusted["device"]["id"] = "other-phone"
        state = helper.normalize_payload(untrusted, config, first, "untrusted")

        self.assertEqual(state["last_ingest"]["status"], "untrusted")
        self.assertFalse(state["last_ingest"]["accepted"])
        self.assertEqual(state["current"]["device"]["id"], "sample-phone")

    def test_unsupported_version_is_explicit(self):
        config = helper.default_config()
        wrong = payload()
        wrong["version"] = 2

        state = helper.normalize_payload(wrong, config, {}, "bad-version")

        self.assertEqual(state["last_ingest"]["status"], "unsupported_version")
        self.assertIsNone(state["current"])

    def test_invalid_persisted_config_falls_back_to_safe_defaults(self):
        config = helper.normalize_config(
            {
                "stale_after_seconds": "not-a-number",
                "listen_port": 0,
                "discovery_port": 70000,
                "discovery_multicast_group": "192.168.1.1",
                "trusted_device_id": "  phone-id  ",
            }
        )

        self.assertEqual(config["stale_after_seconds"], 300)
        self.assertEqual(config["listen_port"], 8765)
        self.assertEqual(config["discovery_port"], 45177)
        self.assertEqual(config["discovery_multicast_group"], "239.255.77.77")
        self.assertEqual(config["trusted_device_id"], "phone-id")

    def test_endpoint_handoff_requires_matching_pinned_device(self):
        config = helper.default_config()
        config["stale_after_seconds"] = 3600
        record = helper.normalize_payload(payload(), config, {}, "unit-test")["current"]

        self.assertFalse(helper.endpoint_is_trusted(record, config))
        config["trusted_device_id"] = "sample-phone"
        self.assertTrue(helper.endpoint_is_trusted(record, config))
        config["trusted_device_id"] = "different-phone"
        self.assertFalse(helper.endpoint_is_trusted(record, config))


class ListenerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sailfish-helper-test-")
        base = Path(self.tmp.name)
        self.paths = {
            "config_dir": base / "config",
            "state_dir": base / "state",
            "cache_dir": base / "cache",
            "log_dir": base / "logs",
            "config_file": base / "config" / "config.json",
            "secrets_file": base / "config" / "secrets.json",
            "state_file": base / "state" / "state.json",
            "log_file": base / "logs" / "helper.log",
        }
        helper.ensure_dirs(self.paths)
        self.config = helper.default_config()
        self.config["stale_after_seconds"] = 3600
        self.logger = logging.getLogger(f"helper-test-{id(self)}")
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())
        self.lock = threading.Lock()

    def tearDown(self):
        self.tmp.cleanup()

    def test_http_listener_ingests_discovery_and_serves_state(self):
        server = helper.make_http_server("127.0.0.1", 0, self.paths, self.config, self.logger, self.lock)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            request = Request(
                f"http://127.0.0.1:{port}/discovery",
                data=json.dumps(payload()).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=2) as response:
                body = json.load(response)
            self.assertEqual(body["status"], "usable")

            with urlopen(f"http://127.0.0.1:{port}/status", timeout=2) as response:
                state = json.load(response)
            self.assertEqual(state["current"]["device"]["id"], "sample-phone")
            self.assertTrue(state["last_ingest"]["source"].startswith("http:127.0.0.1"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_udp_listener_ingests_sailfish_link_style_datagram(self):
        listener = helper.UdpDiscoveryListener(0, None, self.paths, self.config, self.logger, self.lock)
        listener.start()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                sender.sendto(json.dumps(payload()).encode("utf-8"), ("127.0.0.1", listener.port))

            deadline = time.monotonic() + 2
            state = {}
            while time.monotonic() < deadline:
                state = helper.load_state(self.paths)
                if state.get("last_ingest", {}).get("status") == "usable":
                    break
                time.sleep(0.02)
            self.assertEqual(state["last_ingest"]["status"], "usable")
            self.assertTrue(state["last_ingest"]["source"].startswith("udp:127.0.0.1:"))
        finally:
            listener.close()


class DashboardIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sailfish-helper-dashboard-test-")
        base = Path(self.tmp.name)
        self.paths = {
            "config_dir": base / "config",
            "state_dir": base / "state",
            "cache_dir": base / "cache",
            "log_dir": base / "logs",
            "config_file": base / "config" / "config.json",
            "secrets_file": base / "config" / "secrets.json",
            "state_file": base / "state" / "state.json",
            "log_file": base / "logs" / "helper.log",
        }
        helper.ensure_dirs(self.paths)
        self.config = helper.default_config()
        self.config["stale_after_seconds"] = 3600
        self.secrets = helper.default_secrets()
        self.logger = logging.getLogger(f"helper-dashboard-test-{id(self)}")
        self.logger.handlers.clear()
        self.logger.addHandler(logging.NullHandler())
        self.lock = threading.Lock()
        helper.ingest_payload(payload(), "dashboard-test", self.paths, self.config, self.logger)
        self.server = helper.make_gui_http_server(
            "127.0.0.1", 0, self.paths, self.config, self.secrets, self.logger, self.lock
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def get_json(self, path):
        with urlopen(self.base_url + path, timeout=2) as response:
            return json.load(response)

    def post_json(self, path, body):
        request = Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return json.load(response)

    def test_dashboard_keeps_tokens_private_and_requires_trust(self):
        with urlopen(self.base_url + "/", timeout=2) as response:
            page = response.read().decode("utf-8")
        self.assertIn("Sailfish Phone Control", page)

        status = self.get_json("/api/status")
        self.assertFalse(status["trust"]["configured"])
        self.assertFalse(status["tokens"]["webcam_configured"])

        with self.assertRaises(HTTPError) as error:
            self.post_json("/api/action", {"action": "ssh-command"})
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

        trusted = self.post_json("/api/trust", {"device_id": "sample-phone"})
        self.assertEqual(trusted["trusted_device_id"], "sample-phone")
        self.post_json("/api/tokens", {"webcam_token": "private-webcam-token", "lls_token": "private-lls-token"})

        status = self.get_json("/api/status")
        self.assertTrue(status["tokens"]["webcam_configured"])
        self.assertTrue(status["tokens"]["lls_configured"])
        self.assertNotIn("private-webcam-token", json.dumps(status))
        self.assertEqual(oct(self.paths["secrets_file"].stat().st_mode & 0o777), "0o600")

        handoff = self.post_json("/api/action", {"action": "webcam-url"})
        self.assertIn("token=private-webcam-token", handoff["data"]["url"])
        self.assertIn("ffplay", handoff["data"]["ffplay_command"])


if __name__ == "__main__":
    unittest.main()
