"""HTTP server: link API for playbooks + public magic-link form pages.

Routes
  POST   /api/links        (Bearer key) -> {"link_id", "url", "expires_at"}
  GET    /api/links/<id>   (Bearer key) -> link status (open|claimed|used|revoked|expired)
  DELETE /api/links/<id>   (Bearer key) -> revoke an open link, returns its status
  GET    /api/forms        (Bearer key) -> {"forms": [{"name", "title", "fields"}]}
  GET  /f/<token>      render the form
  POST /f/<token>      multipart submit -> vault upload + artifact in SOAR
  GET  /healthz
"""

import hmac
import json
import logging
import os
import re
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .forms import list_forms, load_form, page, parse_multipart, render_form, validate
from .soar import SoarClient, SoarError
from .store import LinkStore

log = logging.getLogger("soar_form")

_LINK_ID_RE = re.compile(r"^[0-9a-f]{16}$")

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
}


@dataclass
class Config:
    base_url: str  # public URL of this service, used to build magic links
    api_key: str
    forms_dir: Path
    max_bytes: int = 10 * 1024 * 1024
    default_ttl_minutes: int = 60 * 24


def make_handler(cfg: Config, store: LinkStore, soar: SoarClient):
    class Handler(BaseHTTPRequestHandler):
        server_version = "soar-form"

        def log_message(self, fmt, *args):  # never log tokens from the path
            log.info("%s %s", self.command, self.path.split("/f/")[0] + ("/f/…" if "/f/" in self.path else ""))

        # ---- helpers -------------------------------------------------------
        def _send(self, status: int, body: str, ctype: str = "text/html; charset=utf-8"):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in _SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status: int, obj: dict):
            self._send(status, json.dumps(obj), "application/json")

        def _gone(self):
            self._send(
                410,
                page(
                    "Link ungültig", "<h1>Link ungültig</h1><p>Dieser Link ist abgelaufen oder wurde schon benutzt.</p>"
                ),
            )

        def _read_body(self) -> bytes | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > cfg.max_bytes:
                self.close_connection = True  # body left unread
                return None
            return self.rfile.read(length)

        def _authorized(self) -> bool:
            auth = self.headers.get("Authorization", "")
            if hmac.compare_digest(auth.encode(), f"Bearer {cfg.api_key}".encode()):
                return True
            self._json(401, {"error": "unauthorized"})
            return False

        def _link_id(self) -> str | None:
            parts = self.path.split("?")[0].strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "links"] and _LINK_ID_RE.match(parts[2]):
                return parts[2]
            return None

        def _link_info(self, info: dict | None):
            if info is None:
                return self._json(404, {"error": "unknown link_id"})
            self._json(200, info)

        def _token(self) -> str | None:
            parts = self.path.split("?")[0].strip("/").split("/")
            return parts[1] if len(parts) == 2 and parts[0] == "f" else None

        # ---- routes --------------------------------------------------------
        def do_GET(self):
            if self.path == "/healthz":
                return self._json(200, {"ok": True})
            if self.path.startswith("/api/"):
                return self._api_get()
            token = self._token()
            link = store.peek(token) if token else None
            if not link:
                return self._gone()
            self._send(200, render_form(load_form(cfg.forms_dir, link.form)))

        def do_POST(self):
            if self.path == "/api/links":
                return self._create_link()
            token = self._token()
            if not token:
                return self._send(404, page("Nicht gefunden", "<h1>Nicht gefunden</h1>"))
            self._submit(token)

        def do_DELETE(self):
            if not self._authorized():
                return
            link_id = self._link_id()
            if not link_id:
                return self._json(404, {"error": "not found"})
            info = store.revoke(link_id)
            if info is not None:
                log.info("link %s revoke -> %s", link_id, info["status"])
            self._link_info(info)

        def _api_get(self):
            if not self._authorized():
                return
            if self.path.split("?")[0] == "/api/forms":
                return self._json(200, {"forms": list_forms(cfg.forms_dir)})
            link_id = self._link_id()
            if not link_id:
                return self._json(404, {"error": "not found"})
            self._link_info(store.info(link_id))

        def _create_link(self):
            if not self._authorized():
                return
            try:
                req = json.loads(self._read_body() or b"{}")
                container_id = int(req["container_id"])
                form = str(req["form"])
                load_form(cfg.forms_dir, form)  # fail early on unknown forms
                ttl = int(req.get("ttl_minutes", cfg.default_ttl_minutes)) * 60
            except (KeyError, ValueError, TypeError, FileNotFoundError) as exc:
                return self._json(400, {"error": str(exc)})
            token, link_id, expires_at = store.create(container_id, form, ttl)
            self._json(
                201,
                {"link_id": link_id, "url": f"{cfg.base_url.rstrip('/')}/f/{token}", "expires_at": expires_at},
            )

        def _submit(self, token: str):
            link = store.peek(token)
            if not link:
                return self._gone()
            form = load_form(cfg.forms_dir, link.form)
            body = self._read_body()
            if body is None:
                mb = cfg.max_bytes // (1024 * 1024)
                return self._send(413, render_form(form, errors=[f"Upload zu groß (max. {mb} MB)."]))
            try:
                sub = parse_multipart(self.headers.get("Content-Type", ""), body)
            except ValueError:
                return self._send(400, render_form(form, errors=["Ungültige Anfrage."]))
            errors = validate(form, sub)
            if errors:
                return self._send(422, render_form(form, sub.values, errors))

            link = store.claim(token)  # single use from here on
            if not link:
                return self._gone()
            try:
                vault_ids = {
                    name: soar.add_attachment(link.container_id, up.filename, up.content)
                    for name, up in sub.files.items()
                }
                cef = {k: v.strip() for k, v in sub.values.items() if v.strip()}
                cef.update({f"{name}_vault_id": vid for name, vid in vault_ids.items()})
                artifact_id = soar.add_artifact(
                    link.container_id,
                    name=f"Form response: {form['title']}",
                    label=form.get("label", "form_response"),
                    cef=cef,
                    data={"form": link.form, "files": {n: sub.files[n].filename for n in vault_ids}},
                )
            except SoarError as exc:
                log.error("SOAR hand-off failed for container %s: %s", link.container_id, exc)
                store.finish(token, success=False)  # let the user retry
                return self._send(
                    502,
                    render_form(
                        form, sub.values, ["Die Übermittlung ist fehlgeschlagen. Bitte später noch einmal versuchen."]
                    ),
                )
            store.finish(token, success=True)
            log.info("container %s: artifact %s, %d file(s)", link.container_id, artifact_id, len(vault_ids))
            self._send(
                200,
                page(
                    "Danke", "<h1>Danke</h1><p>Deine Angaben sind angekommen. Du kannst dieses Fenster schließen.</p>"
                ),
            )

    return Handler


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env = os.environ
    state_dir = Path(env.get("FORM_STATE_DIR", Path.home() / ".local/state/soar-form"))
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        base_url=env["FORM_BASE_URL"],
        api_key=env["FORM_API_KEY"],
        forms_dir=Path(env.get("FORM_DIR", Path(__file__).resolve().parent.parent / "forms")),
        max_bytes=int(env.get("FORM_MAX_MB", "10")) * 1024 * 1024,
    )
    soar = SoarClient(env["SOAR_URL"], env["SOAR_TOKEN"], verify_tls=env.get("SOAR_VERIFY_TLS", "1") != "0")
    store = LinkStore(str(state_dir / "links.db"))
    host, port = env.get("FORM_HOST", "127.0.0.1"), int(env.get("FORM_PORT", "8080"))
    log.info("listening on http://%s:%s (public base %s)", host, port, cfg.base_url)
    ThreadingHTTPServer((host, port), make_handler(cfg, store, soar)).serve_forever()


if __name__ == "__main__":
    main()
