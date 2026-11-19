"""Live end-to-end test of the form service against a real SOAR instance.

Creates a test container, runs the form service in-process, requests a magic link, submits the form
with a test file, then checks in SOAR that the artifact and the vault file exist (vault hash == SHA-1
of the uploaded bytes). The container is left in place so it can be inspected in the UI.

Token: $SOAR_TOKEN, or on macOS the Keychain item  -s soar-form -a soar-token
URL:   $SOAR_URL (required)

    python3 scripts/live_test.py            # run
    python3 scripts/live_test.py --dry-run  # only check config and SOAR reachability (read-only)
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from soar_form.app import Config, make_handler  # noqa: E402
from soar_form.soar import SoarClient  # noqa: E402
from soar_form.store import LinkStore  # noqa: E402


def load_token() -> str:
    if os.environ.get("SOAR_TOKEN"):
        return os.environ["SOAR_TOKEN"]
    if shutil.which("security"):
        out = subprocess.run(  # noqa: S603
            ["security", "find-generic-password", "-s", "soar-form", "-a", "soar-token", "-w"],  # noqa: S607
            capture_output=True,
            text=True,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    sys.exit("No SOAR token: set SOAR_TOKEN or store it in the Keychain (service soar-form, account soar-token).")


class Soar:
    """Small helper for the calls only this test needs (GET + container create)."""

    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(  # noqa: S310 scheme checked by SoarClient
            self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"ph-auth-token": self.token, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                try:
                    return e.code, json.loads(e.read() or b"{}")
                except ValueError:
                    return e.code, {}


def step(msg: str):
    print(f"• {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="read-only: check token and /rest/version")
    ap.add_argument("--label", default="events", help="container label (default: events)")
    args = ap.parse_args()

    url = os.environ.get("SOAR_URL") or sys.exit("Set SOAR_URL, e.g. https://soar.example.com")
    soar = Soar(url, load_token())
    client = SoarClient(url, soar.token)  # validates the scheme

    status, version = soar.call("GET", "/rest/version")
    if status != 200:
        print(f"FAILED: /rest/version → HTTP {status} {version}")
        return 1
    step(f"SOAR reachable, version {version.get('version')}")
    if args.dry_run:
        print("DRY RUN OK – nothing was written.")
        return 0

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    status, cont = soar.call(
        "POST",
        "/rest/container",
        {
            "name": f"form service live test {stamp}",
            "label": args.label,
            "severity": "low",
            "description": "Created by scripts/live_test.py (issue #2). Safe to delete.",
            "tags": ["form_service_live_test"],
        },
    )
    if status != 200 or not cont.get("id"):
        print(f"FAILED: create container → HTTP {status} {cont}")
        return 1
    container_id = cont["id"]
    step(f"container {container_id} created")

    store = LinkStore()
    cfg = Config(base_url="http://127.0.0.1", api_key="live-test", forms_dir=ROOT / "forms")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, store, client))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        req = urllib.request.Request(
            f"{base}/api/links",
            json.dumps({"container_id": container_id, "form": "evidence_upload", "ttl_minutes": 10}).encode(),
            {"Authorization": "Bearer live-test"},
            method="POST",
        )
        link_path = urllib.parse.urlparse(json.loads(urllib.request.urlopen(req).read())["url"]).path  # noqa: S310
        step("magic link created")

        content = f"form service live test {stamp}\n".encode() + bytes(range(256))
        boundary = "----livetest"
        fields = {
            "reporter_name": "Live Test",
            "reporter_email": "live-test@example.com",
            "category": "Phishing",
            "details": "Automated live test of the form spike.",
        }
        body = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
            for k, v in fields.items()
        )
        body += (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="evidence"; filename="live_test.txt"\r\n'
                "Content-Type: text/plain\r\n\r\n"
            ).encode()
            + content
            + f"\r\n--{boundary}--\r\n".encode()
        )
        req = urllib.request.Request(
            base + link_path, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req) as r:  # noqa: S310
                submit_status = r.status
        except urllib.error.HTTPError as e:
            with e:
                submit_status = e.code
        if submit_status != 200:
            print(f"FAILED: form submit → HTTP {submit_status} (see log line above)")
            return 1
        step("form submitted")
    finally:
        srv.shutdown()
        srv.server_close()
        store.close()

    # ---- verify in SOAR ----------------------------------------------------
    status, arts = soar.call("GET", f"/rest/artifact?_filter_container_id={container_id}&page_size=50")
    form_arts = [a for a in arts.get("data", []) if a.get("label") == "form_response"]
    if status != 200 or len(form_arts) != 1:
        print(f"FAILED: expected 1 form_response artifact, got HTTP {status}, {len(form_arts)}")
        return 1
    art = form_arts[0]
    vault_id = art.get("cef", {}).get("evidence_vault_id")
    expected = hashlib.sha1(content, usedforsecurity=False).hexdigest()
    step(f"artifact {art['id']} found, cef.evidence_vault_id={vault_id}")

    status, docs = soar.call("GET", f'/rest/vault_document?_filter_hash="{vault_id}"')
    hashes = [d.get("hash") for d in docs.get("data", [])]
    step(f"vault_document lookup → HTTP {status}, hashes {hashes}")

    ok = vault_id == expected and expected in hashes
    print(
        f"{'LIVE TEST PASSED' if ok else 'LIVE TEST FAILED'}: container={container_id} artifact={art['id']} "
        f"vault_id={vault_id} sha1(upload)={expected}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
