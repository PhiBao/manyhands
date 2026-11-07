"""manyhands — the permission policy, as pure functions.

No database, no hooks, no logging, no I/O: `decide()` takes a role, a tool name and two lists and
returns what the host should do. That is deliberate — the security-critical decision is the one
thing in this plugin that can be read, unit-tested and reviewed without any of the rest of it.

Three states, not two
---------------------
A role does not have "tools"; it has three buckets:

  ALLOW  the tool runs, no approval, no prompt
  ASK    the tool runs only after the OWNER approves it in their own DM
  (else) never — refused with a message that names the person's own role

"ASK" is what makes this a product rather than a blocklist: a member who asks for a shell command
does not get "no", they get *the owner's phone lighting up*. A guest gets no ASK bucket at all —
escalating on a stranger's behalf is a phishing surface, not a feature.

The default is a WHITELIST for ALLOW. A tool nobody named is refused, including tools that ship in
a future Hermes release, third-party plugin tools and MCP tools. A denylist fails open on every tool
nobody thought of.
"""

from __future__ import annotations

from typing import Any, Iterable

OWNER = "owner"
MEMBER = "member"
GUEST = "guest"
BLOCKED = "blocked"

ROLES = (OWNER, MEMBER, GUEST, BLOCKED)

#: Tools that WRITE, EXECUTE or SPEND. For a non-owner these are "ask the owner", never "yes".
DANGEROUS_TOOLS: tuple[str, ...] = (
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
    "spawn_task",
    "inject_message",
)

#: Default ALLOW bucket per role. `*` means everything, present and future.
ROLE_ALLOW: dict[str, list[str]] = {
    OWNER: ["*"],
    # A member answers questions and keeps their own notes. Nothing that writes outside this
    # plugin, nothing that spends money, and NOTHING that reads the operator's own files: the
    # sibling `recall` tools grep the owner's disk, so they are deliberately absent here. An owner
    # who wants a member to have them grants them explicitly, per person.
    MEMBER: [
        "manyhands_whoami",
        "manyhands_remember",
        "manyhands_recall",
        "manyhands_share",
        "web_search",
        "web_extract",
        "todo_list",
        "ask_question",
    ],
    # A guest gets answers, not tools: no memory writes, and no escalation either.
    GUEST: ["manyhands_whoami", "manyhands_recall", "web_search"],
    BLOCKED: [],
}

#: Default ASK bucket per role. An empty list means "this role cannot escalate at all".
ROLE_ASK: dict[str, list[str]] = {
    OWNER: [],
    MEMBER: list(DANGEROUS_TOOLS),
    GUEST: [],
    BLOCKED: [],
}

#: Back-compat: the old name for the ALLOW bucket.
ROLE_TOOLS = ROLE_ALLOW
#: Back-compat: the old name for the default ASK bucket.
DEFAULT_APPROVAL_TOOLS: list[str] = list(DANGEROUS_TOOLS)

ALLOW = "allow"
ASK = "ask"
BLOCK = "block"

#: Back-compat alias: `approve` was the old name for the ASK action.
APPROVE = ASK


def normalize_role(role: str | None) -> str:
    clean = str(role or "").strip().lower()
    return clean if clean in ROLES else GUEST


def _clean(names: Iterable[str] | None) -> list[str]:
    return [str(n).strip() for n in (names or []) if str(n).strip()]


def allows(role: str, tool: str, person_tools: Iterable[str] | None = None) -> bool:
    """Is *tool* inside the ALLOW bucket this role/person carries?

    A person's own list (set by the owner with `manyhands_people` or `/many tools`) REPLACES the
    role default for ALLOW — an explicit grant is an explicit grant, it does not ask again. An empty
    list falls back to the role default, because an empty list and an unset one look identical on
    disk and silently locking a member out of everything is worse.
    """
    role = normalize_role(role)
    named = _clean(person_tools) or ROLE_ALLOW.get(role, [])
    if "*" in named:
        return True
    return str(tool) in named


def asks(role: str, tool: str, person_tools: Iterable[str] | None = None) -> bool:
    """Does this tool need the owner's approval for this role/person?

    An explicit per-person grant wins over the role's ASK bucket: if the owner already added
    `terminal` to Minh's own tool list they said yes, and re-prompting them is noise.
    """
    role = normalize_role(role)
    if role == OWNER:
        return False
    if str(tool) in _clean(person_tools):
        return False
    return str(tool) in ROLE_ASK.get(role, [])


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
        f"I can't use {tool} for {who} — it isn't in their permissions and it isn't one I can ask "
        f"the owner about on their behalf. The owner can add it with "
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

    ``{"action": "allow"|"ask"|"block", "reason": ..., "message": ...}``

    ``approval_tools`` overrides the role's ASK bucket when the operator configured one; pass None
    to use the defaults in ``ROLE_ASK``.

    Order matters and is asserted by the tests:

      1. paused          -> allow  (the owner switched enforcement off; the audit row is the record)
      2. owner           -> allow  (never rate-limited or role-blocked by their own plugin)
      3. explicit grant  -> allow  (the owner added it to this person's own list)
      4. ASK bucket      -> ask    (the owner's DM decides)
      5. role ALLOW      -> allow
      6. otherwise       -> block, with a message that names the person's own role
    """
    role = normalize_role(role)
    tool = str(tool or "")

    if paused:
        return {"action": ALLOW, "reason": "paused", "message": ""}
    if role == OWNER:
        return {"action": ALLOW, "reason": "owner", "message": ""}
    if not tool:
        return {"action": ALLOW, "reason": "no-tool", "message": ""}

    granted = _clean(person_tools)
    if tool in granted:
        return {"action": ALLOW, "reason": f"{role}-granted", "message": ""}

    ask_bucket = _clean(approval_tools) if approval_tools is not None else ROLE_ASK.get(role, [])
    if tool in ask_bucket:
        return {
            "action": ASK,
            "reason": f"{role}-needs-approval",
            "message": f"{who} wants to use {tool}; that needs the owner's approval.",
            "rule_key": tool,
        }

    if allows(role, tool, granted):
        return {"action": ALLOW, "reason": f"{role}-allowed", "message": ""}

    return {
        "action": BLOCK,
        "reason": f"{role}-not-allowed",
        "message": _refusal_for(role, tool, who),
    }


def describe_role(role: str, person_tools: Iterable[str] | None = None) -> str:
    """One line for the roster / the injected context: 'member — 8 tools, 12 need approval'."""
    role = normalize_role(role)
    named = _clean(person_tools) or ROLE_ALLOW.get(role, [])
    if "*" in named:
        return f"{role} — every tool"
    ask = [t for t in ROLE_ASK.get(role, []) if t not in named]
    parts = [f"{len(named)} tool{'s' if len(named) != 1 else ''}"]
    if ask:
        parts.append(f"{len(ask)} need the owner's approval")
    if not named and not ask:
        parts.append("nothing")
    return f"{role} — " + ", ".join(parts)


def tool_card(role: str, person_tools: Iterable[str] | None = None) -> dict[str, list[str]]:
    """The buckets, resolved, for `manyhands_whoami` and the injected person card."""
    role = normalize_role(role)
    named = _clean(person_tools) or ROLE_ALLOW.get(role, [])
    if "*" in named:
        return {"allow": ["*"], "ask": [], "never": []}
    ask = [t for t in ROLE_ASK.get(role, []) if t not in named]
    return {"allow": named, "ask": ask, "never": []}
