# Form service

Small web service that gives external users a **single-use magic link** to a custom form with file upload and
hands the result to Splunk SOAR: files → `POST /rest/container_attachment` (vault), answers →
`POST /rest/artifact` (label from the form, `cef` = fields + `<field>_vault_id`, `run_automation: true`).
Standard library only (Python ≥ 3.11). The SOAR app in the repo root drives it from playbooks.

## Run

```bash
cd service
FORM_BASE_URL=https://forms.example.com FORM_API_KEY=... SOAR_URL=https://soar.example.com SOAR_TOKEN=... \
  python3 -m soar_form
```

Or as a container (see `Dockerfile`): `docker build -t soar-form service/`.

| Env | Default | Meaning |
|---|---|---|
| `FORM_BASE_URL` | – (required) | Public URL used in the magic links |
| `FORM_API_KEY` | – (required) | Bearer key for `/api/*` (same value as the app asset `api_key`) |
| `SOAR_URL`, `SOAR_TOKEN` | – (required) | SOAR REST API and automation user token (`ph-auth-token`) |
| `SOAR_VERIFY_TLS` | `1` | `0` for lab certificates |
| `FORM_HOST`, `FORM_PORT` | `127.0.0.1`, `8080` | Listen address |
| `FORM_DIR` | `service/forms` | Form definitions `<name>.json` |
| `FORM_MAX_MB` | `10` | Upload limit |
| `FORM_STATE_DIR` | `~/.local/state/soar-form` | Holds `links.db` (token hashes only); keep it when moving hosts |

Put it behind an HTTPS reverse proxy; only `/f/*` needs to be public, `/api/*` only for SOAR.

## API

| Route | Auth | Purpose |
|---|---|---|
| `POST /api/links` `{container_id, form, ttl_minutes}` | Bearer | → `{link_id, url, expires_at}` |
| `GET /api/links/<link_id>` | Bearer | status `open`, `claimed`, `used`, `revoked`, `expired` |
| `DELETE /api/links/<link_id>` | Bearer | revoke an open link |
| `GET /api/forms` | Bearer | form names, titles, fields |
| `GET`/`POST /f/<token>` | public | form page / submit |
| `GET /healthz` | – | liveness |

## Forms

`forms/<name>.json`: `title`, `description`, `label` (artifact label, default `form_response`), `fields` with
types `text`, `email`, `textarea`, `select` (`options`), `file` (`accept`). See `forms/evidence_upload.json`.

## Tests

```bash
python3 -m unittest discover -s service/tests -t service     # fake SOAR, no network
SOAR_URL=https://<soar-host> python3 service/scripts/live_test.py --dry-run   # live check, read-only
```

`live_test.py` without `--dry-run` writes a test container to the given SOAR — only with the owner's go.
