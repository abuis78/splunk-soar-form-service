"""Integration test: connector against the real form service (`service/soar_form`) with a fake SOAR.

The service source is taken from $FORM_SERVICE_SRC, default: `service/` in this repo.
"""

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests import fake_phantom

fake_phantom.install()

from soar_form_service_connector import FormServiceConnector  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SRC = Path(os.environ.get("FORM_SERVICE_SRC", ROOT / "service"))
KEY = "integration-key"

try:
    sys.path.insert(0, str(SRC))
    from soar_form.app import Config, make_handler
    from soar_form.soar import SoarClient
    from soar_form.store import LinkStore

    HAVE_SERVICE = True
except ImportError:
    HAVE_SERVICE = False
finally:
    if sys.path and sys.path[0] == str(SRC):
        sys.path.pop(0)


class FakeSoar(BaseHTTPRequestHandler):
    artifacts: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/rest/container_attachment":
            out = {"succeeded": True, "vault_id": "v1"}
        else:
            FakeSoar.artifacts.append(body)
            out = {"success": True, "id": len(FakeSoar.artifacts)}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def http(url, body=None, ctype=None):
    req = urllib.request.Request(url, data=body, method="POST" if body else "GET")
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        with e:
            return e.code


def submission() -> tuple[bytes, str]:
    boundary = "----integration"
    fields = {"reporter_name": "Max", "reporter_email": "max@example.com", "category": "Phishing", "details": "x"}
    out = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()
    )
    out += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="evidence"; filename="mail.eml"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + b"From: a@example.com\r\n\r\nhi\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


@unittest.skipUnless(HAVE_SERVICE, f"form service source not found at {SRC} (set FORM_SERVICE_SRC)")
class IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.soar_srv, soar_url = serve(FakeSoar)
        cls.store = LinkStore()
        cfg = Config(base_url="https://forms.example", api_key=KEY, forms_dir=SRC / "forms")
        cls.app_srv, cls.url = serve(make_handler(cfg, cls.store, SoarClient(soar_url, "soar-token")))

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.app_srv, cls.soar_srv):
            srv.shutdown()
            srv.server_close()
        cls.store.close()

    def act(self, action_id, param=None, container_id=5):
        conn = FormServiceConnector()
        conn.config = {"base_url": self.url, "api_key": KEY, "timeout": 5}
        conn.action_id, conn.container_id = action_id, container_id
        self.assertTrue(conn.initialize(), conn.status)
        conn.handle_action(param or {})
        result = conn.action_results[0]
        self.assertTrue(result.status, f"{action_id}: {result.message}")
        return result

    def local(self, url):
        return self.url + url.removeprefix("https://forms.example")

    def test_full_lifecycle(self):
        self.act("test_connectivity")
        forms = self.act("list_forms").data
        self.assertIn("evidence_upload", [f["name"] for f in forms])

        link = self.act("create_form_link", {"form": "evidence_upload", "ttl_minutes": 10}).data[0]
        self.assertEqual(link["container_id"], 5)
        self.assertEqual(self.act("get_link_status", {"link_id": link["link_id"]}).data[0]["status"], "open")

        body, ctype = submission()
        self.assertEqual(http(self.local(link["url"]), body, ctype), 200)
        self.assertEqual(FakeSoar.artifacts[-1]["container_id"], 5)
        self.assertEqual(FakeSoar.artifacts[-1]["cef"]["evidence_vault_id"], "v1")

        status = self.act("get_link_status", {"link_id": link["link_id"]}).data[0]
        self.assertEqual(status["status"], "used")
        self.assertIsNotNone(status["used_at"])
        self.assertEqual(self.act("revoke_link", {"link_id": link["link_id"]}).data[0]["status"], "used")

    def test_revoke_blocks_form(self):
        link = self.act("create_form_link", {"form": "evidence_upload"}).data[0]
        self.assertEqual(http(self.local(link["url"])), 200)
        self.assertEqual(self.act("revoke_link", {"link_id": link["link_id"]}).summary, {"status": "revoked"})
        self.assertEqual(http(self.local(link["url"])), 410)


if __name__ == "__main__":
    unittest.main()
