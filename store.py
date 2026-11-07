"""manyhands — the store.

One SQLite file, one connection per plugin instance, no ORM and no third-party imports. Every
function takes the connection first so the whole schema can be driven from a temp file in the
tests, with no Hermes install and no network.

Tables (see README §Data model):

  people        one row per (platform, sender_id) — the roster, and the only source of identity
  facts         per-person memory, keyed by person_id. Never merged, never global
  shared_facts  facts the owner or a member chose to publish to everyone
  audit         turns, tool decisions, refusals, approvals, token usage
  sessions      session_id -> person_id, written on every turn so a restart cannot lose identity
  invites       one-shot invite codes (`invite` policy)
  approvals     escalated tool calls waiting on a human decision
  settings      plugin settings that must survive a restart (pause state, owner pin)
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

#: No 0/O, 1/I/L: these tokens get read off a phone screen and typed back.
TOKEN_ALPHABET = "ACDEFGHJKMNPQRSTUVWXYZ23456789"


def new_token(length: int = 6) -> str:
    rng = random.SystemRandom()
    return "".join(rng.choice(TOKEN_ALPHABET) for _ in range(int(length)))

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
  person_id    TEXT PRIMARY KEY,
  platform     TEXT NOT NULL,
  sender_id    TEXT NOT NULL,
  display      TEXT,
  name         TEXT,
  role         TEXT NOT NULL DEFAULT 'guest',
  tools_json   TEXT NOT NULL DEFAULT '[]',
  daily_tokens INTEGER NOT NULL DEFAULT 0,
  created_at   REAL,
  last_seen    REAL,
  UNIQUE(platform, sender_id)
);

CREATE TABLE IF NOT EXISTS facts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id  TEXT NOT NULL,
  topic      TEXT,
  text       TEXT NOT NULL,
  created_at REAL,
  source     TEXT
);
CREATE INDEX IF NOT EXISTS facts_person ON facts(person_id);

CREATE TABLE IF NOT EXISTS shared_facts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  text       TEXT NOT NULL,
  added_by   TEXT,
  created_at REAL
);

CREATE TABLE IF NOT EXISTS audit (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             REAL,
  person_id      TEXT,
  session_id     TEXT,
  turn_id        TEXT,
  kind           TEXT,          -- turn | tool | refusal | approval | tokens | policy
  tool           TEXT,
  allowed        INTEGER,
  needs_approval INTEGER,
  approved       INTEGER,
  tokens         INTEGER,
  note           TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
CREATE INDEX IF NOT EXISTS audit_person ON audit(person_id, ts);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  person_id  TEXT NOT NULL,
  platform   TEXT,
  updated_at REAL
);

CREATE TABLE IF NOT EXISTS invites (
  code       TEXT PRIMARY KEY,
  role       TEXT NOT NULL DEFAULT 'member',
  created_at REAL,
  used_by    TEXT,
  used_at    REAL
);

CREATE TABLE IF NOT EXISTS approvals (
  code       TEXT PRIMARY KEY,
  created_at REAL,
  person_id  TEXT,
  tool       TEXT,
  summary    TEXT,
  request_id TEXT,
  digest     TEXT,
  status     TEXT NOT NULL DEFAULT 'pending',   -- escalated | pending | approved | denied | expired
  decided_by TEXT,
  decided_at REAL,
  choice     TEXT,
  expires_at REAL
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

#: Bump when a column is added; ``migrate()`` applies ``COLUMN_ADDITIONS`` idempotently.
SCHEMA_VERSION = 3

#: How long an approved-but-unspent grant stays live, so the asker has time to retry the tool.
GRANT_TTL_S = 900.0

#: ``(table, column, ddl)`` — every upgrade this plugin has ever needed, applied once each.
COLUMN_ADDITIONS: tuple[tuple[str, str, str], ...] = (
    ("approvals", "choice", "TEXT"),
    ("approvals", "summary", "TEXT"),
    ("audit", "turn_id", "TEXT"),
    # A pending approval the owner grants becomes a one-shot grant; ``consumed_at`` is when the
    # asker spent it. NULL means the grant is still unspent.
    ("approvals", "consumed_at", "REAL"),
)


# --------------------------------------------------------------------------- connection


def migrate(conn: sqlite3.Connection) -> int:
    """Create or upgrade the schema, and return the version now in place.

    ``CREATE TABLE IF NOT EXISTS`` alone is not a migration: it never adds a column to a database
    that already exists, so an install from an earlier version breaks with "no such column" the
    first time a new field is written. Each upgrade is an idempotent column add, which is all a
    SQLite plugin needs and is reviewable at a glance.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    if version < 1:
        conn.executescript(SCHEMA)
    for table, column, ddl in COLUMN_ADDITIONS:
        if column not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    if version != SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return SCHEMA_VERSION


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (and initialise) the plugin database.

    ``check_same_thread=False`` because the hooks and the tool handlers run on different threads,
    and WAL so a reader (audit, roster) never blocks the writer (a turn).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:  # a filesystem that cannot do WAL still works in the default mode
        pass
    migrate(conn)
    conn.commit()
    return conn


