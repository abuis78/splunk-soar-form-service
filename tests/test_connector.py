"""Connector tests: real handlers with a fake `phantom` package against the fake form service."""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests import fake_phantom

fake_phantom.install()

from soar_form_service_connector import FormServiceConnector  # noqa: E402
from tests.test_utils import KEY, LINK_ID, FakeService  # noqa: E402

APP = json.loads((Path(__file__).resolve().parent.parent / "soar_form_service.json").read_text(encoding="utf-8"))


class ConnectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeService)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        FakeService.calls, FakeService.override = [], None

    def run_action(self, action_id, param=None, key=KEY, base_url=None, container_id=7):
        conn = FormServiceConnector()
        conn.config = {"base_url": base_url or self.url, "api_key": key, "verify_server_cert": True, "timeout": 5}
        conn.action_id, conn.container_id = action_id, container_id
        if not conn.initialize():
            return conn, None
        status = conn.handle_action(param or {})
        result = conn.action_results[0]
        self.assertEqual(status, result.status)
        self.assertNotIn(key, result.message + json.dumps(result.data) + " ".join(conn.progress))
        return conn, result

    def test_every_manifest_action_has_a_handler(self):
        for action in APP["actions"]:
            _, result = self.run_action(action["identifier"], {"form": "evidence_upload", "link_id": LINK_ID})
            self.assertIsNotNone(result, action["identifier"])

    def test_connectivity_passes(self):
        conn, result = self.run_action("test_connectivity")
        self.assertTrue(result.status)
        self.assertIn("Test Connectivity Passed", conn.progress)

    def test_connectivity_fails_with_wrong_key(self):
        conn, result = self.run_action("test_connectivity", key="not-the-key")
        self.assertFalse(result.status)
        self.assertIn("401", result.message)
        self.assertIn("Test Connectivity Failed", conn.progress)

    def test_create_link_defaults_to_current_container(self):
        _, result = self.run_action("create_form_link", {"form": "evidence_upload"}, container_id=42)
        self.assertTrue(result.status, result.message)
        self.assertEqual(FakeService.calls[0][3]["container_id"], 42)
        self.assertEqual(result.data[0]["link_id"], LINK_ID)
        self.assertEqual(result.summary, {"link_id": LINK_ID})

    def test_create_link_explicit_container_and_ttl(self):
        _, result = self.run_action(
            "create_form_link", {"form": "evidence_upload", "container_id": 9, "ttl_minutes": 30}
        )
        self.assertTrue(result.status, result.message)
        self.assertEqual(FakeService.calls[0][3], {"form": "evidence_upload", "container_id": 9, "ttl_minutes": 30})

    def test_create_link_invalid_form(self):
        _, result = self.run_action("create_form_link", {"form": "../x"})
        self.assertFalse(result.status)
        self.assertEqual(FakeService.calls, [])

    def test_status_and_revoke(self):
        _, result = self.run_action("get_link_status", {"link_id": LINK_ID})
        self.assertEqual((result.status, result.summary), (True, {"status": "open"}))
        _, result = self.run_action("revoke_link", {"link_id": LINK_ID})
        self.assertEqual((result.status, result.summary), (True, {"status": "revoked"}))

    def test_status_unknown_link(self):
        _, result = self.run_action("get_link_status", {"link_id": "fedcba9876543210"})
        self.assertFalse(result.status)
        self.assertIn("404", result.message)

    def test_list_forms(self):
        _, result = self.run_action("list_forms")
        self.assertEqual(result.summary, {"total_forms": 1})
        self.assertEqual(result.data[0]["name"], "evidence_upload")

    def test_invalid_asset_config_fails_initialize(self):
        conn, result = self.run_action("list_forms", base_url="ftp://nope")
        self.assertIsNone(result)
        self.assertFalse(conn.status[0])
        self.assertIn("Asset configuration invalid", conn.status[1])

    def test_unknown_action(self):
        conn = FormServiceConnector()
        conn.config, conn.action_id = {"base_url": self.url, "api_key": KEY}, "nope"
        conn.initialize()
        self.assertFalse(conn.handle_action({}))


if __name__ == "__main__":
    unittest.main()
