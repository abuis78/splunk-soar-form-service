"""Form definitions (JSON), multipart parsing, validation and HTML rendering."""

import html
import json
import re
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path

FIELD_TYPES = {"text", "email", "textarea", "select", "file"}
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass
class UploadedFile:
    filename: str
    content: bytes


@dataclass
class Submission:
    values: dict[str, str] = field(default_factory=dict)
    files: dict[str, UploadedFile] = field(default_factory=dict)


def load_form(forms_dir: Path, name: str) -> dict:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid form name: {name!r}")
    form = json.loads((forms_dir / f"{name}.json").read_text(encoding="utf-8"))
    for f in form["fields"]:
        if f["type"] not in FIELD_TYPES or not _NAME_RE.match(f["name"]):
            raise ValueError(f"invalid field in form {name}: {f}")
    return form


def list_forms(forms_dir: Path) -> list[dict]:
    """Summaries of all valid forms; invalid files are skipped so one bad file does not break the list."""
    out = []
    for path in sorted(forms_dir.glob("*.json")):
        try:
            form = load_form(forms_dir, path.stem)
        except (ValueError, KeyError, OSError):
            continue
        fields = [{"name": f["name"], "type": f["type"], "required": bool(f.get("required"))} for f in form["fields"]]
        out.append({"name": path.stem, "title": form.get("title", path.stem), "fields": fields})
    return out


def parse_multipart(content_type: str, body: bytes) -> Submission:
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body)
    if not msg.is_multipart():
        raise ValueError("expected multipart/form-data")
    sub = Submission()
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            if filename:  # empty filename = no file chosen
                sub.files[name] = UploadedFile(Path(filename).name, payload)
        else:
            sub.values[name] = payload.decode("utf-8", errors="replace")
    return sub


def validate(form: dict, sub: Submission) -> list[str]:
    errors = []
    for f in form["fields"]:
        label, name = f.get("label", f["name"]), f["name"]
        if f["type"] == "file":
            up = sub.files.get(name)
            if not up:
                if f.get("required"):
                    errors.append(f"{label}: bitte eine Datei auswählen.")
                continue
            accept = [a.strip().lower() for a in f.get("accept", "").split(",") if a.strip()]
            if accept and not any(up.filename.lower().endswith(a) for a in accept):
                errors.append(f"{label}: erlaubt sind {', '.join(accept)}.")
            continue
        value = sub.values.get(name, "").strip()
        if not value:
            if f.get("required"):
                errors.append(f"{label}: Pflichtfeld.")
            continue
        if f["type"] == "select" and value not in f.get("options", []):
            errors.append(f"{label}: ungültige Auswahl.")
        if len(value) > f.get("max_length", 5000):
            errors.append(f"{label}: zu lang.")
    return errors


_STYLE = """
body{font-family:system-ui,sans-serif;max-width:640px;margin:2rem auto;padding:0 16px;color:#1d1d1f;background:#fff}
label{display:block;font-weight:600;margin-top:1rem}
input,select,textarea{width:100%;box-sizing:border-box;padding:.5rem;margin-top:.25rem;font:inherit}
button{margin-top:1.5rem;padding:.6rem 1.2rem;font:inherit}
.err{background:#fde8e8;padding:.75rem;border-radius:6px}
"""


def page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='de'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def render_form(form: dict, values: dict | None = None, errors: list[str] | None = None) -> str:
    values = values or {}
    esc = html.escape
    parts = [f"<h1>{esc(form['title'])}</h1>"]
    if form.get("description"):
        parts.append(f"<p>{esc(form['description'])}</p>")
    if errors:
        parts.append("<div class='err'><ul>" + "".join(f"<li>{esc(e)}</li>" for e in errors) + "</ul></div>")
    parts.append("<form method='post' enctype='multipart/form-data'>")
    for f in form["fields"]:
        name, label = esc(f["name"]), esc(f.get("label", f["name"]))
        req = " required" if f.get("required") else ""
        val = esc(values.get(f["name"], ""))
        parts.append(f"<label for='{name}'>{label}</label>")
        if f["type"] == "textarea":
            parts.append(f"<textarea id='{name}' name='{name}' rows='5'{req}>{val}</textarea>")
        elif f["type"] == "select":
            opts = "".join(
                f"<option{' selected' if o == values.get(f['name']) else ''}>{esc(o)}</option>"
                for o in f.get("options", [])
            )
            parts.append(f"<select id='{name}' name='{name}'{req}><option value=''>–</option>{opts}</select>")
        elif f["type"] == "file":
            accept = f" accept='{esc(f['accept'])}'" if f.get("accept") else ""
            parts.append(f"<input type='file' id='{name}' name='{name}'{accept}{req}>")
        else:
            parts.append(f"<input type='{f['type']}' id='{name}' name='{name}' value='{val}'{req}>")
    parts.append("<button type='submit'>Absenden</button></form>")
    return page(form["title"], "".join(parts))
