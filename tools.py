"""manyhands — the six tools. Every handler returns a JSON string and never raises."""

from __future__ import annotations

import json
from typing import Any

from . import identity, policy, store


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra}, ensure_ascii=False)


def _ok(**payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _caller(
    rt, session_id: str, turn_id: str = "", task_id: str = ""
) -> dict[str, Any]:
    """Who is calling this tool.

    Tool dispatch carries no sender identity: ``tools/registry.py`` passes ``task_id`` + ``session_id``
    (never ``turn_id``), so this resolves turn map -> task map -> session -> fail closed. The session
    fallback is only used when one person has ever spoken there; in a shared session an unattributable
    call comes back as an unidentified guest rather than borrowing the last speaker's identity.
    """
    person, source = rt.person_for_session(session_id or "", turn_id or "", task_id or "")
    if person is None:
        return {
            "person_id": None,
            "role": policy.GUEST,
            "tools": [],
            "display": (
                "a shared session with more than one speaker"
                if source == "ambiguous"
                else "unknown session"
            ),
        }
    return person


def _is_owner(person: dict[str, Any]) -> bool:
    return str(person.get("role") or "") == "owner"


# --------------------------------------------------------------------------- whoami


def whoami(rt, args: dict[str, Any], session_id: str = "", turn_id: str = "", task_id: str = "") -> str:
    try:
        person = _caller(rt, session_id, turn_id, task_id)
        role = policy.normalize_role(person.get("role"))
        card = policy.tool_card(role, person.get("tools"))
        pid = person.get("person_id")
        facts = store.count_facts(rt.conn, pid) if pid and pid != identity.OPERATOR else 0
        return _ok(
            person=identity.label(person),
            person_id=pid,
            role=role,
            platform=person.get("platform"),
            allowed=card["allow"],
            needs_owner_approval=card["ask"],
            tool_count="all" if "*" in (card["allow"] or []) else len(card["allow"] or []),
            facts=facts,
            paused=rt.paused,
            enforcement=(
                "paused by the owner — nothing is being enforced"
                if rt.paused
                else "every tool call is checked against this role"
            ),
        )
    except Exception as exc:
        return _err(f"whoami failed: {exc.__class__.__name__}: {exc}")


# --------------------------------------------------------------------------- people


def people(rt, args: dict[str, Any], session_id: str = "", turn_id: str = "", task_id: str = "") -> str:
    """The roster, read-only.

    Roster *changes* are deliberately not a tool. A tool runs on the model's say-so inside whatever
    turn it happens to be — an owner's turn, a cron turn, a machine turn — and nothing in Hermes asks
    the human before it lands, so a prompt injection in an owner's turn could hand an outsider the
    owner role and every tool that comes with it. That would undo the one thing this plugin promises:
    permissions change when a **human types a command**. The human path is `/many add`, `/many role`,
    `/many tools`, `/many budget` and `/many remove` (`__init__._cmd`), and it is the only path.
    """
    try:
        conn = rt.conn
        caller = _caller(rt, session_id, turn_id, task_id)
        viewer_is_owner = _is_owner(caller)
        action = str(args.get("action") or "list").strip().lower()

        if action not in {"", "list"}:
            return _err(
                "manyhands_people is read-only: a roster change has to be typed by a human, not "
                "requested by the model. Ask the owner to run /many add <platform> <sender_id> "
                "[role], /many role <name> <role>, /many tools <name> … , /many budget <name> "
                "<tokens> or /many remove <name>.",
                you=identity.label(caller),
                role=policy.normalize_role(caller.get("role")),
            )

        rows = store.list_people(conn)
        return _ok(
            count=len(rows),
            people=[
                {
                    # Only the owner sees the platform ids: a roster is a list of people, not a
                    # directory of accounts for whoever happens to be talking to the bot.
                    "person_id": row["person_id"] if viewer_is_owner else None,
                    "name": row.get("name") or row.get("display") or row.get("sender_id"),
                    "platform": row.get("platform") if viewer_is_owner else None,
                    "role": policy.normalize_role(row.get("role")),
                    "summary": policy.describe_role(row.get("role") or "", row.get("tools")),
                    "facts": store.count_facts(conn, row["person_id"]),
                    # Another person's spend is theirs; the owner sees everyone's.
                    "daily_tokens": (
                        int(row.get("daily_tokens") or 0) if viewer_is_owner else None
                    ),
                    "you": row["person_id"] == caller.get("person_id"),
                }
                for row in rows
            ],
            open_invites=len(store.open_invites(conn)) if viewer_is_owner else None,
            note=None
            if viewer_is_owner
            else "platform ids and budgets are only shown to the owner; roles and tool counts are "
            "public inside this chat. Use /many role|tools|budget|remove to change the roster.",
        )
    except Exception as exc:
        return _err(f"people failed: {exc.__class__.__name__}: {exc}")


# --------------------------------------------------------------------------- memory


def remember(rt, args: dict[str, Any], session_id: str = "", turn_id: str = "", task_id: str = "") -> str:
    try:
        person = _caller(rt, session_id, turn_id, task_id)
        pid = person.get("person_id")
        text = str(args.get("text") or "").strip()
        if not text:
            return _err("text is required")
        if not pid or pid == identity.OPERATOR:
            return _err("there is no person attached to this conversation, so there is nobody to remember this for")
        if policy.normalize_role(person.get("role")) == policy.BLOCKED:
            return _err("that person is blocked and cannot store notes")
        topic = str(args.get("topic") or "").strip() or None
        store.add_fact(rt.conn, pid, text, topic=topic, source="tool")
        rt.audit(kind="memory", person_id=pid, note=f"remember ({topic or 'no topic'})")
        return _ok(saved=True, for_person=identity.label(person), topic=topic, text=text[:200],
                   total=store.count_facts(rt.conn, pid))
    except Exception as exc:
        return _err(f"remember failed: {exc.__class__.__name__}: {exc}")


def recall(rt, args: dict[str, Any], session_id: str = "", turn_id: str = "", task_id: str = "") -> str:
    try:
        person = _caller(rt, session_id, turn_id, task_id)
        pid = person.get("person_id")
        if not pid or pid == identity.OPERATOR:
            return _ok(results=[], note="no person attached to this conversation")
        query = str(args.get("query") or "").strip()
        limit = max(1, min(int(args.get("limit") or 5), 20))
        hits = store.search_facts(rt.conn, pid, query, limit=limit) if query else store.list_facts(
            rt.conn, pid, limit=limit
        )
        shared = store.search_shared(rt.conn, query, limit=3) if query else []
        return _ok(
            person=identity.label(person),
            namespace=pid,
            query=query or None,
            results=[
                {"text": hit["text"], "topic": hit.get("topic"), "created_at": hit.get("created_at")}
                for hit in hits
            ],
            shared=[{"text": hit["text"]} for hit in shared],
            note=(
                None
                if hits or shared
                else f"nothing stored for {identity.label(person)} matches that — do not guess"
            ),
        )
    except Exception as exc:
        return _err(f"recall failed: {exc.__class__.__name__}: {exc}")


def share(rt, args: dict[str, Any], session_id: str = "", turn_id: str = "", task_id: str = "") -> str:
    try:
        person = _caller(rt, session_id, turn_id, task_id)
        role = policy.normalize_role(person.get("role"))
        if role not in (policy.OWNER, policy.MEMBER):
            return _err(f"a {role} cannot publish shared facts — ask the owner")
        text = str(args.get("text") or "").strip()
        if not text:
            return _err("text is required")
        store.add_shared(rt.conn, text, added_by=identity.label(person))
        rt.audit(kind="memory", person_id=person.get("person_id"), note="shared fact added")
        return _ok(shared=True, text=text[:200], added_by=identity.label(person),
                   visible_to=len(store.list_people(rt.conn)))
    except Exception as exc:
        return _err(f"share failed: {exc.__class__.__name__}: {exc}")


# --------------------------------------------------------------------------- audit


def audit(rt, args: dict[str, Any], session_id: str = "", turn_id: str = "", task_id: str = "") -> str:
    try:
        caller = _caller(rt, session_id, turn_id, task_id)
        target = str(args.get("person") or "").strip()
        hours = float(args.get("hours") or 24)
        limit = max(1, min(int(args.get("limit") or 25), 100))

        if target and not _is_owner(caller):
            # Reading somebody else's trail is an owner action; your own is always yours.
            if target.lower() not in {
                str(caller.get("person_id") or "").lower(),
                identity.label(caller).lower(),
            }:
                return _err("only the owner can read another person's audit trail")

        person_id = None
        if target:
            matches = store.find_by_name(rt.conn, target)
            if not matches:
                return _err(f"no person matches {target!r}")
            person_id = matches[0]["person_id"]
        privileged = _is_owner(caller) or str(caller.get("person_id") or "") == identity.OPERATOR
        if not target and not privileged:
            # No target means "everyone", and across a household that is everybody's tool names, token
            # counts and refusal notes. A member reads their own trail; only the owner reads the house.
            if not caller.get("person_id"):
                return _err(
                    "no person is attached to this conversation, so there is no trail of yours to read",
                    you=identity.label(caller),
                )
            person_id = caller["person_id"]

        pending = store.pending_approvals(rt.conn)
        if not privileged:
            # A code plus who asked for what tool is the owner's queue, not a household noticeboard.
            pending = [
                row for row in pending if row.get("person_id") == caller.get("person_id")
            ]

        summary = store.audit_summary(rt.conn, person_id=person_id, hours=hours)
        rows = store.audit_rows(rt.conn, person_id=person_id, hours=hours, limit=limit)
        return _ok(
            subject=target or ("everyone" if privileged else "you"),
            hours=hours,
            summary=summary,
            recent=[
                {
                    "ts": row.get("ts"),
                    "kind": row.get("kind"),
                    "who": row.get("person_id"),
                    "tool": row.get("tool"),
                    "allowed": None if row.get("allowed") is None else bool(row["allowed"]),
                    "needs_approval": None
                    if row.get("needs_approval") is None
                    else bool(row["needs_approval"]),
                    "approved": None if row.get("approved") is None else bool(row["approved"]),
                    "tokens": row.get("tokens"),
                    "note": row.get("note"),
                }
                for row in rows
            ],
            pending_approvals=[
                {"code": row["code"], "who": row.get("person_id"), "tool": row.get("tool")}
                for row in pending
            ],
        )
    except Exception as exc:
        return _err(f"audit failed: {exc.__class__.__name__}: {exc}")


# --------------------------------------------------------------------------- schemas

WHOAMI_SCHEMA = {
    "name": "manyhands_whoami",
    "description": (
        "Who am I talking to, and what are they allowed to do? Returns the caller's roster name, "
        "their role, the exact tools that role carries and how many private facts are stored for "
        "them. Use it before offering a capability, so you never promise a tool the person does "
        "not have."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

PEOPLE_SCHEMA = {
    "name": "manyhands_people",
    "description": (
        "The roster of people this Hermes serves: each person's name, role (owner, member, guest, "
        "blocked), what that role may use, and how many facts they have stored. Read-only — a "
        "roster change is a human step (`/many add`, `/many role`, `/many tools`, `/many budget`, "
        "`/many remove` in the owner's own chat), so do not offer to make one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list"],
                "description": "list (the default and the only action).",
            },
        },
        "required": [],
    },
}

