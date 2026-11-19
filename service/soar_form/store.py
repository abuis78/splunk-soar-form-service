"""Single-use magic-link store backed by SQLite. Only token hashes are stored."""

import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass


@dataclass
class Link:
    container_id: int
    form: str
    expires_at: float


_COLUMNS = {"link_id": "TEXT", "created_at": "REAL", "used_at": "REAL"}
_INFO_KEYS = ("link_id", "status", "form", "container_id", "created_at", "expires_at", "used_at")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LinkStore:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS links ("
                " token_hash TEXT PRIMARY KEY,"
                " container_id INTEGER NOT NULL,"
                " form TEXT NOT NULL,"
                " expires_at REAL NOT NULL,"
                " state TEXT NOT NULL DEFAULT 'open')"  # open | claimed | used | revoked
            )
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the first release; existing links.db files stay usable."""
        have = {row[1] for row in self._db.execute("PRAGMA table_info(links)")}
        for name, kind in _COLUMNS.items():
            if name not in have:
                self._db.execute(f"ALTER TABLE links ADD COLUMN {name} {kind}")
        for (token_hash,) in self._db.execute("SELECT token_hash FROM links WHERE link_id IS NULL").fetchall():
            self._db.execute("UPDATE links SET link_id = ? WHERE token_hash = ?", (secrets.token_hex(8), token_hash))
        self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS links_link_id ON links (link_id)")

    def create(self, container_id: int, form: str, ttl_seconds: int) -> tuple[str, str, float]:
        """Return (token, link_id, expires_at). link_id is random and not derived from the token."""
        token, link_id = secrets.token_urlsafe(32), secrets.token_hex(8)
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO links (token_hash, link_id, container_id, form, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (_hash(token), link_id, container_id, form, now, now + ttl_seconds),
            )
            self._db.commit()
        return token, link_id, now + ttl_seconds

    def info(self, link_id: str) -> dict | None:
        """Status of a link for the API; 'expired' is derived for open links past expires_at."""
        with self._lock:
            row = self._db.execute(
                "SELECT link_id, state, form, container_id, created_at, expires_at, used_at"
                " FROM links WHERE link_id = ?",
                (link_id,),
            ).fetchone()
        if not row:
            return None
        info = dict(zip(_INFO_KEYS, row, strict=True))
        if info["status"] == "open" and info["expires_at"] <= time.time():
            info["status"] = "expired"
        return info

    def revoke(self, link_id: str) -> dict | None:
        """Revoke an open link. Used, claimed or already revoked links keep their state."""
        with self._lock:
            self._db.execute("UPDATE links SET state = 'revoked' WHERE link_id = ? AND state = 'open'", (link_id,))
            self._db.commit()
        return self.info(link_id)

    def peek(self, token: str) -> Link | None:
        """Return the link if it is still open and not expired."""
        with self._lock:
            row = self._db.execute(
                "SELECT container_id, form, expires_at FROM links"
                " WHERE token_hash = ? AND state = 'open' AND expires_at > ?",
                (_hash(token), time.time()),
            ).fetchone()
        return Link(*row) if row else None

    def claim(self, token: str) -> Link | None:
        """Atomically move an open link to 'claimed' so it cannot be submitted twice."""
        with self._lock:
            row = self._db.execute(
                "SELECT container_id, form, expires_at FROM links"
                " WHERE token_hash = ? AND state = 'open' AND expires_at > ?",
                (_hash(token), time.time()),
            ).fetchone()
            if not row:
                return None
            self._db.execute("UPDATE links SET state = 'claimed' WHERE token_hash = ?", (_hash(token),))
            self._db.commit()
        return Link(*row)

    def finish(self, token: str, success: bool) -> None:
        """Mark a claimed link used, or reopen it if the SOAR hand-off failed."""
        with self._lock:
            self._db.execute(
                "UPDATE links SET state = ?, used_at = ? WHERE token_hash = ? AND state = 'claimed'",
                ("used" if success else "open", time.time() if success else None, _hash(token)),
            )
            self._db.commit()

    def close(self) -> None:
        self._db.close()