def now() -> float:
    return time.time()


def person_id_for(platform: str, sender_id: str) -> str:
    return f"{str(platform or 'unknown').strip().lower()}:{str(sender_id).strip()}"


def _row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if "tools_json" in data:
        try:
            data["tools"] = json.loads(data.get("tools_json") or "[]")
        except (TypeError, ValueError):
            data["tools"] = []
    return data


# --------------------------------------------------------------------------- people


def get_person(conn: sqlite3.Connection, person_id: str) -> dict[str, Any] | None:
    return _row(conn.execute("SELECT * FROM people WHERE person_id = ?", (person_id,)).fetchone())


def get_person_by_sender(conn: sqlite3.Connection, platform: str, sender_id: str) -> dict[str, Any] | None:
    return _row(
        conn.execute(
            "SELECT * FROM people WHERE platform = ? AND sender_id = ?",
            (str(platform or "").strip().lower(), str(sender_id).strip()),
        ).fetchone()
    )


def add_person(
    conn: sqlite3.Connection,
    *,
    platform: str,
    sender_id: str,
    role: str = "guest",
    display: str | None = None,
    name: str | None = None,
    tools: Iterable[str] | None = None,
    daily_tokens: int = 0,
) -> dict[str, Any]:
    pid = person_id_for(platform, sender_id)
    ts = now()
    conn.execute(
        """INSERT INTO people (person_id, platform, sender_id, display, name, role, tools_json,
                               daily_tokens, created_at, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(person_id) DO UPDATE SET
             role = excluded.role,
             display = COALESCE(excluded.display, people.display),
             name = COALESCE(excluded.name, people.name),
             tools_json = excluded.tools_json,
             daily_tokens = excluded.daily_tokens,
             last_seen = excluded.last_seen""",
        (
            pid,
            str(platform or "").strip().lower(),
            str(sender_id).strip(),
            display,
            name,
            role,
            json.dumps(list(tools or []), ensure_ascii=False),
            int(daily_tokens or 0),
            ts,
            ts,
        ),
    )
    conn.commit()
    return get_person(conn, pid) or {}


def touch_person(conn: sqlite3.Connection, person_id: str, display: str | None = None) -> None:
    conn.execute(
        "UPDATE people SET last_seen = ?, display = COALESCE(?, display) WHERE person_id = ?",
        (now(), display, person_id),
    )
    conn.commit()


def set_person_name(conn: sqlite3.Connection, person_id: str, name: str) -> bool:
    """Record the platform display name the first time we see it (used in prompts and the roster)."""
    cur = conn.execute(
        "UPDATE people SET name = ? WHERE person_id = ? AND (name IS NULL OR name = '')",
        (str(name), str(person_id)),
    )
    conn.commit()
    return cur.rowcount == 1


def set_role(conn: sqlite3.Connection, person_id: str, role: str) -> bool:
    cur = conn.execute("UPDATE people SET role = ? WHERE person_id = ?", (role, person_id))
    conn.commit()
    return cur.rowcount > 0


def set_person_tools(conn: sqlite3.Connection, person_id: str, tools: Iterable[str] | None) -> bool:
    cur = conn.execute(
        "UPDATE people SET tools_json = ? WHERE person_id = ?",
        (json.dumps(list(tools or []), ensure_ascii=False), person_id),
    )
    conn.commit()
    return cur.rowcount > 0


