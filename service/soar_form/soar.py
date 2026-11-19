"""Minimal Splunk SOAR REST client (stdlib only).

Endpoints used (SOAR On-prem 8.7 REST reference):
- POST /rest/container_attachment  -> adds a file to the container vault, returns vault_id
- POST /rest/artifact              -> adds the form answers; run_automation triggers active playbooks
"""

import base64
import json
import ssl
import urllib.error
import urllib.request


class SoarError(RuntimeError):
    pass


class SoarClient:
    def __init__(self, base_url: str, token: str, verify_tls: bool = True, timeout: float = 30):
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("SOAR_URL must start with https:// or http://")
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._ctx = None if verify_tls else ssl._create_unverified_context()  # noqa: S323 opt-in for lab certs

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(  # noqa: S310 scheme checked in __init__
            f"{self._base}{path}",
            data=json.dumps(body).encode(),
            headers={"ph-auth-token": self._token, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=self._ctx) as resp:  # noqa: S310
                data = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            with exc:
                detail = exc.read()[:300]
            raise SoarError(f"{path}: HTTP {exc.code} {detail!r}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SoarError(f"{path}: {exc}") from exc
        if data.get("failed"):
            raise SoarError(f"{path}: {data.get('message')}")
        return data

    def add_attachment(self, container_id: int, file_name: str, content: bytes) -> str:
        data = self._post(
            "/rest/container_attachment",
            {
                "container_id": container_id,
                "file_name": file_name,
                "file_content": base64.b64encode(content).decode(),
                "metadata": {"contains": ["vault id"]},
            },
        )
        return data["vault_id"]

    def add_artifact(self, container_id: int, name: str, label: str, cef: dict, data: dict) -> int:
        resp = self._post(
            "/rest/artifact",
            {
                "container_id": container_id,
                "name": name,
                "label": label,
                "cef": cef,
                "data": data,
                "run_automation": True,
            },
        )
        return resp["id"]
