#!/usr/bin/env python3
"""Per-mind SQLite storage.

Everything for one mind lives under data/minds/{username}/:
mind.db (config, notes, this chat), memory/topics/, tool-calls/, uploads/.
Each store has its own lock so minds do not block each other.
"""

import os
import re
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager

from clock import utcnow_iso


def safe_filename(name: str) -> str:
    base = os.path.basename(name or "") or "file"
    base = re.sub(r"[^\w.\- ]+", "_", base, flags=re.UNICODE).strip(" .") or "file"
    return base[:120]


class MindStore:
    def __init__(self, data_root: str, username: str):
        self.username = username
        self.dir = os.path.join(data_root, username)
        os.makedirs(self.dir, exist_ok=True)
        self.db_path = os.path.join(self.dir, "mind.db")
        self.tool_calls_dir = os.path.join(self.dir, "tool-calls")
        os.makedirs(self.tool_calls_dir, exist_ok=True)
        self.uploads_dir = os.path.join(self.dir, "uploads")
        os.makedirs(self.uploads_dir, exist_ok=True)
        self.memory_dir = os.path.join(self.dir, "memory")
        self.topics_dir = os.path.join(self.memory_dir, "topics")
        os.makedirs(self.topics_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def _connect(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mind_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    note TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY,
                    message_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    bytes INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_attachments_message
                    ON attachments(message_id);
                """
            )

    # ----- config / state -----
    def get_config(self, key: str, default=None):
        return self._get("config", key, default)

    def set_config(self, key: str, value):
        self._set("config", key, value)

    def get_state(self, key: str, default=None):
        return self._get("mind_state", key, default)

    def set_state(self, key: str, value):
        self._set("mind_state", key, value)

    def _get(self, table: str, key: str, default=None):
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT value FROM {table} WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return row["value"]

    def _set(self, table: str, key: str, value):
        raw = json.dumps(value)
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {table} (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, raw),
            )

    def bump(self, key: str, n: int = 1) -> int:
        cur = int(self.get_state(key) or 0) + int(n)
        self.set_state(key, cur)
        return cur

    def add_uptime(self, seconds: float) -> float:
        try:
            extra = float(seconds)
        except (TypeError, ValueError):
            extra = 0.0
        if extra < 0:
            extra = 0.0
        total = float(self.get_state("uptime_seconds") or 0) + extra
        total = round(total, 3)
        self.set_state("uptime_seconds", total)
        return total

    # ----- observations -----
    def add_observation(self, note: str, max_keep: int = 50):
        note = (note or "").strip()
        if not note:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO observations (ts, note) VALUES (?, ?)",
                (utcnow_iso(), note),
            )
            conn.execute(
                """
                DELETE FROM observations WHERE id NOT IN (
                    SELECT id FROM observations ORDER BY id DESC LIMIT ?
                )
                """,
                (max_keep,),
            )

    def recent_observations(self, limit: int = 15) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, note FROM observations ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"ts": r["ts"], "note": r["note"]} for r in reversed(rows)]

    # ----- activity (what the loop has been doing) -----
    def add_activity(self, kind: str, detail: str, max_keep: int = 40):
        detail = (detail or "").strip()
        kind = (kind or "").strip() or "note"
        if not detail:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO activity (ts, kind, detail) VALUES (?, ?, ?)",
                (utcnow_iso(), kind, detail[:500]),
            )
            conn.execute(
                """
                DELETE FROM activity WHERE id NOT IN (
                    SELECT id FROM activity ORDER BY id DESC LIMIT ?
                )
                """,
                (max_keep,),
            )

    def recent_activity(self, limit: int = 12) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, kind, detail FROM activity ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"ts": r["ts"], "kind": r["kind"], "detail": r["detail"]} for r in rows]

    # ----- durable memory (markdown topics) -----
    def write_topic(self, title: str, body: str) -> dict | None:
        title = (title or "").strip()
        body = (body or "").strip()
        slug = _topic_slug(title)
        if not slug or not title:
            return None
        low = body.lower()
        if not body or low in ("none", "n/a", "nothing significant"):
            return None
        text = _topic_file_text(title, body)
        path = os.path.join(self.topics_dir, slug + ".md")
        with self._lock:
            os.makedirs(self.topics_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text if text.endswith("\n") else text + "\n")
            self._write_memory_index()
        return {"title": title, "slug": slug}

    def read_topic(self, title: str) -> dict | None:
        slug = _topic_slug(title)
        if not slug:
            return None
        with self._lock:
            hit = self._topic_from_slug(slug)
            if hit:
                return hit
            want = (title or "").strip().lower()
            for item in self._list_topics_unlocked():
                if item["title"].lower() == want:
                    return item
        return None

    def delete_topic(self, title: str) -> dict | None:
        found = self.read_topic(title)
        if not found:
            return None
        path = os.path.join(self.topics_dir, found["slug"] + ".md")
        with self._lock:
            if os.path.isfile(path):
                os.remove(path)
            self._write_memory_index()
        return {"title": found["title"], "slug": found["slug"]}

    def list_topics(self) -> list[dict]:
        with self._lock:
            return self._list_topics_unlocked()

    def get_memory_index(self) -> str:
        topics = self.list_topics()
        if not topics:
            return ""
        lines = ["Topics:"]
        for t in topics:
            summary = t["summary"]
            line = f"- **{t['title']}**"
            if summary:
                line += f" — {summary}"
            lines.append(line)
        lines.append("Titles only. read_memory opens a page. remember with the same title replaces it.")
        return "\n".join(lines)

    def _topic_from_slug(self, slug: str) -> dict | None:
        path = os.path.join(self.topics_dir, slug + ".md")
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as f:
            text = f.read()
        title, summary = _parse_topic(text, slug)
        return {"title": title, "slug": slug, "summary": summary, "body": text}

    def _list_topics_unlocked(self) -> list[dict]:
        if not os.path.isdir(self.topics_dir):
            return []
        out = []
        for name in os.listdir(self.topics_dir):
            if not name.endswith(".md") or name.startswith("."):
                continue
            slug = name[:-3]
            item = self._topic_from_slug(slug)
            if item:
                out.append(item)
        out.sort(key=lambda t: t["title"].lower())
        return out

    def _write_memory_index(self) -> None:
        os.makedirs(self.memory_dir, exist_ok=True)
        topics = self._list_topics_unlocked()
        lines = [
            "# Memory",
            "",
            "> Generated. Use remember / forget; do not edit this file.",
            "",
        ]
        if topics:
            lines.append("## Topics")
            lines.append("")
            for t in topics:
                summary = t["summary"]
                line = f"- **{t['title']}**"
                if summary:
                    line += f" — {summary}"
                line += f" (`topics/{t['slug']}.md`)"
                lines.append(line)
            lines.append("")
        else:
            lines.append("No topics yet.")
            lines.append("")
        path = os.path.join(self.memory_dir, "MEMORY.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    # ----- chat -----
    def save_upload(self, data: bytes, name: str, *, kind: str, mime: str) -> dict:
        fid = secrets.token_hex(8)
        safe = safe_filename(name)
        path = os.path.abspath(os.path.join(self.uploads_dir, f"{fid}_{safe}"))
        with open(path, "wb") as f:
            f.write(data)
        return {
            "id": fid,
            "name": safe,
            "mime": mime,
            "kind": kind,
            "path": path,
            "bytes": len(data),
        }

    def add_message(self, author: str, text: str, attachments: list | None = None) -> dict:
        author = (author or "").strip()
        if author not in ("human", "mind"):
            raise ValueError("author must be human or mind")
        text = text or ""
        attachments = list(attachments or [])
        if not text.strip() and not attachments:
            raise ValueError("empty message")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages (author, text, created_at) VALUES (?, ?, ?)",
                (author, text, utcnow_iso()),
            )
            mid = int(cur.lastrowid)
            for att in attachments:
                conn.execute(
                    """
                    INSERT INTO attachments
                        (id, message_id, name, mime, kind, path, bytes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        att["id"],
                        mid,
                        att.get("name") or "file",
                        att.get("mime") or "application/octet-stream",
                        att.get("kind") or "file",
                        att.get("path") or "",
                        int(att.get("bytes") or 0),
                    ),
                )
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
            return self._hydrate(conn, [row])[0]

    def clear_messages(self) -> int:
        with self._connect() as conn:
            paths = [
                r["path"]
                for r in conn.execute("SELECT path FROM attachments").fetchall()
            ]
            count = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
            conn.execute("DELETE FROM attachments")
            conn.execute("DELETE FROM messages")
        for path in paths:
            self._remove_upload(path)
        self.set_state("last_seen_message_id", 0)
        return int(count)

    def _remove_upload(self, path: str) -> None:
        if not path:
            return
        real = os.path.realpath(path)
        root = os.path.realpath(self.uploads_dir)
        if real != root and not real.startswith(root + os.sep):
            return
        try:
            os.remove(real)
        except OSError:
            pass

    def message_count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"])

    def latest_message_id(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(id) AS n FROM messages").fetchone()
        return int(row["n"] or 0)

    def oldest_message_id(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT MIN(id) AS n FROM messages").fetchone()
        return int(row["n"] or 0)

    def count_human_after(self, after_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE id > ? AND author = 'human'",
                (int(after_id or 0),),
            ).fetchone()
        return int(row["n"])

    def recent_messages(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit or 50), 200))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            rows = list(reversed(rows))
            return self._hydrate(conn, rows)

    def messages_after(self, after_id: int, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit or 100), 200))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE id > ? ORDER BY id ASC LIMIT ?",
                (int(after_id or 0), limit),
            ).fetchall()
            return self._hydrate(conn, rows)

    def unseen_human(self, after_id: int, limit: int = 80) -> list[dict]:
        """Newest human messages after id, returned oldest-first."""
        limit = max(1, min(int(limit or 80), 200))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE id > ? AND author = 'human'
                ORDER BY id DESC LIMIT ?
                """,
                (int(after_id or 0), limit),
            ).fetchall()
            rows = list(reversed(rows))
            return self._hydrate(conn, rows)

    def messages_before(self, before_id: int | None, limit: int = 40) -> list[dict]:
        limit = max(1, min(int(limit or 40), 200))
        with self._connect() as conn:
            if before_id:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE id < ? ORDER BY id DESC LIMIT ?",
                    (int(before_id), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            rows = list(reversed(rows))
            return self._hydrate(conn, rows)

    def search_messages(self, query: str, limit: int = 20) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return []
        limit = max(1, min(int(limit or 20), 50))
        needle = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE text LIKE ? ESCAPE '\\'
                ORDER BY id DESC LIMIT ?
                """,
                (needle, limit),
            ).fetchall()
            rows = list(reversed(rows))
            return self._hydrate(conn, rows)

    def get_attachment(self, fid: str) -> dict | None:
        fid = (fid or "").strip()
        if not fid:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (fid,)
            ).fetchone()
        if not row:
            return None
        return self._public_attachment(row)

    def latest_preview(self) -> dict | None:
        msgs = self.recent_messages(1)
        if not msgs:
            return None
        m = msgs[-1]
        text = " ".join((m.get("text") or "").split())
        if not text:
            atts = m.get("attachments") or []
            if atts:
                kind = "Image" if atts[0].get("kind") == "image" else "File"
                text = f"{kind}: {atts[0].get('name') or ''}".strip()
            else:
                text = ""
        return {
            "text": text[:160],
            "at": m.get("created_at"),
            "author": m.get("author"),
            "id": m.get("id"),
        }

    def _hydrate(self, conn, rows) -> list[dict]:
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        atts = conn.execute(
            f"SELECT * FROM attachments WHERE message_id IN ({marks}) ORDER BY rowid",
            ids,
        ).fetchall()
        by_msg: dict[int, list] = {}
        for a in atts:
            by_msg.setdefault(a["message_id"], []).append(self._public_attachment(a))
        return [self._public_message(r, by_msg.get(r["id"], [])) for r in rows]

    def _public_attachment(self, row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "mime": row["mime"],
            "kind": row["kind"],
            "path": row["path"],
            "bytes": int(row["bytes"] or 0),
            "url": f"/api/minds/{self.username}/files/{row['id']}",
        }

    def _public_message(self, row, attachments: list) -> dict:
        return {
            "id": int(row["id"]),
            "author": row["author"],
            "text": row["text"] or "",
            "created_at": row["created_at"],
            "attachments": attachments,
        }


