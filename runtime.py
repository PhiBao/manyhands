"""manyhands — runtime: the one object the hooks, tools, commands and transport share.

Owns the SQLite connection, the settings, and the in-process ``session_id -> person_id`` map that
carries identity from ``pre_llm_call`` (which receives the sender) to ``pre_tool_call`` (which does
not — see README §Why the map exists).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from . import identity, store

DEFAULTS: dict[str, Any] = {
    # Unknown senders: "invite" (fail closed until a code is redeemed), "open" (guest), "closed".
    "unknown_sender_policy": "invite",
    "invite_role": "member",
    # How many of this person's own facts are injected per turn, and how many shared ones.
    "max_person_facts": 8,
    "max_shared_facts": 5,
    # Owner DM approval: how long to wait for a human decision before failing closed.
    "approval_timeout_s": 120,
    # Enforcement can be switched off at runtime with /many pause; the audit row records it.
    "paused": False,
    # Whether a session that never carried a human turn may use the tools it asks for.
    # "guest" fails closed for restricted tools; "operator" trusts the host's own callers.
    "unknown_session_policy": "guest",
}


class Runtime:
    """Thread-safe handle on the plugin database + settings."""

    def __init__(self, ctx: Any = None, *, db_path: str | Path | None = None) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._conn = None
        self._hot: dict[str, str] = {}
        self._override_path = Path(db_path) if db_path else None
        key = ""
        for attr in ("plugin_id", "name"):
            try:
                key = str(getattr(ctx, attr, "") or getattr(getattr(ctx, "manifest", None), attr, "") or "")
            except Exception:
                key = ""
            if key:
                break
        self.key = (key or "manyhands").strip().lower().replace(" ", "-")

    # ------------------------------------------------------------------ settings

    def setting(self, key: str, default: Any = None) -> Any:
        fallback = DEFAULTS.get(key, default)
        value = None
        if self.ctx is not None:
            try:
                value = self.ctx.get_config(key, fallback)
            except Exception:
                value = None
        if value is None:
            value = fallback
        if key == "paused":
            # The pause flag lives in the database so /many pause survives a reload and a restart.
            try:
                return store.get_setting(self.conn, "paused", "1" if value else "0") == "1"
            except Exception:
                return bool(value)
        return value

    def set_paused(self, paused: bool) -> None:
        store.set_setting(self.conn, "paused", "1" if paused else "0")

    # ------------------------------------------------------------------ storage

    @property
    def db_path(self) -> Path:
        if self._override_path is not None:
            return self._override_path
        env = os.environ.get("MANYHANDS_DB")
        if env:
            return Path(env)
        try:
            from plugins.plugin_storage import plugin_data_dir

            return plugin_data_dir(self.key) / "manyhands.db"
        except Exception:
            home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
            root = home / "plugin-data" / self.key
            root.mkdir(parents=True, exist_ok=True)
            return root / "manyhands.db"

    @property
    def conn(self):
        with self._lock:
            if self._conn is None:
                conn = store.connect(self.db_path)
                # Seed + commit immediately: an open implicit transaction here pins SQLite's write
                # lock for the life of this connection and every later writer dies with
                # "database is locked" (this cost an hour in the sibling plugin).
                store.set_setting(conn, "db_path", str(self.db_path))
                conn.commit()
                self._conn = conn
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ------------------------------------------------------------------ session map

    def remember(self, session_id: str, person_id: str, platform: str | None = None) -> None:
        if not session_id:
            return
        self._hot[session_id] = person_id
        try:
            store.remember_session(self.conn, session_id, person_id, platform)
        except Exception:
            pass  # the hot map already answers for this process

    def forget(self, session_id: str) -> None:
        self._hot.pop(session_id, None)

    def person_for_session(self, session_id: str) -> tuple[dict[str, Any] | None, str]:
        return identity.person_for_tool_call(self.conn, session_id=session_id, hot_map=self._hot)

    # ------------------------------------------------------------------ outbound

    def dm(self, person: dict[str, Any], text: str) -> bool:
        """Send *person* a direct message through the host gateway.

        On Telegram a private chat's id IS the user id, so an origin built from ``sender_id``
        reaches that person's DM. Other platforms need their own DM-channel resolution; the call
        returns False there instead of pretending, and the caller fails closed.
        """
        if self.ctx is None or not person:
            return False
        platform = str(person.get("platform") or "").strip().lower()
        sender_id = str(person.get("sender_id") or "").strip()
        if not platform or not sender_id or platform == "host":
            return False
        origin = {
            "platform": platform,
            "chat_id": sender_id,
            "chat_type": "dm",
            "user_id": sender_id,
        }
        try:
            return bool(
                self.ctx.inject_message(str(text), role="assistant", origin=origin)
            )
        except Exception:
            return False

    def dm_owner(self, text: str) -> tuple[bool, dict[str, Any] | None]:
        owner = identity.pinned_owner(self.conn)
        if not owner:
            return False, None
        return self.dm(owner, text), owner

    # ------------------------------------------------------------------ misc

    def audit(self, **kwargs: Any) -> None:
        try:
            store.audit(self.conn, **kwargs)
        except Exception:
            pass  # auditing must never break a turn

    @property
    def paused(self) -> bool:
        return bool(self.setting("paused", False))

    def approval_tools(self) -> list[str]:
        raw = self.setting("approval_tools", None)
        if isinstance(raw, str) and raw.strip():
            return [t.strip() for t in raw.split(",") if t.strip()]
        if isinstance(raw, (list, tuple)):
            return [str(t) for t in raw]
        from .policy import DEFAULT_APPROVAL_TOOLS

        return list(DEFAULT_APPROVAL_TOOLS)
