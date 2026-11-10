"""manyhands — the `llm_request` middleware: the per-person system prompt.

Why this exists
---------------
`pre_llm_call` can only append text to the *user message*. Hermes builds its system prompt for
every session from the operator's own state: `MEMORY.md` (the agent's private notes) and `USER.md`
(who the owner is), rendered as two delimited blocks. Without this middleware a guest talking to a
shared bot is handed the owner's memory verbatim — which makes "everyone gets their own Hermes" a
lie, no matter how good the tool permissions are.

`ctx.register_middleware("llm_request", cb)` is the only public seam that can rewrite the outgoing
provider payload. Hermes calls it once per API request with the full request plus `session_id`,
`turn_id` and `platform`, and a returned `{"request": {...}}` replaces the kwargs that go to the
model. So this module:

  * resolves the person for the request (turn map -> session map -> nothing),
  * leaves the owner's and the host's own turns completely untouched,
  * for everyone else strips the host's MEMORY/USER blocks out of the system prompt,
  * and puts the person's own card where it belongs — in the system prompt, not stapled to their
    message.

Fail-open by design: this is a privacy filter, not an authorization gate. If anything about the
payload is unexpected, return None and let the request through unchanged; `pre_tool_call` is what
stops a tool, and it never depends on this module having run.
"""

from __future__ import annotations

import re
from typing import Any

from . import identity, policy

#: Hermes renders each built-in memory block as a 46-char rule, a title line, another rule, then the
#: entries (`tools/memory_tool_store.py: _render_block`). Kept as literals so this module does not
#: import host internals to stay correct; the live probe asserts the real headers still match.
BLOCK_RULE = "═" * 46
HOST_MEMORY_TITLES = (
    "MEMORY (your personal notes)",
    "USER PROFILE (who the user is)",
)

_TITLE = r"(?P<title>[^\n]{1,120}?)"
_USAGE = r"(?: \[\d+%\])?"

_BLOCK_RE = re.compile(
    rf"(?P<rule>{re.escape(BLOCK_RULE)})\n{_TITLE}{_USAGE}\n"
    rf"{re.escape(BLOCK_RULE)}\n(?P<body>.*?)(?=\n{re.escape(BLOCK_RULE)}\n|\Z)",
    re.DOTALL,
)

#: Never append the person card twice to the same request.
CARD_MARKER = "[manyhands]"

WITHHELD_NOTE = (
    "[manyhands] The operator's own memory and user profile are withheld from this conversation "
    "because it is not the operator's. Do not claim to remember anything about the operator here. "
    "Use only the facts in this block, and if a fact is missing, say so instead of guessing."
)


def strip_host_memory(text: str) -> tuple[str, int]:
    """Remove the host's MEMORY/USER blocks from *text*; return ``(text, blocks_removed)``."""
    if not text or BLOCK_RULE not in text:
        return text, 0
    removed = 0

    def _drop_or_keep(match: re.Match[str]) -> str:
        nonlocal removed
        title = re.sub(r"\s*\[[^\]]*\]\s*$", "", match.group("title")).strip()
        if title not in HOST_MEMORY_TITLES:
            return match.group(0)
        removed += 1
        return ""

    out = _BLOCK_RE.sub(_drop_or_keep, text)
    if removed:
        out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out, removed


def person_card(rt, person: dict[str, Any]) -> str:
    """The system-prompt block for one person. Bounded, and honest about what was withheld."""
    who = identity.label(person)
    role = policy.normalize_role(person.get("role"))
    card = policy.tool_card(role, person.get("tools"))
    allow = "every tool" if "*" in card["allow"] else ", ".join(card["allow"]) or "nothing"
    ask = ", ".join(card["ask"]) or "nothing"
    lines = [
        f"{CARD_MARKER} This conversation belongs to {who} ({role}, id {person.get('person_id')}).",
        f"Tools that run without asking the owner: {allow}.",
        f"Tools that need the owner's approval first: {ask}.",
        (
            "Calling one of those is the right move when they ask for it: the call is parked under a "
            "code, the owner is messaged in their own DM, and nothing runs until they answer. Do "
            "not tell them a tool is unavailable — offer to ask."
            if card["ask"]
            else "Do not offer a tool outside those buckets, and never describe what anybody else has access to."
        ),
    ]
    try:
        facts = rt.conn.execute(
            "SELECT topic, text FROM facts WHERE person_id = ? ORDER BY id DESC LIMIT ?",
            (person.get("person_id"), int(rt.setting("max_person_facts", 8) or 8)),
        ).fetchall()
    except Exception:
        facts = []
    if facts:
        lines.append(f"Facts {who} stored for themselves:")
        lines += [f"- {f['topic']}: {f['text']}" if f["topic"] else f"- {f['text']}" for f in facts]
    else:
        lines.append(f"No stored facts about {who} yet.")
    return "\n".join(lines)