def rename_person(conn: sqlite3.Connection, person_id: str, name: str) -> bool:
    cur = conn.execute("UPDATE people SET name = ? WHERE person_id = ?", (name, person_id))
    conn.commit()
    return cur.rowcount > 0


def remove_person(conn: sqlite3.Connection, person_id: str) -> bool:
    cur = conn.execute("DELETE FROM people WHERE person_id = ?", (person_id,))
    conn.execute("DELETE FROM facts WHERE person_id = ?", (person_id,))
    conn.execute("DELETE FROM sessions WHERE person_id = ?", (person_id,))
    conn.commit()
    return cur.rowcount > 0


def list_people(conn: sqlite3.Connection, role: str | None = None) -> list[dict[str, Any]]:
    if role:
        rows = conn.execute(
            "SELECT * FROM people WHERE role = ? ORDER BY role, name, person_id", (role,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM people ORDER BY "
            "CASE role WHEN 'owner' THEN 0 WHEN 'member' THEN 1 WHEN 'guest' THEN 2 ELSE 3 END, "
            "COALESCE(name, display, person_id)"
        ).fetchall()
    return [_row(r) or {} for r in rows]


def owners(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return list_people(conn, role="owner")


def find_by_name(conn: sqlite3.Connection, needle: str) -> list[dict[str, Any]]:
    """Resolve a human-typed name ("zara", "telegram:123") to roster rows, best match first."""
    needle = (needle or "").strip().lower().lstrip("@")
    if not needle:
        return []
    exact = [
        p
        for p in list_people(conn)
        if needle in {(p.get("name") or "").lower(), (p.get("person_id") or "").lower()}
    ]
    if exact:
        return exact
    return [
        p
        for p in list_people(conn)
        if needle in (p.get("name") or "").lower() or needle in (p.get("person_id") or "").lower()
    ]


# --------------------------------------------------------------------------- facts


def add_fact(
    conn: sqlite3.Connection, person_id: str, text: str, topic: str | None = None, source: str = "tool"
) -> int:
    cur = conn.execute(
        "INSERT INTO facts (person_id, topic, text, created_at, source) VALUES (?, ?, ?, ?, ?)",
        (person_id, topic, text.strip(), now(), source),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_facts(conn: sqlite3.Connection, person_id: str, limit: int = 8) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM facts WHERE person_id = ? ORDER BY id DESC LIMIT ?", (person_id, int(limit))
    ).fetchall()
    return [dict(r) for r in rows]


def count_facts(conn: sqlite3.Connection, person_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM facts WHERE person_id = ?", (person_id,)).fetchone()
    return int(row["n"] if row else 0)


def search_facts(
    conn: sqlite3.Connection, person_id: str, query: str, limit: int = 5
) -> list[dict[str, Any]]:
    """Keyword search inside ONE person's namespace.

    Deliberately not FTS5: the schema stays readable, the ranking is deterministic and reviewable
    (matches first, then recency), and a person's rows are the only rows this query can see.
    """
    words = [w for w in _words(query) if len(w) > 1][:8]
    rows = conn.execute(
        "SELECT * FROM facts WHERE person_id = ? ORDER BY id DESC", (person_id,)
    ).fetchall()
    scored: list[tuple[int, float, dict[str, Any]]] = []
    for row in rows:
        data = dict(row)
        haystack = f"{data.get('topic') or ''} {data.get('text') or ''}".lower()
        score = sum(haystack.count(w) for w in words)
        if words and score == 0:
            continue
        scored.append((score, float(data.get("created_at") or 0.0), data))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored[: int(limit)]]


def _words(text: str) -> list[str]:
    out: list[str] = []
    current: list[str] = []
    for ch in str(text or "").lower():
        if ch.isalnum():
            current.append(ch)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def add_shared(conn: sqlite3.Connection, text: str, added_by: str) -> int:
    cur = conn.execute(
        "INSERT INTO shared_facts (text, added_by, created_at) VALUES (?, ?, ?)",
        (text.strip(), added_by, now()),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_shared(conn: sqlite3.Connection, limit: int = 5) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM shared_facts ORDER BY id DESC LIMIT ?", (int(limit),)
    ).fetchall()
    return [dict(r) for r in rows]


def search_shared(conn: sqlite3.Connection, query: str, limit: int = 5) -> list[dict[str, Any]]:
    words = [w for w in _words(query) if len(w) > 1][:8]
    rows = conn.execute("SELECT * FROM shared_facts ORDER BY id DESC").fetchall()
    hits = []
    for row in rows:
        data = dict(row)
        haystack = str(data.get("text") or "").lower()
        if not words or any(w in haystack for w in words):
            hits.append(data)
    return hits[: int(limit)]


# --------------------------------------------------------------------------- sessions


def remember_session(
    conn: sqlite3.Connection, session_id: str, person_id: str, platform: str | None = None
) -> None:
    if not session_id:
        return
    conn.execute(
        """INSERT INTO sessions (session_id, person_id, platform, updated_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
             person_id = excluded.person_id, platform = excluded.platform,
             updated_at = excluded.updated_at""",
        (session_id, person_id, platform, now()),
    )
    conn.commit()


def person_for_session(conn: sqlite3.Connection, session_id: str) -> str | None:
    if not session_id:
        return None
    row = conn.execute(
        "SELECT person_id FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return str(row["person_id"]) if row else None


# --------------------------------------------------------------------------- invites


def mint_invite(conn: sqlite3.Connection, code: str, role: str = "member") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO invites (code, role, created_at, used_by, used_at) VALUES (?, ?, ?, NULL, NULL)",
        (code.upper(), role, now()),
    )
    conn.commit()


def redeem_invite(conn: sqlite3.Connection, code: str, person_id: str) -> dict[str, Any] | None:
    """Consume a code exactly once. Returns the invite row, or None if it was already used."""
    row = conn.execute(
        "SELECT * FROM invites WHERE code = ? AND used_by IS NULL", (str(code).strip().upper(),)
    ).fetchone()
    if row is None:
        return None
    cur = conn.execute(
        "UPDATE invites SET used_by = ?, used_at = ? WHERE code = ? AND used_by IS NULL",
        (person_id, now(), str(code).strip().upper()),
    )
    conn.commit()
    if cur.rowcount != 1:  # lost a race with another redemption
        return None
    return dict(row)


def open_invites(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM invites WHERE used_by IS NULL ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- approvals


def add_escalation(
    conn: sqlite3.Connection, *, tool: str, person_id: str | None, summary: str
) -> str:
    """Park a `pre_tool_call` escalation so the transport can name the asker.

    The hook knows who is asking; the approval transport only receives the host's redacted
    request. This row is the join between them: the transport binds its request_id/digest onto the
    newest unbound escalation for the same tool and turns it into a real prompt.
    """
    code = f"esc-{uuid.uuid4().hex[:10]}"
    conn.execute(
        """INSERT INTO approvals
           (code, created_at, person_id, tool, summary, request_id, digest, status, decided_by,
            decided_at, expires_at)
           VALUES (?, ?, ?, ?, ?, '', '', 'escalated', NULL, NULL, ?)""",
        (code, now(), person_id, tool, summary, now() + 300.0),
    )
    conn.commit()
    return code


def match_escalation(conn: sqlite3.Connection, tool: str, window_s: float = 180.0) -> dict[str, Any] | None:
    """The newest unclaimed escalation for *tool* inside *window_s* seconds."""
    row = conn.execute(
        """SELECT * FROM approvals
           WHERE status = 'escalated' AND tool = ? AND created_at >= ?
           ORDER BY created_at DESC LIMIT 1""",
        (str(tool), now() - float(window_s)),
    ).fetchone()
    return dict(row) if row else None


def recent_asker(
    conn: sqlite3.Connection, tool: str, window_s: float = 300.0
) -> dict[str, Any] | None:
    """The newest row naming who asked for *tool*, whatever its status.

    The dangerous-command gate fires *after* a plugin approval is spent, so by then the escalation
    row is already ``approved``/``used``. Falling back to the newest row for the same tool keeps the
    owner's prompt naming the person who asked instead of "the machine".
    """
    row = conn.execute(
        """SELECT * FROM approvals
           WHERE tool = ? AND person_id IS NOT NULL AND created_at >= ?
           ORDER BY created_at DESC LIMIT 1""",
        (str(tool), now() - float(window_s)),
    ).fetchone()
    return dict(row) if row else None


def bind_approval(
    conn: sqlite3.Connection,
    *,
    from_code: str,
    code: str,
    request_id: str,
    digest: str,
    expires_at: float,
    tool: str,
    person_id: str | None,
    summary: str,
) -> None:
    """Turn a parked escalation into a live prompt under a short, human-typable code."""
    conn.execute("DELETE FROM approvals WHERE code = ?", (str(from_code),))
    conn.execute(
        """INSERT OR REPLACE INTO approvals
           (code, created_at, person_id, tool, summary, request_id, digest, status, decided_by,
            decided_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?)""",
        (code.upper(), now(), person_id, tool, summary, request_id, digest, expires_at),
    )
    conn.commit()


def open_approval(
    conn: sqlite3.Connection,
    *,
    code: str,
    person_id: str,
    tool: str,
    summary: str,
    request_id: str = "",
    digest: str = "",
    expires_at: float | None = None,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO approvals
           (code, created_at, person_id, tool, summary, request_id, digest, status, decided_by,
            decided_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?)""",
        (code.upper(), now(), person_id, tool, summary, request_id, digest, expires_at),
    )
    conn.commit()


def decision_for(conn: sqlite3.Connection, code: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM approvals WHERE code = ?", (code.upper(),)).fetchone()
    return dict(row) if row else None


def decide_approval(
    conn: sqlite3.Connection, code: str, approved: bool, decided_by: str, choice: str = ""
) -> bool:
    # An approval is not a prompt any more: it becomes a one-shot grant with its own window, so the
    # asker can retry after the owner answers without the row expiring in the meantime.
    cur = conn.execute(
        """UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, choice = ?,
                               expires_at = CASE WHEN ? = 'approved' THEN ? ELSE expires_at END
           WHERE code = ? AND status = 'pending' AND expires_at > ?""",
        (
            "approved" if approved else "denied",
            decided_by,
            now(),
            choice,
            "approved" if approved else "denied",
            now() + GRANT_TTL_S,
            code.upper(),
            now(),
        ),
    )
    conn.commit()
    return cur.rowcount == 1


def expire_approval(conn: sqlite3.Connection, code: str) -> None:
    conn.execute(
        "UPDATE approvals SET status = 'expired', decided_at = ? WHERE code = ? AND status = 'pending'",
        (now(), code.upper()),
    )
    conn.commit()


def pending_approvals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Codes still waiting on the owner — expired ones are not pending, even before anyone sweeps them."""
    rows = conn.execute(
        """SELECT * FROM approvals
           WHERE status = 'pending' AND expires_at > ?
           ORDER BY created_at DESC""",
        (now(),),
    ).fetchall()
    return [dict(r) for r in rows]


def supersede_pending(conn: sqlite3.Connection, person_id: str | None, tool: str) -> int:
    """Close any older open ask for the same person and tool, so exactly one code is ever live."""
    cur = conn.execute(
        """UPDATE approvals SET status = 'expired', decided_at = ?
           WHERE status = 'pending' AND tool = ? AND IFNULL(person_id, '') = IFNULL(?, '')""",
        (now(), str(tool), person_id),
    )
    conn.commit()
    return cur.rowcount


def live_grant(
    conn: sqlite3.Connection, person_id: str | None, tool: str
) -> dict[str, Any] | None:
    """The newest approval *person* got for *tool* that is approved, unspent, and not expired.

    The host's action gate never consults plugin transports, so the owner's "yes" is stored as a
    one-shot grant and spent by the asker's next attempt at the same tool. A grant is bound to the
    person and the tool: approving one person's `terminal` never unlocks somebody else's.
    """
    if not person_id:
        return None
    row = conn.execute(
        """SELECT * FROM approvals
           WHERE person_id = ? AND tool = ? AND status = 'approved'
             AND consumed_at IS NULL AND expires_at > ?
           ORDER BY created_at DESC LIMIT 1""",
        (str(person_id), str(tool), now()),
    ).fetchone()
    return dict(row) if row else None


def consume_grant(conn: sqlite3.Connection, code: str) -> bool:
    """Spend a grant, so the same approval can never run a tool twice."""
    cur = conn.execute(
        "UPDATE approvals SET consumed_at = ? WHERE code = ? AND consumed_at IS NULL",
        (now(), str(code).upper()),
    )
    conn.commit()
    return cur.rowcount == 1


# --------------------------------------------------------------------------- audit


def audit(
    conn: sqlite3.Connection,
    *,
    kind: str,
    person_id: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    tool: str | None = None,
    allowed: bool | None = None,
    needs_approval: bool | None = None,
    approved: bool | None = None,
    tokens: int | None = None,
    note: str | None = None,
) -> None:
    def _b(value: bool | None) -> int | None:
        return None if value is None else (1 if value else 0)

    conn.execute(
        """INSERT INTO audit (ts, person_id, session_id, turn_id, kind, tool, allowed,
                              needs_approval, approved, tokens, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            now(),
            person_id,
            session_id,
            turn_id,
            kind,
            tool,
            _b(allowed),
            _b(needs_approval),
            _b(approved),
            int(tokens) if tokens is not None else None,
            note,
        ),
    )
    conn.commit()


def audit_rows(
    conn: sqlite3.Connection,
    *,
    person_id: str | None = None,
    hours: float = 24.0,
    limit: int = 50,
) -> list[dict[str, Any]]:
    since = now() - max(float(hours), 0.0) * 3600.0
    if person_id:
        rows = conn.execute(
            "SELECT * FROM audit WHERE ts >= ? AND person_id = ? ORDER BY id DESC LIMIT ?",
            (since, person_id, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit WHERE ts >= ? ORDER BY id DESC LIMIT ?", (since, int(limit))
        ).fetchall()
    return [dict(r) for r in rows]


def audit_summary(
    conn: sqlite3.Connection, *, person_id: str | None = None, hours: float = 24.0
) -> dict[str, Any]:
    since = now() - max(float(hours), 0.0) * 3600.0
    where = "ts >= ?" + (" AND person_id = ?" if person_id else "")
    params: list[Any] = [since] + ([person_id] if person_id else [])
    row = conn.execute(
        f"""SELECT COUNT(*) FILTER (WHERE kind = 'turn')  AS turns,
                   COUNT(*) FILTER (WHERE kind = 'refusal') AS refusals,
                   COUNT(*) FILTER (WHERE kind = 'approval') AS approvals,
                   COUNT(*) FILTER (WHERE kind = 'tool')  AS tool_calls,
                   COALESCE(SUM(tokens), 0) AS tokens
            FROM audit WHERE {where}""",
        params,
    ).fetchone()
    tools = conn.execute(
        f"""SELECT tool, COUNT(*) AS n FROM audit
            WHERE {where} AND kind = 'tool' AND tool IS NOT NULL
            GROUP BY tool ORDER BY n DESC LIMIT 8""",
        params,
    ).fetchall()
    return {
        "turns": int(row["turns"] or 0) if row else 0,
        "refusals": int(row["refusals"] or 0) if row else 0,
        "approvals": int(row["approvals"] or 0) if row else 0,
        "tool_calls": int(row["tool_calls"] or 0) if row else 0,
        "tokens": int(row["tokens"] or 0) if row else 0,
        "top_tools": [{"tool": r["tool"], "count": int(r["n"])} for r in tools],
        "hours": float(hours),
    }


def tokens_spent_since(conn: sqlite3.Connection, person_id: str, since: float) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(tokens), 0) AS n FROM audit WHERE person_id = ? AND ts >= ? AND tokens IS NOT NULL",
        (person_id, since),
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def tokens_today(conn: sqlite3.Connection, person_id: str) -> int:
    start = time.time() - (time.time() % 86400)  # UTC day boundary, documented in the README
    return tokens_spent_since(conn, person_id, start)


# --------------------------------------------------------------------------- settings


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for label, sql in (
        ("people", "SELECT COUNT(*) AS n FROM people"),
        ("facts", "SELECT COUNT(*) AS n FROM facts"),
        ("shared", "SELECT COUNT(*) AS n FROM shared_facts"),
        ("audit", "SELECT COUNT(*) AS n FROM audit"),
    ):
        row = conn.execute(sql).fetchone()
        out[label] = int(row["n"] or 0) if row else 0
    return out
