"""HTTP client for the form service API. No `phantom` imports, so it runs and is tested outside SOAR.

Endpoints (see `service/soar_form/app.py`):
  GET    /healthz             (no auth)
  GET    /api/forms           -> {"forms": [{"name", "title", "fields"}]}
  POST   /api/links           -> {"link_id", "url", "expires_at"}
  GET    /api/links/<id>      -> link status
  DELETE /api/links/<id>      -> revoke, returns link status
"""

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

LINK_ID_RE = re.compile(r"^[0-9a-f]{16}$")
FORM_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
LINK_STATUSES = ("open", "claimed", "used", "revoked", "expired")


class FormServiceError(Exception):
    """Error talking to the form service. The message never contains the API key."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would resend the Authorization header to the redirect target; refuse redirects instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FormServiceError(f"Form service answered with redirect {code}; check base_url", code)


def validate_link_id(link_id) -> str:
    link_id = str(link_id or "").strip()
    if not LINK_ID_RE.match(link_id):
        raise FormServiceError("Invalid link_id: expected 16 hex characters as returned by 'create form link'")
    return link_id


def validate_form_name(form) -> str:
    form = str(form or "").strip()
    if not FORM_NAME_RE.match(form):
        raise FormServiceError("Invalid form name: use lowercase letters, digits, '_' or '-' (see 'list forms')")
    return form


def _positive_int(value, name: str, minimum: int) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise FormServiceError(f"{name} must be a whole number") from None
    if number != int(number) or number < minimum:
        raise FormServiceError(f"{name} must be a whole number >= {minimum}")
    return int(number)


class FormServiceClient:
    def __init__(self, base_url: str, api_key: str, verify: bool = True, timeout: float = 30):
        parsed = urllib.parse.urlsplit(str(base_url or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise FormServiceError("base_url must start with http:// or https:// and name a host")
        if not api_key:
            raise FormServiceError("api_key is missing in the asset configuration")
        self.base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        self._api_key = api_key
        self.timeout = _positive_int(timeout or 30, "timeout", 1)
        context = ssl.create_default_context()
        if not verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _NoRedirect())

    # ---- transport ---------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None, auth: bool = True) -> dict:
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._api_key}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        # scheme is restricted to http/https in __init__
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)  # noqa: S310
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            with exc:
                raise FormServiceError(self._error_text(exc.code, exc.read()), exc.code) from None
        except FormServiceError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise FormServiceError(f"Form service not reachable at {self.base_url}: {reason}") from None
        try:
            out = json.loads(raw or b"{}")
        except ValueError:
            raise FormServiceError("Form service returned no valid JSON") from None
        if not isinstance(out, dict):
            raise FormServiceError("Form service returned unexpected JSON")
        return out

    def _error_text(self, status: int, raw: bytes) -> str:
        try:
            detail = json.loads(raw).get("error")
        except (ValueError, AttributeError):
            detail = None
        hint = {401: "API key rejected", 404: "not found", 400: "bad request"}.get(status, "request failed")
        text = f"Form service HTTP {status}: {hint}" + (f" ({detail})" if detail else "")
        return text.replace(self._api_key, "***")

    # ---- API -----------------------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/healthz", auth=False)

    def list_forms(self) -> list[dict]:
        forms = self._request("GET", "/api/forms").get("forms")
        if not isinstance(forms, list):
            raise FormServiceError("Form service returned no form list")
        return forms

    def create_link(self, form, container_id, ttl_minutes=None) -> dict:
        body = {"form": validate_form_name(form), "container_id": _positive_int(container_id, "container_id", 1)}
        if ttl_minutes not in (None, ""):
            body["ttl_minutes"] = _positive_int(ttl_minutes, "ttl_minutes", 1)
        out = self._request("POST", "/api/links", body)
        for key in ("link_id", "url", "expires_at"):
            if key not in out:
                raise FormServiceError(f"Form service response misses '{key}'; is the service up to date?")
        return {**out, "form": body["form"], "container_id": body["container_id"]}

    def link_status(self, link_id) -> dict:
        return self._request("GET", f"/api/links/{validate_link_id(link_id)}")

    def revoke_link(self, link_id) -> dict:
        return self._request("DELETE", f"/api/links/{validate_link_id(link_id)}")
