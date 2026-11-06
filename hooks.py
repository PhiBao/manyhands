"""manyhands — the three hooks.

``pre_llm_call``  is the only place the sender's identity exists (``sender_id`` + ``platform``).
                  It resolves the person, applies the unknown-sender policy, enforces the daily
                  budget, records ``session_id -> person_id``, and injects that person's card.
``pre_tool_call`` is where permissions are enforced — but it receives NO sender identity, so it
                  reads the map written above and fails CLOSED when the map and the database both
                  come up empty.
``post_api_request`` carries the token usage of each model call; it is attributed to whoever owns
                  the session, which is what makes ``daily_tokens`` and ``manyhands_audit`` real
                  numbers instead of estimates.

Every hook is wrapped: a plugin that raises inside ``pre_llm_call`` breaks every turn of every
session, so anything unexpected returns ``None`` (do nothing) rather than an exception.
"""

from __future__ import annotations

import json
from typing import Any

from . import identity, policy, store
from .runtime import Runtime


# --------------------------------------------------------------------------- context building


def build_person_context(rt: Runtime, person: dict[str, Any]) -> str:
    """The text appended to this person's user message. Small on purpose (< 2 KB)."""
    conn = rt.conn
    pid = person["person_id"]
    who = identity.label(person)
    role = policy.normalize_role(person.get("role"))
    tools = policy.ROLE_TOOLS.get(role, []) if not person.get("tools") else person["tools"]
    tool_line = "every tool" if "*" in (tools or []) else ", ".join(tools or []) or "no tools"

    lines = [
        f"[manyhands] This turn's speaker is {who} ({role}, id {pid}). Address them as themselves, "
        "with their own name and role.",
        f"Their tools: {tool_line}. Do not offer a tool they do not have — say what you can do "
        "instead, without describing what anyone else has access to.",
    ]

    facts = store.list_facts(conn, pid, limit=int(rt.setting("max_person_facts", 8) or 8))
    if facts:
        lines.append(f"What you know about {who} (their own notes, nobody else's):")
        for fact in facts:
            topic = f"{fact['topic']}: " if fact.get("topic") else ""
            lines.append(f"- {topic}{fact['text']}")
    else:
        lines.append(f"You have no stored facts about {who} yet — say so rather than guessing.")

    shared = store.list_shared(conn, limit=int(rt.setting("max_shared_facts", 5) or 5))
    if shared:
        lines.append("Facts shared with everybody in this chat:")
        for fact in shared:
            lines.append(f"- {fact['text']}")

    return "\n".join(lines)


# --------------------------------------------------------------------------- pre_llm_call


def _pre_llm_call(rt: Runtime, **kwargs: Any) -> dict[str, Any] | None:
    user_message = kwargs.get("user_message") or ""
    session_id = str(kwargs.get("session_id") or "")
    turn_id = str(kwargs.get("turn_id") or "")
    platform = str(kwargs.get("platform") or "")
    sender_id = str(kwargs.get("sender_id") or "")

    conn = rt.conn

    # 1. The host talking to itself (cron, internal notices, auto-continuation). Recorded, never
    #    policed — an operator's scheduled job is not a stranger.
    if identity.is_machine_turn(user_message):
        rt.remember(session_id, identity.OPERATOR, platform or "host")
        rt.audit(
            kind="turn",
            person_id=identity.OPERATOR,
            session_id=session_id,
            turn_id=turn_id,
            note="machine-generated turn (not policed)",
        )
        return None

    # 2. Resolve identity from the platform event. Message text is never evidence.
    person, refusal = identity.resolve(
        conn,
        platform=platform,
        sender_id=sender_id,
        policy=str(rt.setting("unknown_sender_policy", "invite")),
        invite_role=str(rt.setting("invite_role", "member")),
        user_message=user_message,
    )

    if person is None:
        if refusal is None:
            # No identity on the event at all: the local CLI seat, which is the operator's own.
            rt.remember(session_id, identity.OPERATOR, platform or "cli")
            return None
        rt.audit(
            kind="refusal",
            session_id=session_id,
            turn_id=turn_id,
            note=f"unknown sender {platform or '?'}:{sender_id or '?'} refused "
            f"(policy={rt.setting('unknown_sender_policy', 'invite')})",
        )
        return {"action": "block", "message": refusal}

    pid = person["person_id"]
    rt.remember(session_id, pid, platform)

    # 3. Owner switch: /many pause stops enforcement and says so in the audit trail.
    if rt.paused:
        rt.audit(
            kind="policy",
            person_id=pid,
            session_id=session_id,
            turn_id=turn_id,
            note="plugin paused by owner — turn not enforced",
        )
        return None

    # 4. Daily budget (0 = unlimited). Tokens are counted from post_api_request, not estimated.
    cap = int(person.get("daily_tokens") or 0)
    if cap > 0:
        spent = store.tokens_today(conn, pid)
        if spent >= cap:
            rt.audit(
                kind="refusal",
                person_id=pid,
                session_id=session_id,
                turn_id=turn_id,
                tokens=spent,
                note=f"daily budget reached ({spent}/{cap} tokens)",
            )
            return {
                "action": "block",
                "message": (
                    f"I've used up today's budget for {identity.label(person)} "
                    f"({spent:,} of {cap:,} tokens). The owner can raise it, and it resets at "
                    "00:00 UTC."
                ),
            }

    rt.audit(kind="turn", person_id=pid, session_id=session_id, turn_id=turn_id, note=platform or None)
    return {"context": build_person_context(rt, person)}


