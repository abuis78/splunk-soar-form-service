"""Unit tests for the form service client against a fake HTTP server (no network, no SOAR)."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from soar_form_service_utils import FormServiceClient, FormServiceError, validate_link_id

KEY = "secret-test-key"
LINK_ID = "0123456789abcdef"
STATUS = {
    "link_id": LINK_ID,
    "status": "open",
    "form": "evidence_upload",
    "container_id": 7,
    "created_at": 1.0,
    "expires_at": 2.0,
    "used_at": None,
}


class FakeService(BaseHTTPRequestHandler):
    calls: list = []
    override: tuple | None = None  # (status, body bytes, extra headers)

    def log_message(self, *a):
        pass

    def _reply(self, status, obj=None, raw=None, headers=None):
        data = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        FakeService.calls.append((self.command, self.path, self.headers.get("Authorization"), body))
        if FakeService.override:
            status, raw, headers = FakeService.override
            return self._reply(status, raw=raw, headers=headers)
        if self.path == "/healthz":
            return self._reply(200, {"ok": True})
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            return self._reply(401, {"error": "unauthorized"})
        if self.command == "GET" and self.path == "/api/forms":
            return self._reply(200, {"forms": [{"name": "evidence_upload", "title": "T", "fields": []}]})
        if self.command == "POST" and self.path == "/api/links":
            return self._reply(201, {"link_id": LINK_ID, "url": "https://forms.example/f/tok", "expires_at": 2.0})
        if self.path == f"/api/links/{LINK_ID}":
            return self._reply(200, {**STATUS, "status": "revoked" if self.command == "DELETE" else "open"})
        return self._reply(404, {"error": "unknown link_id"})

    do_GET = do_POST = do_DELETE = _handle


class ClientTest(unittest.TestCase):
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
        self.client = FormServiceClient(self.url + "/", KEY, timeout=5)

    def test_health_without_auth(self):
        self.assertEqual(self.client.health(), {"ok": True})
        self.assertIsNone(FakeService.calls[0][2])

    def test_list_forms_sends_bearer(self):
        self.assertEqual(self.client.list_forms()[0]["name"], "evidence_upload")
        self.assertEqual(FakeService.calls[0][:3], ("GET", "/api/forms", f"Bearer {KEY}"))

    def test_create_link(self):
        out = self.client.create_link("evidence_upload", "7", ttl_minutes=60.0)
        self.assertEqual(out["link_id"], LINK_ID)
        self.assertEqual((out["form"], out["container_id"]), ("evidence_upload", 7))
        self.assertEqual(FakeService.calls[0][3], {"form": "evidence_upload", "container_id": 7, "ttl_minutes": 60})

    def test_create_link_without_ttl_uses_service_default(self):
        self.client.create_link("evidence_upload", 7, ttl_minutes="")
        self.assertNotIn("ttl_minutes", FakeService.calls[0][3])

    def test_create_link_rejects_bad_input_before_calling(self):
        for form, cid, ttl in (("../etc", 7, None), ("evidence_upload", 0, None), ("evidence_upload", 7, 1.5)):
            with self.assertRaises(FormServiceError):
                self.client.create_link(form, cid, ttl)
        self.assertEqual(FakeService.calls, [])

    def test_create_link_detects_old_service(self):
        FakeService.override = (201, json.dumps({"url": "x", "expires_at": 1}).encode(), {})
        with self.assertRaisesRegex(FormServiceError, "link_id"):
            self.client.create_link("evidence_upload", 7)

    def test_status_and_revoke(self):
        self.assertEqual(self.client.link_status(LINK_ID)["status"], "open")
        self.assertEqual(self.client.revoke_link(LINK_ID)["status"], "revoked")
        self.assertEqual(
            [c[:2] for c in FakeService.calls], [("GET", f"/api/links/{LINK_ID}"), ("DELETE", f"/api/links/{LINK_ID}")]
        )

    def test_unknown_link_is_404(self):
        with self.assertRaises(FormServiceError) as ctx:
            self.client.link_status("fedcba9876543210")
        self.assertEqual(ctx.exception.status, 404)
        self.assertIn("unknown link_id", str(ctx.exception))

    def test_wrong_key_message_has_no_key(self):
        client = FormServiceClient(self.url, "wrong-key-value")
        with self.assertRaises(FormServiceError) as ctx:
            client.list_forms()
        self.assertEqual(ctx.exception.status, 401)
        self.assertNotIn("wrong-key-value", str(ctx.exception))

    def test_key_echoed_by_service_is_masked(self):
        FakeService.override = (500, json.dumps({"error": f"bad {KEY}"}).encode(), {})
        with self.assertRaises(FormServiceError) as ctx:
            self.client.list_forms()
        self.assertNotIn(KEY, str(ctx.exception))

    def test_redirect_is_not_followed(self):
        FakeService.override = (302, b"", {"Location": "http://127.0.0.1:1/steal"})
        with self.assertRaisesRegex(FormServiceError, "redirect 302"):
            self.client.list_forms()
        self.assertEqual(len(FakeService.calls), 1)

    def test_non_json_response(self):
        FakeService.override = (200, b"<html>proxy</html>", {})
        with self.assertRaisesRegex(FormServiceError, "JSON"):
            self.client.list_forms()

    def test_unreachable(self):
        client = FormServiceClient("http://127.0.0.1:1", KEY, timeout=2)
        with self.assertRaisesRegex(FormServiceError, "not reachable"):
            client.health()

    def test_config_validation(self):
        for url in ("ftp://x", "file:///etc/passwd", "forms.example", ""):
            with self.assertRaises(FormServiceError):
                FormServiceClient(url, KEY)
        with self.assertRaises(FormServiceError):
            FormServiceClient(self.url, "")

    def test_validate_link_id(self):
        self.assertEqual(validate_link_id(f" {LINK_ID} "), LINK_ID)
        for bad in ("", None, "../forms", LINK_ID.upper(), LINK_ID + "0"):
            with self.assertRaises(FormServiceError):
                validate_link_id(bad)


if __name__ == "__main__":
    unittest.main()
