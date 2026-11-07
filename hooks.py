"""manyhands — the hooks.

``pre_llm_call``  is the only place the sender's identity exists (``sender_id`` + ``platform``).
                  It resolves the person, applies the unknown-sender policy, enforces the daily
                  budget, records the turn's identity, and injects that person's card.
``pre_tool_call`` is where permissions are enforced — but it receives NO sender identity, so it
                  reads the map written above and fails CLOSED when every source comes up empty.
``post_api_request`` carries the token usage of each model call; it is attributed to whoever owns
                  the turn, which is what makes ``daily_tokens`` and ``manyhands_audit`` real numbers
                  instead of estimates.

Identity is keyed on ``turn_id`` FIRST and ``session_id`` second, because both hooks receive
``turn_id`` for the same turn while a session can be shared by several people
(``group_sessions_per_user: false`` — the exact configuration a household picks). With a
session-only key, two people talking in one shared session would be attributed to whoever spoke
last, and a tool call from one of them would be judged against the other's role.

Every hook is wrapped: a plugin that raises inside ``pre_llm_call`` breaks every turn of every
session, so anything unexpected returns ``None`` (do nothing) rather than an exception.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import identity, policy, store, transport
from .runtime import Runtime


# --------------------------------------------------------------------------- context building


def build_person_context(rt: Runtime, person: dict[str, Any]) -> str:
    """The text appended to this person's user message. Small on purpose (< 2 KB)."""
    conn = rt.conn
    pid = person["person_id"]
    who = identity.label(person)
    role = policy.normalize_role(person.get("role"))
    card = policy.tool_card(role, person.get("tools"))
    if "*" in card["allow"]:
        tool_line = "every tool"
    else:
        tool_line = ", ".join(card["allow"]) or "nothing"
        if card["ask"]:
            tool_line += f" · needs the owner's approval first: {', '.join(card['ask'])}"

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
    # The platform's own display name, so the owner's approval prompt says "Haruki wants terminal"
    # instead of an id. Cosmetic only: identity is still decided by (platform, sender_id).
    sender_name = str(
        kwargs.get("sender_name") or kwargs.get("user_name") or kwargs.get("display_name") or ""
    ).strip()

    conn = rt.conn

    # 1. The host talking to itself (cron, internal notices, auto-continuation). Recorded, never
    #    policed — an operator's scheduled job is not a stranger.
    if identity.is_machine_turn(user_message):
        rt.remember(session_id, identity.OPERATOR, platform or "host", turn_id=turn_id)
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

    if person is not None and sender_name and not person.get("name"):
        try:
            store.set_person_name(conn, person["person_id"], sender_name[:60])
            person["name"] = sender_name[:60]
        except Exception:
            pass

    if person is None:
        if refusal is None:
            # No identity on the event at all: the local CLI seat, which is the operator's own.
            rt.remember(session_id, identity.OPERATOR, platform or "cli", turn_id=turn_id)
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
    rt.remember(session_id, pid, platform, turn_id=turn_id)

    # 3. Daily budget (0 = unlimited). Tokens are counted from post_api_request, not estimated.
    cap = int(person.get("daily_tokens") or 0)
    if cap > 0 and not rt.paused:
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

    rt.audit(
        kind="turn",
        person_id=pid,
        session_id=session_id,
        turn_id=turn_id,
        note=(platform or None) if not rt.paused else "plugin paused by owner — turn not enforced",
    )
    # Pause switches ENFORCEMENT off; identity stays. A paused plugin that stops telling the model
    # who it is talking to is a plugin that looks broken.
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

    person, source = rt.person_for_session(session_id, turn_id)

    # Fail closed. A session that never carried a human turn (cold start after a restart, a
    # subagent, a tool driven from the socket) gets the guest allowlist unless the operator opts
    # into `unknown_session_policy: operator`.
    cold = person is None
    if cold:
        if str(rt.setting("unknown_session_policy", "guest")).lower() == "operator":
            person = dict(identity.OPERATOR_PERSON)
        else:
            person = {
                "person_id": None,
                "role": policy.GUEST,
                "tools": [],
                "display": "unknown session",
            }

    who = identity.label(person)
    decision = policy.decide(
        role=person.get("role"),
        tool=tool_name,
        person_tools=person.get("tools"),
        approval_tools=rt.approval_tools(),
        paused=False,
        who=who,
    )

    note = decision["reason"] + (
        f" (cold session: {source})" if cold else f" ({source})"
    )
    if cold:
        note += " — failed closed, no human turn on this session"

    pid = person.get("person_id")
    action = decision["action"]
    allowed = action == policy.ALLOW
    needs = action == policy.ASK

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

    if action == policy.BLOCK:
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
        # The host's action gate (`request_tool_approval` → `_ACTION_GATE`) never consults approval
        # transports — only the dangerous-command and execute_code gates set `spec.transport`. So a
        # plugin-level escalation cannot reach the owner through the transport seam, and the prompt
        # would land in whatever chat asked for the tool. manyhands owns this hop itself:
        #   1. park the ask under a short code and DM the owner,
        #   2. block this attempt (nothing runs on silence),
        #   3. the owner answers `/many approve CODE` in their own chat → one-shot grant,
        #   4. the asker's next attempt spends the grant and runs exactly once.
        grant = None if cold else store.live_grant(rt.conn, pid, tool_name)
        if grant and store.consume_grant(rt.conn, grant["code"]):
            rt.audit(
                kind="approval",
                person_id=pid,
                session_id=session_id,
                turn_id=turn_id,
                tool=tool_name,
                needs_approval=True,
                approved=True,
                note=f"code {grant['code']} spent — runs once",
            )
            return None

        code, delivered, owner_who = _ask_owner(rt, person=person, tool=tool_name, args=args, who=who)
        if not delivered:
            rt.audit(
                kind="refusal",
                person_id=pid,
                session_id=session_id,
                turn_id=turn_id,
                tool=tool_name,
                allowed=False,
                note=f"could not reach the owner ({owner_who or 'nobody on the roster'}) — denied",
            )
            return {
                "action": "block",
                "message": (
                    f"I couldn't reach the owner to ask about `{tool_name}`, so I haven't run it. "
                    "Nothing happened."
                ),
            }
        return {
            "action": "block",
            "message": (
                f"`{tool_name}` needs the owner's say-so. I've sent {owner_who} a request as code "
                f"`{code}` — nothing runs until they answer `/many approve {code}` in their own chat. "
                "Once they approve, ask me again and it will run once."
            ),
        }

    return None


