"""manyhands — the approval transport.

A tool the person may not use *yet* is escalated by ``pre_tool_call`` to the host's approval gate.
This module is the plugin's answer to "who sees that prompt": it sends it to the **owner's DM**, in
the owner's own chat, under a short code — not to whoever happens to be in the room where the tool
was asked for.

How a decision comes back
-------------------------
Only a human typing a slash command can authorize anything. ``/many approve CODE`` is handled by the
host's command dispatcher on the owner's own message, so no tool, no prompt and no model output can
approve its own escalation. When the code is approved the row flips in SQLite, this function notices
on its next poll (0.5 s) and the tool runs once. No answer before the deadline means **deny**.

Fail-closed rules (all tested):

* no owner on the roster            -> deny
* the DM could not be delivered     -> deny (never fall back to the chat that asked)
* the owner said deny               -> deny
* the clock ran out                 -> deny, and the row is marked expired
* the transport itself raised       -> deny (see ``make_transport``)
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

from . import identity, store

#: No 0/O, 1/I/L: these codes get read off a phone screen and typed back.
CODE_ALPHABET = "ACDEFGHJKMNPQRSTUVWXYZ23456789"

_RANDOM = random.SystemRandom()

POLL_INTERVAL_S = 0.5

logger = logging.getLogger("hermes_plugins.manyhands")


def new_code(conn, length: int = 4) -> str:
    """A short code that is unique among the approvals still open."""
    for _ in range(50):
        code = "".join(_RANDOM.choice(CODE_ALPHABET) for _ in range(length))
        if store.decision_for(conn, code) is None:
            return code
    return "".join(_RANDOM.choice(CODE_ALPHABET) for _ in range(length + 3))


def _choices(request: Any) -> tuple[str, ...]:
    allowed = tuple(getattr(request, "allowed_choices", ()) or ())
    return allowed or ("once", "deny")


def _coerce(choice: str, allowed: tuple[str, ...]) -> str:
    """Map the owner's answer onto a choice the host offered (never widen it)."""
    choice = str(choice or "").strip().lower()
    if choice in allowed:
        return choice
    if choice in {"always", "session"} and "once" in allowed:
        return "once"
    return "deny"


def prompt_text(request: Any, *, who: str, code: str, timeout_s: float) -> str:
    command = str(getattr(request, "command", "") or "").strip()
    description = str(getattr(request, "description", "") or "").strip()
    lines = [
        f"*Approval needed* — {who}",
        "",
        f"`{command[:280]}`" if command else "",
    ]
    if description and description != command:
        lines.append(description[:280])
    lines += [
        "",
        f"Reply `/many approve {code}` to allow it once, or `/many deny {code}` to refuse.",
        f"Nothing runs if nobody answers within {int(timeout_s)}s.",
    ]
    return "\n".join(line for line in lines if line is not None).strip()


def _present(rt, request: Any) -> Any:
    conn = rt.conn
    allowed = _choices(request)
    timeout_s = float(getattr(request, "timeout_seconds", 120) or 120)
    try:
        cap = float(rt.setting("approval_timeout_s", 120) or 0)
    except (TypeError, ValueError):
        cap = 0.0
    if cap > 0:
        timeout_s = min(timeout_s, cap)
    timeout_s = max(timeout_s, 5.0)

    tool = str(getattr(request, "pattern_key", "") or "command")
    escalation = store.match_escalation(conn, tool) or store.recent_asker(conn, tool)
    person_id = escalation.get("person_id") if escalation else None
    who = "the machine" if not person_id else identity.label(store.get_person(conn, person_id))
    summary = (escalation or {}).get("summary") or str(getattr(request, "description", "") or tool)

    owner = identity.pinned_owner(conn)
    if owner is None:
        rt.audit(
            kind="approval",
            person_id=person_id,
            tool=tool,
            needs_approval=True,
            approved=False,
            note="no owner on the roster — denied",
        )
        return request.respond("deny")

    code = new_code(conn)
    store.bind_approval(
        conn,
        from_code=(escalation or {}).get("code", ""),
        code=code,
        request_id=str(getattr(request, "request_id", "") or ""),
        digest=str(getattr(request, "digest", "") or ""),
        expires_at=time.time() + timeout_s,
        tool=tool,
        person_id=person_id,
        summary=summary,
    )

    text = prompt_text(request, who=who, code=code, timeout_s=timeout_s)
    logger.info(
        "manyhands: approval %s for %s -> prompting owner %s under code %s",
        tool, who, owner.get("person_id"), code,
    )
    delivered = rt.dm(owner, text)
    if not delivered:
        logger.warning("manyhands: owner DM for code %s could not be delivered", code)
    rt.audit(
        kind="approval",
        person_id=person_id,
        tool=tool,
        needs_approval=True,
        approved=None,
        note=f"code {code} -> owner DM ({'delivered' if delivered else 'DELIVERY FAILED'})",
    )
    if not delivered:
        store.expire_approval(conn, code)
        return request.respond("deny")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        row = store.decision_for(conn, code)
        status = str((row or {}).get("status") or "")
        if status == "approved":
            rt.audit(
                kind="approval",
                person_id=person_id,
                tool=tool,
                needs_approval=True,
                approved=True,
                note=f"code {code} approved by {(row or {}).get('decided_by') or 'owner'}",
            )
            return request.respond(_coerce(str((row or {}).get("choice") or "once"), allowed))
        if status == "denied":
            rt.audit(
                kind="approval",
                person_id=person_id,
                tool=tool,
                needs_approval=True,
                approved=False,
                note=f"code {code} refused by {(row or {}).get('decided_by') or 'owner'}",
            )
            return request.respond("deny")
        time.sleep(POLL_INTERVAL_S)

    store.expire_approval(conn, code)
    rt.audit(
        kind="approval",
        person_id=person_id,
        tool=tool,
        needs_approval=True,
        approved=False,
        note=f"code {code} expired after {int(timeout_s)}s — denied",
    )
    return request.respond("deny")


def make_transport(rt) -> Callable[[Any], Any]:
    """Build the ``present_fn`` the host calls for every approval while this transport is selected."""

    def present(request: Any) -> Any:
        try:
            return _present(rt, request)
        except Exception as exc:  # noqa: BLE001 - a transport bug must never authorize, but it must be visible
            logger.exception("manyhands: approval transport failed for %r", getattr(request, "pattern_key", ""))
            try:
                rt.audit(
                    kind="approval",
                    tool=str(getattr(request, "pattern_key", "") or ""),
                    needs_approval=True,
                    approved=False,
                    note=f"transport error: {type(exc).__name__}: {exc}"[:300],
                )
            except Exception:
                pass
            # Never let a transport bug authorize anything.
            try:
                return request.respond("deny")
            except Exception:
                raise

    return present
