from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MENTION_RE = re.compile(r"(?<![A-Za-z0-9._-])@([A-Za-z0-9][A-Za-z0-9._-]{0,63})")
AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STATUS_ONLINE = "online"
STATUS_AWAY = "away"
STATUS_BUSY = "busy"
STATUS_OFFLINE = "offline"
ALLOWED_PRESENCE = {STATUS_ONLINE, STATUS_AWAY, STATUS_BUSY, STATUS_OFFLINE}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def parse_iso8601(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid timestamp: {value}") from exc


def encode_cursor(message_id: int | None) -> str | None:
    if message_id is None:
        return None
    payload = json.dumps({"after": int(message_id)}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_cursor(cursor: str | None) -> int | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Invalid cursor.") from exc
    value = payload.get("after")
    if not isinstance(value, int) or value < 0:
        raise ValueError("Invalid cursor.")
    return value


def validate_agent_name(agent_name: str) -> str:
    value = agent_name.strip()
    if not AGENT_NAME_RE.fullmatch(value):
        raise ValueError(
            "agent_name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}."
        )
    return value


def canonicalize_project_key(raw_path: str) -> str:
    expanded = os.path.expanduser(raw_path.strip())
    if not expanded:
        raise ValueError("project_key is required.")
    base = Path(expanded)
    if not base.is_absolute():
        base = (Path.cwd() / base).resolve(strict=False)
    else:
        base = base.resolve(strict=False)
    base_str = os.path.realpath(str(base))
    git_cmd = [
        "git",
        "-C",
        base_str,
        "rev-parse",
        "--show-toplevel",
    ]
    try:
        result = subprocess.run(
            git_cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        result = None
    if result and result.returncode == 0:
        root = result.stdout.strip()
        if root:
            return os.path.realpath(root)
    return base_str


def project_slug(project_key: str) -> str:
    name = Path(project_key).name or "project"
    stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
    digest = hashlib.sha256(project_key.encode("utf-8")).hexdigest()[:10]
    return f"{stem}-{digest}"


@dataclass(frozen=True)
class Project:
    id: int
    project_key: str
    project_slug: str
    created_at: str


@dataclass(frozen=True)
class Agent:
    id: int
    project_id: int
    agent_name: str
    program: str
    model: str
    registration_token: str
    session_id: str | None
    started_at: str
    last_seen_at: str
    status: str


class Store:
    def __init__(self, db_path: Path, stale_after_seconds: int = 1800) -> None:
        self.db_path = db_path
        self.stale_after_seconds = stale_after_seconds
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_key TEXT NOT NULL UNIQUE,
                    project_slug TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    agent_name TEXT NOT NULL,
                    program TEXT NOT NULL,
                    model TEXT NOT NULL,
                    registration_token TEXT NOT NULL,
                    session_id TEXT,
                    started_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    UNIQUE(project_id, agent_name COLLATE NOCASE)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    sender_agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    scope TEXT NOT NULL CHECK(scope IN ('project', 'dm')),
                    thread_id TEXT,
                    body_md TEXT NOT NULL,
                    ack_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS message_recipients (
                    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    read_at TEXT,
                    ack_at TEXT,
                    PRIMARY KEY (message_id, agent_id)
                );

                CREATE TABLE IF NOT EXISTS mention_deliveries (
                    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    read_at TEXT,
                    PRIMARY KEY (message_id, agent_id)
                );
                """
            )

    def ensure_project(self, raw_project_key: str) -> Project:
        key = canonicalize_project_key(raw_project_key)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, project_key, project_slug, created_at FROM projects WHERE project_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                created_at = utc_now()
                slug = project_slug(key)
                conn.execute(
                    "INSERT INTO projects(project_key, project_slug, created_at) VALUES (?, ?, ?)",
                    (key, slug, created_at),
                )
                row = conn.execute(
                    "SELECT id, project_key, project_slug, created_at FROM projects WHERE project_key = ?",
                    (key,),
                ).fetchone()
            assert row is not None
            return self._project_from_row(row)

    def _project_from_row(self, row: sqlite3.Row) -> Project:
        return Project(
            id=int(row["id"]),
            project_key=str(row["project_key"]),
            project_slug=str(row["project_slug"]),
            created_at=str(row["created_at"]),
        )

    def find_project(self, raw_project_key: str) -> Project | None:
        key = canonicalize_project_key(raw_project_key)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, project_key, project_slug, created_at FROM projects WHERE project_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return self._project_from_row(row)

    def require_project(self, raw_project_key: str) -> Project:
        project = self.find_project(raw_project_key)
        if project is None:
            raise ValueError(f"Unknown project: {canonicalize_project_key(raw_project_key)}")
        return project

    def get_project_by_slug(self, slug: str) -> Project:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, project_key, project_slug, created_at FROM projects WHERE project_slug = ?",
                (slug,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown project slug: {slug}")
        return self._project_from_row(row)

    def get_agent(self, project_id: int, agent_name: str) -> Agent | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, project_id, agent_name, program, model, registration_token, session_id,
                       started_at, last_seen_at, status
                FROM agents
                WHERE project_id = ? AND agent_name = ? COLLATE NOCASE
                """,
                (project_id, agent_name),
            ).fetchone()
        if row is None:
            return None
        return Agent(
            id=int(row["id"]),
            project_id=int(row["project_id"]),
            agent_name=str(row["agent_name"]),
            program=str(row["program"]),
            model=str(row["model"]),
            registration_token=str(row["registration_token"]),
            session_id=str(row["session_id"]) if row["session_id"] else None,
            started_at=str(row["started_at"]),
            last_seen_at=str(row["last_seen_at"]),
            status=str(row["status"]),
        )

    def get_agent_by_id(self, agent_id: int) -> Agent:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, project_id, agent_name, program, model, registration_token, session_id,
                       started_at, last_seen_at, status
                FROM agents
                WHERE id = ?
                """,
                (agent_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown agent id: {agent_id}")
        return Agent(
            id=int(row["id"]),
            project_id=int(row["project_id"]),
            agent_name=str(row["agent_name"]),
            program=str(row["program"]),
            model=str(row["model"]),
            registration_token=str(row["registration_token"]),
            session_id=str(row["session_id"]) if row["session_id"] else None,
            started_at=str(row["started_at"]),
            last_seen_at=str(row["last_seen_at"]),
            status=str(row["status"]),
        )

    def register_agent(
        self,
        project: Project,
        agent_name: str,
        program: str,
        model: str,
        session_id: str | None,
        registration_token: str | None = None,
    ) -> Agent:
        name = validate_agent_name(agent_name)
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT id, registration_token
                FROM agents
                WHERE project_id = ? AND agent_name = ? COLLATE NOCASE
                """,
                (project.id, name),
            ).fetchone()
            if existing is not None:
                stored = str(existing["registration_token"])
                if not registration_token or not hmac.compare_digest(stored, registration_token):
                    raise ValueError(
                        f"Active agent_name '{name}' already exists for this project. "
                        "Pass the existing registration_token to refresh it."
                    )
                token = stored
                now = utc_now()
                conn.execute(
                    """
                    UPDATE agents
                    SET program = ?, model = ?, session_id = ?, last_seen_at = ?, status = ?
                    WHERE id = ?
                    """,
                    (program, model, session_id, now, STATUS_ONLINE, int(existing["id"])),
                )
                return self.get_agent_by_id(int(existing["id"]))

            token = registration_token or secrets.token_urlsafe(24)
            now = utc_now()
            conn.execute(
                """
                INSERT INTO agents(
                    project_id, agent_name, program, model, registration_token, session_id,
                    started_at, last_seen_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (project.id, name, program, model, token, session_id, now, now, STATUS_ONLINE),
            )
            agent = self.get_agent(project.id, name)
            assert agent is not None
            return agent

    def touch_agent(self, agent_id: int, session_id: str | None = None, status: str = STATUS_ONLINE) -> None:
        now = utc_now()
        with self._connect() as conn:
            if session_id is None:
                conn.execute(
                    "UPDATE agents SET last_seen_at = ?, status = ? WHERE id = ?",
                    (now, status, agent_id),
                )
            else:
                conn.execute(
                    "UPDATE agents SET last_seen_at = ?, session_id = ?, status = ? WHERE id = ?",
                    (now, session_id, status, agent_id),
                )

    def set_presence(self, project: Project, agent_name: str, status: str) -> Agent:
        if status not in ALLOWED_PRESENCE:
            raise ValueError(f"status must be one of: {', '.join(sorted(ALLOWED_PRESENCE))}")
        agent = self.get_agent(project.id, agent_name)
        if agent is None:
            raise ValueError(f"Unknown agent: {agent_name}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE agents SET status = ?, last_seen_at = ? WHERE id = ?",
                (status, utc_now(), agent.id),
            )
        return self.get_agent_by_id(agent.id)

    def list_active_agents(self, project: Project) -> list[dict[str, Any]]:
        cutoff = datetime.now(UTC).timestamp() - self.stale_after_seconds
        rows: list[dict[str, Any]] = []
        with self._connect() as conn:
            result = conn.execute(
                """
                SELECT agent_name, status, last_seen_at
                FROM agents
                WHERE project_id = ?
                ORDER BY agent_name COLLATE NOCASE ASC
                """,
                (project.id,),
            ).fetchall()
        for row in result:
            ts = datetime.fromisoformat(str(row["last_seen_at"]).replace("Z", "+00:00")).timestamp()
            if ts >= cutoff:
                rows.append(
                    {
                        "agent_name": str(row["agent_name"]),
                        "status": str(row["status"]),
                        "last_seen_at": str(row["last_seen_at"]),
                    }
                )
        return rows

    def _resolve_mentions(self, conn: sqlite3.Connection, project: Project, sender_agent_id: int, body_md: str) -> list[int]:
        mention_names = {match.group(1) for match in MENTION_RE.finditer(body_md)}
        if not mention_names:
            return []
        placeholders = ",".join(["?"] * len(mention_names))
        rows = conn.execute(
            f"""
            SELECT id, agent_name
            FROM agents
            WHERE project_id = ? AND lower(agent_name) IN ({placeholders})
            """,
            (project.id, *[name.lower() for name in sorted(mention_names)]),
        ).fetchall()
        recipients: list[int] = []
        for row in rows:
            agent_id = int(row["id"])
            if agent_id != sender_agent_id:
                recipients.append(agent_id)
        return recipients

    def send_project_message(
        self,
        project: Project,
        sender: Agent,
        body_md: str,
        thread_id: str | None = None,
    ) -> int:
        if not body_md.strip():
            raise ValueError("body_md is required.")
        created_at = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO messages(project_id, sender_agent_id, scope, thread_id, body_md, ack_requested, created_at)
                VALUES (?, ?, 'project', ?, ?, 0, ?)
                """,
                (project.id, sender.id, thread_id, body_md, created_at),
            )
            message_id = int(cursor.lastrowid)
            for agent_id in self._resolve_mentions(conn, project, sender.id, body_md):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO mention_deliveries(message_id, agent_id, read_at)
                    VALUES (?, ?, NULL)
                    """,
                    (message_id, agent_id),
                )
            return message_id

    def send_direct_message(
        self,
        project: Project,
        sender: Agent,
        recipients: list[str],
        body_md: str,
        thread_id: str | None = None,
        ack_requested: bool = False,
    ) -> int:
        if not body_md.strip():
            raise ValueError("body_md is required.")
        if not recipients:
            raise ValueError("At least one direct recipient is required.")
        seen: set[str] = set()
        recipient_names: list[str] = []
        for raw_name in recipients:
            name = validate_agent_name(raw_name)
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            recipient_names.append(name)
        with self._connect() as conn:
            placeholders = ",".join(["?"] * len(recipient_names))
            rows = conn.execute(
                f"""
                SELECT id, agent_name
                FROM agents
                WHERE project_id = ? AND lower(agent_name) IN ({placeholders})
                """,
                (project.id, *[name.lower() for name in recipient_names]),
            ).fetchall()
            found = {str(row["agent_name"]).lower(): int(row["id"]) for row in rows}
            missing = [name for name in recipient_names if name.lower() not in found]
            if missing:
                raise ValueError(f"Unknown direct message recipients: {', '.join(sorted(missing))}")
            created_at = utc_now()
            cursor = conn.execute(
                """
                INSERT INTO messages(project_id, sender_agent_id, scope, thread_id, body_md, ack_requested, created_at)
                VALUES (?, ?, 'dm', ?, ?, ?, ?)
                """,
                (project.id, sender.id, thread_id, body_md, 1 if ack_requested else 0, created_at),
            )
            message_id = int(cursor.lastrowid)
            for name in recipient_names:
                conn.execute(
                    """
                    INSERT INTO message_recipients(message_id, agent_id, read_at, ack_at)
                    VALUES (?, ?, NULL, NULL)
                    """,
                    (message_id, found[name.lower()]),
                )
            return message_id

    def fetch_project_feed(
        self,
        project: Project,
        cursor: str | None,
        since_ts: str | None,
        limit: int,
    ) -> dict[str, Any]:
        after_message_id = decode_cursor(cursor)
        since_iso = parse_iso8601(since_ts)
        clauses = ["m.project_id = ?", "m.scope = 'project'"]
        params: list[Any] = [project.id]
        if after_message_id is not None:
            clauses.append("m.id > ?")
            params.append(after_message_id)
        if since_iso is not None:
            clauses.append("m.created_at >= ?")
            params.append(since_iso)
        params.append(limit + 1)
        query = f"""
            SELECT m.id, m.thread_id, m.body_md, m.created_at, a.agent_name AS sender_name
            FROM messages m
            JOIN agents a ON a.id = m.sender_agent_id
            WHERE {' AND '.join(clauses)}
            ORDER BY m.id ASC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        more = len(rows) > limit
        visible_rows = rows[:limit]
        items = [
            {
                "message_id": int(row["id"]),
                "scope": "project",
                "thread_id": row["thread_id"],
                "sender_name": str(row["sender_name"]),
                "body_md": str(row["body_md"]),
                "created_at": str(row["created_at"]),
            }
            for row in visible_rows
        ]
        next_cursor = encode_cursor(int(visible_rows[-1]["id"])) if more and visible_rows else None
        return {"messages": items, "next_cursor": next_cursor}

    def fetch_inbox(
        self,
        project: Project,
        agent: Agent,
        cursor: str | None,
        since_ts: str | None,
        limit: int,
        unread_only: bool,
    ) -> dict[str, Any]:
        after_message_id = decode_cursor(cursor)
        since_iso = parse_iso8601(since_ts)
        clauses_dm = ["m.project_id = ?", "mr.agent_id = ?"]
        clauses_mention = ["m.project_id = ?", "md.agent_id = ?"]
        params_dm: list[Any] = [project.id, agent.id]
        params_mention: list[Any] = [project.id, agent.id]
        if after_message_id is not None:
            clauses_dm.append("m.id > ?")
            clauses_mention.append("m.id > ?")
            params_dm.append(after_message_id)
            params_mention.append(after_message_id)
        if since_iso is not None:
            clauses_dm.append("m.created_at >= ?")
            clauses_mention.append("m.created_at >= ?")
            params_dm.append(since_iso)
            params_mention.append(since_iso)
        if unread_only:
            clauses_dm.append("mr.read_at IS NULL")
            clauses_mention.append("md.read_at IS NULL")
        query = f"""
            SELECT m.id AS message_id,
                   'dm' AS entry_kind,
                   m.thread_id,
                   m.body_md,
                   m.created_at,
                   m.ack_requested,
                   a.agent_name AS sender_name,
                   mr.read_at,
                   mr.ack_at
            FROM message_recipients mr
            JOIN messages m ON m.id = mr.message_id
            JOIN agents a ON a.id = m.sender_agent_id
            WHERE {' AND '.join(clauses_dm)}
            UNION ALL
            SELECT m.id AS message_id,
                   'mention' AS entry_kind,
                   m.thread_id,
                   m.body_md,
                   m.created_at,
                   0 AS ack_requested,
                   a.agent_name AS sender_name,
                   md.read_at,
                   NULL AS ack_at
            FROM mention_deliveries md
            JOIN messages m ON m.id = md.message_id
            JOIN agents a ON a.id = m.sender_agent_id
            WHERE {' AND '.join(clauses_mention)}
            ORDER BY message_id ASC
            LIMIT ?
        """
        params = (*params_dm, *params_mention, limit + 1)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        more = len(rows) > limit
        visible_rows = rows[:limit]
        items = [
            {
                "message_id": int(row["message_id"]),
                "entry_kind": str(row["entry_kind"]),
                "thread_id": row["thread_id"],
                "sender_name": str(row["sender_name"]),
                "body_md": str(row["body_md"]),
                "created_at": str(row["created_at"]),
                "read_at": row["read_at"],
                "ack_requested": bool(row["ack_requested"]),
                "ack_at": row["ack_at"],
            }
            for row in visible_rows
        ]
        next_cursor = encode_cursor(int(visible_rows[-1]["message_id"])) if more and visible_rows else None
        return {"messages": items, "next_cursor": next_cursor}

    def mark_read(self, project: Project, agent: Agent, message_id: int) -> None:
        now = utc_now()
        with self._connect() as conn:
            updated_dm = conn.execute(
                """
                UPDATE message_recipients
                SET read_at = COALESCE(read_at, ?)
                WHERE message_id = ? AND agent_id = ?
                """,
                (now, message_id, agent.id),
            ).rowcount
            updated_mention = conn.execute(
                """
                UPDATE mention_deliveries
                SET read_at = COALESCE(read_at, ?)
                WHERE message_id = ? AND agent_id = ?
                """,
                (now, message_id, agent.id),
            ).rowcount
        if updated_dm == 0 and updated_mention == 0:
            raise ValueError(f"Message {message_id} is not visible in this agent inbox.")

    def ack(self, project: Project, agent: Agent, message_id: int) -> None:
        now = utc_now()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT m.ack_requested
                FROM message_recipients mr
                JOIN messages m ON m.id = mr.message_id
                WHERE mr.message_id = ? AND mr.agent_id = ?
                """,
                (message_id, agent.id),
            ).fetchone()
            if row is None:
                raise ValueError(f"Message {message_id} is not a direct-message delivery for this agent.")
            if int(row["ack_requested"]) != 1:
                raise ValueError(f"Message {message_id} was not sent with ack_requested = true.")
            conn.execute(
                """
                UPDATE message_recipients
                SET read_at = COALESCE(read_at, ?), ack_at = COALESCE(ack_at, ?)
                WHERE message_id = ? AND agent_id = ?
                """,
                (now, now, message_id, agent.id),
            )

    def unread_counts(self, project: Project, agent: Agent) -> dict[str, int]:
        with self._connect() as conn:
            dm = conn.execute(
                """
                SELECT COUNT(*)
                FROM message_recipients mr
                JOIN messages m ON m.id = mr.message_id
                WHERE mr.agent_id = ? AND m.project_id = ? AND mr.read_at IS NULL
                """,
                (agent.id, project.id),
            ).fetchone()
            mention = conn.execute(
                """
                SELECT COUNT(*)
                FROM mention_deliveries md
                JOIN messages m ON m.id = md.message_id
                WHERE md.agent_id = ? AND m.project_id = ? AND md.read_at IS NULL
                """,
                (agent.id, project.id),
            ).fetchone()
        direct_count = int(dm[0]) if dm else 0
        mention_count = int(mention[0]) if mention else 0
        return {
            "direct_unread": direct_count,
            "mention_unread": mention_count,
            "total_unread": direct_count + mention_count,
        }

    def visible_thread_messages(self, project: Project, agent: Agent, thread_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.id, m.scope, m.thread_id, m.body_md, m.created_at, a.agent_name AS sender_name
                FROM messages m
                JOIN agents a ON a.id = m.sender_agent_id
                LEFT JOIN message_recipients mr
                  ON mr.message_id = m.id AND mr.agent_id = ?
                WHERE m.project_id = ?
                  AND m.thread_id = ?
                  AND (
                    m.scope = 'project'
                    OR m.sender_agent_id = ?
                    OR mr.agent_id IS NOT NULL
                  )
                ORDER BY m.id ASC
                """,
                (agent.id, project.id, thread_id, agent.id),
            ).fetchall()
        return [
            {
                "message_id": int(row["id"]),
                "scope": str(row["scope"]),
                "thread_id": row["thread_id"],
                "sender_name": str(row["sender_name"]),
                "body_md": str(row["body_md"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def projects_for_agent_ids(self, agent_ids: set[int]) -> list[dict[str, Any]]:
        if not agent_ids:
            return []
        placeholders = ",".join(["?"] * len(agent_ids))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT p.id, p.project_key, p.project_slug, p.created_at
                FROM agents a
                JOIN projects p ON p.id = a.project_id
                WHERE a.id IN ({placeholders})
                ORDER BY p.project_slug ASC
                """,
                tuple(sorted(agent_ids)),
            ).fetchall()
        return [
            {
                "project_id": int(row["id"]),
                "project_key": str(row["project_key"]),
                "project_slug": str(row["project_slug"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def authenticate_agent(self, project: Project, agent_name: str, registration_token: str | None) -> Agent:
        agent = self.get_agent(project.id, agent_name)
        if agent is None:
            raise ValueError(f"Unknown agent: {agent_name}")
        if not registration_token:
            raise ValueError("registration_token is required for this call.")
        if not hmac.compare_digest(agent.registration_token, registration_token):
            raise ValueError("Invalid registration_token.")
        return agent
