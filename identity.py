"""manyhands — identity resolution.

``(platform, sender_id)`` -> a roster row. Identity comes from the platform event and from nothing
else: a message that says "I'm the owner" is just a message. The only two ways a person enters the
roster are (a) the unknown-sender policy for this bot and (b) a one-shot invite code.

Two kinds of turn never reach the policy at all:

* **machine-generated turns** (cron prompts, internal notifications, auto-continuations) are the
  operator talking to itself. They are recorded as the virtual ``operator`` person, which carries
  owner rights, because the alternative is a plugin that polices the owner's own scheduled jobs.
  A machine turn is recognised by its marker **and by having no sender**: the marker is a prefix on
  text that any human can type, so a message that starts with ``[cron]`` from somebody the platform
  names is that somebody's turn, resolved and policed like any other.
* **the operator's own terminal** — ``platform="cli"`` plus the local subject the gateway's control
  socket reports only for a peer with this process's own uid — is the same seat: see
  ``is_local_seat``.
* **a turn with no sender at all** (a platform that cannot name its user) is the operator's seat too.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from . import store

#: person_id of the virtual operator: the host itself, not a human in a chat.
OPERATOR = "operator"

OPERATOR_PERSON: dict[str, Any] = {
    "person_id": OPERATOR,
    "platform": "host",
    "sender_id": OPERATOR,
    "display": "operator",
    "name": "operator",
    "role": "owner",
    "tools": ["*"],
    "tools_json": '["*"]',
    "daily_tokens": 0,
    "virtual": True,
}

#: Prefixes the host puts on turns it generated itself. Matching is prefix-only and case-insensitive
#: on the FIRST characters of the message, so ordinary text that merely mentions them is unaffected.
MACHINE_MARKERS: tuple[str, ...] = (
    "[important:",
    "[internal notification",
    "[auto-continue",
    "[system notification",
    "[cron]",
    "[gateway notice",
)

#: The host also wraps mid-turn steering from the *same human* in this marker. It is deliberately
#: NOT treated as machine-generated: the sender identity still arrives with the payload, and
#: trusting an out-of-band message as the operator would let a guest's steer run with owner tools.
OOB_MARKER = "[out-of-band user message"

INVITE_PREFIXES = ("join ", "/join ", "many join ", "/many join ")


def is_machine_turn(user_message: Any) -> bool:
    text = str(user_message or "").lstrip().lower()
    return any(text.startswith(marker) for marker in MACHINE_MARKERS)


#: Platforms that are this machine's own terminal rather than a chat network.
LOCAL_PLATFORMS: tuple[str, ...] = ("cli", "local")


def local_seat_subject() -> str:
    """The sender id this machine's own terminal shows up as, or "" when it cannot be attested."""
    getuid = getattr(os, "getuid", None)  # windows-footgun: ok — absent on Windows, so no local seat
    if getuid is None:
        return ""
    try:
        return f"uid:{getuid()}"
    except Exception:
        return ""


def is_local_seat(platform: str | None, sender_id: str | None) -> bool:
    """Is this the operator's own terminal?

    A local turn is not sender-less in every Hermes version: the gateway's control socket attests the
    peer's uid and hands turns ``platform="cli"`` with ``uid:<uid>`` as the sender, and that subject
    is returned **only** when the peer's uid equals this process's own (``gateway/control_socket.py``
    ``_posix_peer_subject``). Same uid is the same trust boundary as "no sender at all", which is why
    this seat is the operator. Anything else on those platforms is a different account and goes
    through the roster like any other sender — the check is an exact match on the attested subject,
    never a prefix test on message text.
    """
    platform = str(platform or "").strip().lower()
    if platform not in LOCAL_PLATFORMS:
        return False
    subject = local_seat_subject()
    return bool(subject) and str(sender_id or "").strip() == subject


def invite_attempt(user_message: Any) -> str | None:
    """Extract a code from ``join ABC123`` / ``/many join ABC123``; None when it isn't an attempt."""
    text = str(user_message or "").strip()
    lowered = text.lower()
    for prefix in INVITE_PREFIXES:
        if lowered.startswith(prefix):
            code = text[len(prefix):].strip().strip("`'\"")
            return (code.split()[0] if code else "") or None
    return None