def make_pre_llm_call(rt: Runtime):
    def hook(**kwargs: Any) -> dict[str, Any] | None:
        try:
            return _pre_llm_call(rt, **kwargs)
        except Exception:
            return None

    return hook


# --------------------------------------------------------------------------- pre_tool_call


def _pre_tool_call(rt: Runtime, **kwargs: Any) -> dict[str, Any] | None:
    tool_name = str(kwargs.get("tool_name") or "")
    session_id = str(kwargs.get("session_id") or "")
    turn_id = str(kwargs.get("turn_id") or "")
    args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {}

    if not tool_name:
        return None

    if rt.paused:
        rt.audit(
            kind="policy",
            session_id=session_id,
            turn_id=turn_id,
            tool=tool_name,
            allowed=True,
            note="plugin paused by owner — tool not enforced",
        )
        return None

    person, source = rt.person_for_session(session_id)

    # Fail closed. A session that never carried a human turn (cold start after a restart, a
    # subagent, a tool driven from the socket) gets the guest allowlist unless the operator opts
    # into `unknown_session_policy: operator`.
    cold = person is None
    if cold:
        if str(rt.setting("unknown_session_policy", "guest")).lower() == "operator":
            person = dict(identity.OPERATOR_PERSON)
        else:
            person = {"person_id": None, "role": policy.GUEST, "tools": [], "display": "unknown session"}

    who = identity.label(person)
    decision = policy.decide(
        role=person.get("role"),
        tool=tool_name,
        person_tools=person.get("tools"),
        approval_tools=rt.approval_tools(),
        paused=False,
        who=who,
    )

    note = decision["reason"] + (f" (cold session: {source})" if cold else f" ({source})")
    if cold:
        note += " — failed closed, no human turn on this session"

    pid = person.get("person_id")
    allowed = decision["action"] == policy.ALLOW
    needs = decision["action"] == policy.APPROVE

    rt.audit(
        kind="tool",
        person_id=pid,
        session_id=session_id,
        turn_id=turn_id,
        tool=tool_name,
        allowed=allowed,
        needs_approval=needs,
        note=note,
    )

    if decision["action"] == policy.BLOCK:
        rt.audit(
            kind="refusal",
            person_id=pid,
            session_id=session_id,
            turn_id=turn_id,
            tool=tool_name,
            allowed=False,
            note=decision["reason"],
        )
        return {"action": "block", "message": decision["message"]}

    if needs:
        # Park the escalation so the approval transport knows who asked and what for. The host's
        # gate decides how the prompt is presented; if the operator selected the manyhands
        # transport, the prompt goes to the owner's DM (see transport.py).
        try:
            store.add_escalation(
                rt.conn,
                tool=tool_name,
                person_id=pid,
                summary=f"{who} wants {tool_name}"
                + (f" {json.dumps(args, ensure_ascii=False)[:160]}" if args else ""),
            )
        except Exception:
            pass
        return {
            "action": "approve",
            "message": decision["message"],
            "rule_key": decision.get("rule_key") or tool_name,
        }

    return None


def make_pre_tool_call(rt: Runtime):
    def hook(**kwargs: Any) -> dict[str, Any] | None:
        try:
            return _pre_tool_call(rt, **kwargs)
        except Exception:
            return None

    return hook


# --------------------------------------------------------------------------- post_api_request


def _post_api_request(rt: Runtime, **kwargs: Any) -> None:
    usage = kwargs.get("usage") or {}
    if not isinstance(usage, dict):
        return
    tokens = int(usage.get("total_tokens") or 0)
    if tokens <= 0:
        return
    session_id = str(kwargs.get("session_id") or "")
    person, _ = rt.person_for_session(session_id)
    pid = person.get("person_id") if person else None
    rt.audit(
        kind="tokens",
        person_id=pid,
        session_id=session_id,
        turn_id=str(kwargs.get("turn_id") or ""),
        tokens=tokens,
        note=str(kwargs.get("model") or ""),
    )


def make_post_api_request(rt: Runtime):
    def hook(**kwargs: Any) -> None:
        try:
            _post_api_request(rt, **kwargs)
        except Exception:
            return None
        return None

    return hook