def _rewrite_text(text: str, rt, person: dict[str, Any]) -> tuple[str, int]:
    stripped, removed = strip_host_memory(text)
    if CARD_MARKER in stripped:  # already injected this request (or a retry of it)
        return stripped, removed
    prefix = WITHHELD_NOTE if removed or HOST_MEMORY_TITLES[0] else ""
    card = person_card(rt, person)
    joined = "\n\n".join(part for part in (stripped.rstrip(), prefix, card) if part)
    return joined, removed


def _rewrite_messages(messages: list[Any], rt, person: dict[str, Any]) -> tuple[list[Any], int, bool]:
    changed = False
    removed_total = 0
    out: list[Any] = []
    injected = any(
        isinstance(m, dict) and CARD_MARKER in str(m.get("content") or "") for m in messages
    )
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            out.append(message)
            continue
        if message.get("role") != "system" or (injected and removed_total):
            out.append(message)
            continue
        content = message.get("content")
        if isinstance(content, str):
            new_text, removed = _rewrite_text(content, rt, person)
            removed_total += removed
            if new_text != content:
                out.append({**message, "content": new_text})
                changed = True
            else:
                out.append(message)
            injected = True
            continue
        if isinstance(content, list):
            new_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                    new_text, removed = _rewrite_text(part["text"], rt, person)
                    removed_total += removed
                    if new_text != part["text"]:
                        new_parts.append({**part, "text": new_text})
                        changed = True
                        continue
                new_parts.append(part)
            out.append({**message, "content": new_parts} if new_parts != content else message)
            injected = True
            continue
        out.append(message)
    return out, removed_total, changed


def rewrite_request(rt, request: dict[str, Any], *, person: dict[str, Any]) -> dict[str, Any] | None:
    """Return the replacement kwargs, or None when nothing should change."""
    body = dict(request)
    removed_total = 0
    changed = False

    messages = body.get("messages")
    if isinstance(messages, list) and messages:
        new_messages, removed_total, changed = _rewrite_messages(messages, rt, person)
        if changed:
            body["messages"] = new_messages

    # Anthropic-style system prompt sits outside `messages`.
    for key in ("system", "instructions"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            new_text, removed = _rewrite_text(value, rt, person)
            if new_text != value:
                body[key] = new_text
                changed = True
                removed_total += removed

    if not changed:
        return None
    rt.audit(
        kind="policy",
        person_id=person.get("person_id"),
        note=(
            "llm_request: system prompt scoped to this person "
            f"({removed_total} host memory block(s) withheld)"
        ),
    )
    return body


def make_llm_request_middleware(rt):
    """Build the callback `ctx.register_middleware("llm_request", ...)` expects."""

    def middleware(
        request: Any = None,
        original_request: Any = None,
        session_id: str = "",
        turn_id: str = "",
        task_id: str = "",
        platform: str = "",
        **kwargs: Any,
    ):
        try:
            if not isinstance(request, dict) or not request:
                return None
            if not bool(rt.setting("scope_host_memory", True)):
                return None
            person, source = rt.person_for_session(
                session_id or "", turn_id or "", task_id or ""
            )
            if person is None:
                if source != "ambiguous":
                    return None  # unknown session: leave the host's own request alone
                # Several people have spoken in this session and this request carries no turn/task
                # identity, so there is no "not the owner" to prove. Withhold rather than fail open:
                # an unidentified request in a shared session is the one case where guessing hands
                # somebody the operator's own notes.
                person = {
                    "person_id": None,
                    "role": policy.GUEST,
                    "tools": [],
                    "display": "an unidentified speaker in a shared session",
                }
            if str(person.get("person_id")) == identity.OPERATOR:
                return None  # the host talking to itself
            if policy.normalize_role(person.get("role")) == policy.OWNER:
                return None  # the owner's own memory is theirs
            body = rewrite_request(rt, request, person=person)
            if body is None:
                return None
            return {
                "request": body,
                "reason": f"manyhands: system prompt scoped to {identity.label(person)}",
                "source": "manyhands",
            }
        except Exception:
            return None  # never break or block a request over this

    return middleware