REMEMBER_SCHEMA = {
    "name": "manyhands_remember",
    "description": (
        "Store a fact in the CALLER's own memory namespace. It is visible to that person and to "
        "nobody else, so use it for anything personal: allergies, deadlines, preferences. Quote it "
        "back when asked."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The fact, in one sentence."},
            "topic": {"type": "string", "description": "Optional short label, e.g. 'health'."},
        },
        "required": ["text"],
    },
}

RECALL_SCHEMA = {
    "name": "manyhands_recall",
    "description": (
        "Search the caller's own stored facts (plus facts the owner shared with everybody). An "
        "empty result means there is nothing to answer from — say that plainly instead of guessing, "
        "and never use another person's facts to fill the gap."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for; omit to list the most recent facts."},
            "limit": {"type": "integer", "description": "Max results (default 5, max 20)."},
        },
        "required": [],
    },
}

SHARE_SCHEMA = {
    "name": "manyhands_share",
    "description": (
        "Publish a fact to everyone this Hermes serves (owner or member only). Use it for things "
        "the whole household or team should know — the wifi password is not one of them."
    ),
    "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "The shared fact."}},
        "required": ["text"],
    },
}

AUDIT_SCHEMA = {
    "name": "manyhands_audit",
    "description": (
        "Who asked for what: turns, tool calls, refusals, approvals and token usage over the last N "
        "hours, optionally for one person. Shows the refusals and the escalations that a normal "
        "chat transcript hides."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "person": {"type": "string", "description": "Roster name or person_id; omit for everyone."},
            "hours": {"type": "number", "description": "Window in hours (default 24)."},
            "limit": {"type": "integer", "description": "Max audit rows (default 25, max 100)."},
        },
        "required": [],
    },
}
