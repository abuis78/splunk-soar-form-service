"""End-to-end tests: form service against a fake SOAR REST API (no network)."""

import base64
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from soar_form.app import Config, make_handler
from soar_form.soar import SoarClient
from soar_form.store import LinkStore

FORMS = Path(__file__).resolve().parent.parent / "forms"
API_KEY = "test-key"
SOAR_TOKEN = "soar-token"


class FakeSoar(BaseHTTPRequestHandler):
    calls: list = []
    fail = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeSoar.calls.append((self.path, self.headers.get("ph-auth-token"), body))
        if FakeSoar.fail:
            out, status = {"failed": True, "message": "boom"}, 500
        elif self.path == "/rest/container_attachment":
            out, status = {"succeeded": True, "vault_id": "abc123", "container": body["container_id"]}, 200
        else:
            out, status = {"success": True, "id": 41}, 200
        data = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def multipart(fields: dict, files: dict) -> tuple[str, bytes]:
    boundary = "----spikeboundary"
    out = b""
    for k, v in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k, (fname, content) in files.items():
        out += (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + content
            + b"\r\n"
        )
    out += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", out


def request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read().decode()


VALID = {"reporter_name": "Max", "reporter_email": "max@example.com", "category": "Phishing", "details": "Mail"}
EML = b"From: evil@example.com\r\nSubject: win\r\n\r\n\x00\xffbinary"


class FlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.soar_srv, soar_url = serve(FakeSoar)
        cfg = Config(base_url="https://forms.example", api_key=API_KEY, forms_dir=FORMS, max_bytes=64 * 1024)
        cls.store = LinkStore()
        cls.app_srv, cls.url = serve(make_handler(cfg, cls.store, SoarClient(soar_url, SOAR_TOKEN)))

    @classmethod
    def tearDownClass(cls):
        for srv in (cls.app_srv, cls.soar_srv):
            srv.shutdown()
            srv.server_close()
        cls.store.close()

    def setUp(self):
        FakeSoar.calls, FakeSoar.fail = [], False

    def api(self, method, path):
        status, body = request(f"{self.url}{path}", method, headers={"Authorization": f"Bearer {API_KEY}"})
        return status, json.loads(body)

    def create(self, ttl_minutes=60) -> dict:
        status, body = request(
            f"{self.url}/api/links",
            "POST",
            json.dumps({"container_id": 7, "form": "evidence_upload", "ttl_minutes": ttl_minutes}).encode(),
            {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        )
        self.assertEqual(status, 201, body)
        out = json.loads(body)
        self.assertTrue(out["url"].startswith("https://forms.example/f/"))
        return out

    def local(self, created: dict) -> str:
        return self.url + created["url"].removeprefix("https://forms.example")

    def new_link(self, ttl_minutes=60) -> str:
        return self.local(self.create(ttl_minutes))

    def submit(self, link, fields=VALID, files=None):
        ctype, body = multipart(fields, {"evidence": ("mail.eml", EML)} if files is None else files)
        return request(link, "POST", body, {"Content-Type": ctype})

    def test_link_api_requires_key(self):
        status, _ = request(f"{self.url}/api/links", "POST", b"{}", {"Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)

    def test_unknown_form_rejected(self):
        status, _ = request(
            f"{self.url}/api/links",
            "POST",
            json.dumps({"container_id": 1, "form": "../etc"}).encode(),
            {"Authorization": f"Bearer {API_KEY}"},
        )
        self.assertEqual(status, 400)

    def test_happy_path_uploads_file_and_creates_artifact(self):
        link = self.new_link()
        status, html = request(link)
        self.assertEqual(status, 200)
        self.assertIn("type='file'", html)

        status, html = self.submit(link)
        self.assertEqual(status, 200, html)
        self.assertIn("Danke", html)

        (p1, tok1, att), (p2, tok2, art) = FakeSoar.calls
        self.assertEqual((p1, p2), ("/rest/container_attachment", "/rest/artifact"))
        self.assertEqual((tok1, tok2), (SOAR_TOKEN, SOAR_TOKEN))
        self.assertEqual(att["container_id"], 7)
        self.assertEqual(att["file_name"], "mail.eml")
        self.assertEqual(base64.b64decode(att["file_content"]), EML)
        self.assertEqual(art["cef"]["evidence_vault_id"], "abc123")
        self.assertEqual(art["cef"]["category"], "Phishing")
        self.assertEqual(art["label"], "form_response")
        self.assertTrue(art["run_automation"])

    def test_link_is_single_use(self):
        link = self.new_link()
        self.assertEqual(self.submit(link)[0], 200)
        self.assertEqual(self.submit(link)[0], 410)
        self.assertEqual(request(link)[0], 410)

    def test_expired_link(self):
        link = self.new_link(ttl_minutes=0)
        self.assertEqual(request(link)[0], 410)

    def test_unknown_token(self):
        self.assertEqual(request(f"{self.url}/f/nope")[0], 410)

    def test_validation_errors_keep_link_open(self):
        link = self.new_link()
        status, html = self.submit(link, {**VALID, "category": "Hack"}, files={})
        self.assertEqual(status, 422)
        self.assertIn("Beweisdatei", html)
        self.assertIn("ungültige Auswahl", html)
        self.assertEqual(FakeSoar.calls, [])
        self.assertEqual(self.submit(link)[0], 200)

    def test_wrong_file_type(self):
        link = self.new_link()
        status, html = self.submit(link, files={"evidence": ("run.exe", b"MZ")})
        self.assertEqual(status, 422)
        self.assertIn(".eml", html)

    def test_too_large(self):
        link = self.new_link()
        status, _ = self.submit(link, files={"evidence": ("big.zip", b"x" * 70 * 1024)})
        self.assertEqual(status, 413)
        self.assertEqual(FakeSoar.calls, [])

    def test_soar_failure_reopens_link(self):
        link = self.new_link()
        FakeSoar.fail = True
        self.assertEqual(self.submit(link)[0], 502)
        FakeSoar.fail = False
        self.assertEqual(self.submit(link)[0], 200)

    def test_html_is_escaped(self):
        link = self.new_link()
        _, html = self.submit(link, {**VALID, "reporter_name": "<script>x</script>", "category": "?"}, files={})
        self.assertNotIn("<script>x", html)

    # ---- link API for the SOAR app (issue #8) ----------------------------------
    def test_link_id_is_independent_of_token(self):
        created = self.create()
        self.assertRegex(created["link_id"], r"^[0-9a-f]{16}$")
        self.assertNotIn(created["link_id"], created["url"])

    def test_status_lifecycle_open_to_used(self):
        created = self.create()
        status, info = self.api("GET", f"/api/links/{created['link_id']}")
        self.assertEqual(status, 200)
        self.assertEqual((info["status"], info["form"], info["container_id"]), ("open", "evidence_upload", 7))
        self.assertIsNone(info["used_at"])
        self.assertEqual(self.submit(self.local(created))[0], 200)
        _, info = self.api("GET", f"/api/links/{created['link_id']}")
        self.assertEqual(info["status"], "used")
        self.assertIsNotNone(info["used_at"])

    def test_status_expired(self):
        created = self.create(ttl_minutes=0)
        self.assertEqual(self.api("GET", f"/api/links/{created['link_id']}")[1]["status"], "expired")

    def test_revoke_closes_link(self):
        created = self.create()
        status, info = self.api("DELETE", f"/api/links/{created['link_id']}")
        self.assertEqual((status, info["status"]), (200, "revoked"))
        self.assertEqual(request(self.local(created))[0], 410)
        self.assertEqual(self.submit(self.local(created))[0], 410)
        self.assertEqual(FakeSoar.calls, [])

    def test_revoke_keeps_used_state(self):
        created = self.create()
        self.assertEqual(self.submit(self.local(created))[0], 200)
        self.assertEqual(self.api("DELETE", f"/api/links/{created['link_id']}")[1]["status"], "used")

    def test_unknown_or_malformed_link_id(self):
        self.assertEqual(self.api("GET", "/api/links/0123456789abcdef")[0], 404)
        self.assertEqual(self.api("DELETE", "/api/links/0123456789abcdef")[0], 404)
        self.assertEqual(self.api("GET", "/api/links/../forms")[0], 404)

    def test_link_api_endpoints_require_key(self):
        link_id = self.create()["link_id"]
        for method, path in (
            ("GET", f"/api/links/{link_id}"),
            ("DELETE", f"/api/links/{link_id}"),
            ("GET", "/api/forms"),
        ):
            status, _ = request(f"{self.url}{path}", method, headers={"Authorization": "Bearer wrong"})
            self.assertEqual(status, 401, path)
        self.assertEqual(self.api("GET", f"/api/links/{link_id}")[1]["status"], "open")

    def test_list_forms(self):
        status, out = self.api("GET", "/api/forms")
        self.assertEqual(status, 200)
        form = next(f for f in out["forms"] if f["name"] == "evidence_upload")
        self.assertIn({"name": "evidence", "type": "file", "required": True}, form["fields"])


class StoreMigrationTest(unittest.TestCase):
    def test_old_database_gets_link_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/links.db"
            db = sqlite3.connect(path)
            db.execute(
                "CREATE TABLE links (token_hash TEXT PRIMARY KEY, container_id INTEGER NOT NULL,"
                " form TEXT NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'open')"
            )
            db.execute("INSERT INTO links VALUES ('h1', 1, 'evidence_upload', 9999999999, 'open')")
            db.commit()
            db.close()
            store = LinkStore(path)
            try:
                (link_id,) = store._db.execute("SELECT link_id FROM links").fetchone()
                self.assertRegex(link_id, r"^[0-9a-f]{16}$")
                self.assertEqual(store.info(link_id)["status"], "open")
                token, new_id, _ = store.create(2, "evidence_upload", 60)
                self.assertNotEqual(new_id, link_id)
                self.assertIsNotNone(store.peek(token))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