def _topic_slug(title: str) -> str:
    s = (title or "").strip().lower()
    s = re.sub(r"[^\w]+", "-", s, flags=re.UNICODE).strip("-")
    s = s.replace("_", "-")
    return s[:80]


def _topic_file_text(title: str, body: str) -> str:
    body = (body or "").strip()
    heading = f"# {title}"
    if body.startswith("# "):
        first = body.split("\n", 1)[0][2:].strip()
        if first.lower() == title.lower():
            return body if body.endswith("\n") else body + "\n"
    if body:
        return f"{heading}\n\n{body}\n"
    return heading + "\n"


def _parse_topic(text: str, slug: str) -> tuple[str, str]:
    lines = (text or "").splitlines()
    title = slug.replace("-", " ")
    i = 0
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip() or title
        i = 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    summary = ""
    if i < len(lines):
        summary = lines[i].strip()
        if summary.startswith("#"):
            summary = ""
    if len(summary) > 160:
        summary = summary[:157].rstrip() + "…"
    return title, summary


def list_usernames(data_root: str) -> list[str]:
    if not os.path.isdir(data_root):
        return []
    names = []
    for name in sorted(os.listdir(data_root)):
        path = os.path.join(data_root, name)
        if ".deleting-" in name:
            continue
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, "mind.db")):
            names.append(name)
    return names