def _ask_owner(
    rt: Runtime, *, person: dict[str, Any], tool: str, args: Any, who: str
) -> tuple[str, bool, str]:
    """Park an ask under a short code and send it to the owner's DM.

    Returns ``(code, delivered, owner_label)``. ``delivered`` is False when there is no owner, or
    the gateway could not carry the DM — both are the caller's cue to fail closed.
    """
    owner = identity.pinned_owner(rt.conn)
    if not owner:
        return "", False, ""
    owner_who = identity.label(owner)
    timeout_s = float(rt.setting("approval_timeout_s", 120) or 120)
    timeout_s = max(timeout_s, 5.0)
    try:
        code = transport.new_code(rt.conn)
    except Exception:
        code = ""
    if not code:
        return "", False, owner_who

    detail = ""
    if args:
        try:
            detail = json.dumps(args, ensure_ascii=False)[:200]
        except Exception:
            detail = ""
    summary = f"{who} wants {tool}" + (f" {detail}" if detail else "")
    # One live code per person and tool: a second ask supersedes the first instead of leaving two
    # prompts with different codes in the owner's chat.
    try:
        store.supersede_pending(rt.conn, person.get("person_id"), tool)
    except Exception:
        pass
    store.bind_approval(
        rt.conn,
        from_code="",
        code=code,
        request_id="",
        digest="",
        expires_at=time.time() + timeout_s,
        tool=tool,
        person_id=person.get("person_id"),
        summary=summary,
    )
    text = "\n".join(
        line
        for line in (
            f"*Approval needed* — {who}",
            "",
            f"`{tool}`" + (f" `{detail}`" if detail else ""),
            "",
            f"Reply `/many approve {code}` to allow it once, or `/many deny {code}` to refuse.",
            f"Nothing runs if nobody answers within {int(timeout_s)}s.",
        )
        if line is not None
    )
    delivered = rt.dm(owner, text)
    rt.audit(
        kind="approval",
        person_id=person.get("person_id"),
        tool=tool,
        needs_approval=True,
        approved=None,
        note=f"code {code} -> owner DM ({'delivered' if delivered else 'DELIVERY FAILED'})",
    )
    if not delivered:
        # Nobody has the code, so it must not be spendable and must not sit in "waiting on you".
        try:
            store.expire_approval(rt.conn, code)
        except Exception:
            pass
    return code, bool(delivered), owner_who


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
    turn_id = str(kwargs.get("turn_id") or "")
    person, _ = rt.person_for_session(session_id, turn_id)
    pid = person.get("person_id") if person else None
    rt.audit(
        kind="tokens",
        person_id=pid,
        session_id=session_id,
        turn_id=turn_id,
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
