"""manyhands — the permission policy, as pure functions.

No database, no hooks, no logging, no I/O: `decide()` takes a role, a tool name and two settings and
returns what the host should do. That is deliberate — the security-critical decision is the one thing
in this plugin that can be read, unit-tested and reviewed without any of the rest of it.

The model is an ALLOWLIST, not a denylist. A tool that is not named for a role is refused, including
tools that ship in a future Hermes release, third-party plugin tools and MCP tools. The alternative
(a denylist of "dangerous" tools) fails open on every tool nobody thought of.
"""

from __future__ import annotations

from typing import Any, Iterable

OWNER = "owner"
MEMBER = "member"
GUEST = "guest"
BLOCKED = "blocked"

ROLES = (OWNER, MEMBER, GUEST, BLOCKED)

#: Default tool allowlist per role. `*` means everything, present and future.
ROLE_TOOLS: dict[str, list[str]] = {
    OWNER: ["*"],
    # A member can answer, search, and keep their own notes. Nothing that writes outside the
    # plugin, nothing that spends money, nothing that reads the owner's files.
    MEMBER: [
        "manyhands_whoami",
        "manyhands_remember",
        "manyhands_recall",
        "manyhands_share",
        "web_search",
        "web_extract",
        "recall_search",
        "recall_open",
        "todo_list",
        "ask_question",
    ],
    # A guest gets answers, not tools: no memory writes at all, and not even their own.
    GUEST: ["manyhands_whoami", "manyhands_recall", "web_search"],
    BLOCKED: [],
}

#: Tools that a non-owner may use ONLY with the owner's say-so (when they are allowed at all).
DEFAULT_APPROVAL_TOOLS: list[str] = [
    "terminal",
    "execute_code",
    "write_file",
    "patch",
    "browser_exec",
    "delegate_task",
    "cronjob_manage",
    "memory",
    "computer_use",
    "process_manage",
]

ALLOW = "allow"
BLOCK = "block"
APPROVE = "approve"


def normalize_role(role: str | None) -> str:
    clean = str(role or "").strip().lower()
    return clean if clean in ROLES else GUEST


def allows(role: str, tool: str, person_tools: Iterable[str] | None = None) -> bool:
    """Is *tool* inside the allowlist this role/person carries?

    A person's own list (set by the owner with `manyhands_people`) REPLACES the role default; an
    empty list means "use the role default" rather than "nothing", because an empty allowlist and an
    unset one look identical on disk and silently locking a member out of everything is worse.
    """
    role = normalize_role(role)
    named = [t for t in (person_tools or []) if str(t).strip()]
    if not named:
        named = ROLE_TOOLS.get(role, [])
    if "*" in named:
        return True
    return str(tool) in named


def _refusal_for(role: str, tool: str, who: str) -> str:
    role = normalize_role(role)
    who = (who or "this person").strip() or "this person"
    if role == BLOCKED:
        return (
            "I can't do that — I don't have you on my list of people, and I won't run tools for "
            "someone I can't identify. Ask the owner to add you."
        )
    if role == GUEST:
        return (
            f"I can't use {tool} here — a guest gets answers, not tools. If you need that, ask the "
            "owner to make you a member."
        )
    return (
        f"I can't use {tool} for {who} — it isn't in their permissions. The owner can add it with "
        f"`/many tools {who} add {tool}`."
    )


def decide(
    *,
    role: str | None,
    tool: str,
    person_tools: Iterable[str] | None = None,
    approval_tools: Iterable[str] | None = None,
    paused: bool = False,
    who: str = "this person",
) -> dict[str, Any]:
    """Return the directive the host should apply for one tool call.

    ``{"action": "allow"|"block"|"approve", "reason": ..., "message": ...}``

    Order matters and is asserted by the tests:

      1. paused  -> allow (the owner switched enforcement off; the audit row is the record)
      2. owner   -> allow (the owner is never rate-limited or role-blocked by their own plugin)
      3. not allowed for the role -> block, with a message that names the role
      4. allowed but in the approval set -> approve (the owner's DM decides)
      5. otherwise -> allow
    """
    role = normalize_role(role)
    tool = str(tool or "")

    if paused:
        return {"action": ALLOW, "reason": "paused", "message": ""}
    if role == OWNER:
        return {"action": ALLOW, "reason": "owner", "message": ""}
    if not tool:
        return {"action": ALLOW, "reason": "no-tool", "message": ""}

    if not allows(role, tool, person_tools):
        return {
            "action": BLOCK,
            "reason": f"{role}-not-allowed",
            "message": _refusal_for(role, tool, who),
        }

    needs = [str(t) for t in (approval_tools if approval_tools is not None else DEFAULT_APPROVAL_TOOLS)]
    if tool in needs:
        return {
            "action": APPROVE,
            "reason": f"{role}-needs-approval",
            "message": f"{who} wants to use {tool}; that needs the owner's approval.",
            "rule_key": tool,
        }

    return {"action": ALLOW, "reason": f"{role}-allowed", "message": ""}


def describe_role(role: str, person_tools: Iterable[str] | None = None) -> str:
    """One line for the roster / the injected context: 'member — 10 tools'."""
    role = normalize_role(role)
    named = [t for t in (person_tools or []) if str(t).strip()] or ROLE_TOOLS.get(role, [])
    if "*" in named:
        return f"{role} — every tool"
    return f"{role} — {len(named)} tool{'s' if len(named) != 1 else ''}"