def refusal_for_policy(policy: str) -> str:
    if policy == "closed":
        return (
            "This Hermes is private and I can't take requests from this account. "
            "Nothing was run and nothing was saved."
        )
    # invite (the default) and anything unrecognised: fail closed, but tell the person what to do.
    return (
        "I don't run for people I can't place, so I haven't done anything yet. If the owner wants "
        "me to work with you, they'll give you an invite code — reply `join CODE` with it."
    )


def resolve(
    conn: sqlite3.Connection,
    *,
    platform: str | None,
    sender_id: str | None,
    display: str | None = None,
    policy: str = "invite",
    invite_role: str = "member",
    user_message: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve one sender to a person.

    Returns ``(person, None)`` for somebody on the roster, or ``(None, refusal)`` when the
    unknown-sender policy says no. A refusal is a return value, never an exception: the caller
    turns it into a blocked turn.
    """
    platform = str(platform or "").strip().lower()
    sender_id = str(sender_id or "").strip()

    if not sender_id:
        # No identity on the event at all (a local CLI turn, a platform that cannot name its user).
        return None, None

    known = store.get_person_by_sender(conn, platform, sender_id)
    if known:
        store.touch_person(conn, known["person_id"], display)
        return store.get_person(conn, known["person_id"]) or known, None

    policy = str(policy or "invite").strip().lower()
    code = invite_attempt(user_message)

    # A code is checked before the policy so that `closed` can still be joined deliberately.
    if code:
        person_id = store.person_id_for(platform, sender_id)
        redeemed = store.redeem_invite(conn, code, person_id)
        if redeemed:
            person = store.add_person(
                conn,
                platform=platform,
                sender_id=sender_id,
                role=redeemed.get("role") or invite_role,
                display=display,
            )
            return person, None
        return None, (
            "That invite code isn't valid, or somebody already used it. Every code works once — "
            "ask the owner for a fresh one."
        )

    if policy == "open":
        person = store.add_person(conn, platform=platform, sender_id=sender_id, role="guest", display=display)
        return person, None

    return None, refusal_for_policy(policy)


def person_for_tool_call(
    conn: sqlite3.Connection, *, session_id: str, hot_map: dict[str, str] | None = None
) -> tuple[dict[str, Any] | None, str]:
    """Who is this tool call being made for? Returns ``(person, source)``.

    ``source`` is diagnostic and lands in the audit row: ``hot`` (this process resolved the turn),
    ``db`` (a restart lost the in-process map; the persisted session row answered), or
    ``unknown`` (a session that never carried a human turn — a cold start, a subagent, a tool run
    from a socket). The caller fails CLOSED on ``unknown``: see ``hooks.pre_tool_call``.
    """
    if session_id and hot_map is not None:
        pid = hot_map.get(session_id)
        if pid:
            return _load(conn, pid), "hot"
    pid = store.person_for_session(conn, session_id)
    if pid:
        return _load(conn, pid), "db"
    return None, "unknown"


def _load(conn: sqlite3.Connection, person_id: str) -> dict[str, Any] | None:
    if person_id == OPERATOR:
        return dict(OPERATOR_PERSON)
    return store.get_person(conn, person_id)


def label(person: dict[str, Any] | None) -> str:
    """What to call somebody in copy, the audit trail and the roster."""
    if not person:
        return "somebody"
    return str(person.get("name") or person.get("display") or person.get("sender_id") or "somebody")


def is_owner(person: dict[str, Any] | None) -> bool:
    return bool(person) and str(person.get("role") or "") == "owner"


def has_owner(conn: sqlite3.Connection) -> bool:
    return bool(store.owners(conn))


def owner_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return store.owners(conn)


def pin_owner(conn: sqlite3.Connection, person_id: str) -> None:
    store.set_setting(conn, "owner_person_id", person_id)


def pinned_owner(conn: sqlite3.Connection) -> dict[str, Any] | None:
    pid = store.get_setting(conn, "owner_person_id")
    if pid:
        person = store.get_person(conn, pid)
        if person:
            return person
    rows = owner_rows(conn)
    return rows[0] if rows else None
