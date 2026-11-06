"""manyhands — one Hermes, many people.

Every sender who talks to this bot gets their own identity, their own memory, their own tool
allowlist and their own approvals, and the owner can see who asked for what.

Registers (and nothing else — see plugin.yaml):

  tools      manyhands_whoami, manyhands_people, manyhands_remember, manyhands_recall,
             manyhands_share, manyhands_audit
  hooks      pre_llm_call   identity, unknown-sender policy, budget, per-person context, session map
             pre_tool_call  role -> tool allowlist, and escalation to the owner
             post_api_request  token usage attributed to the person who owns the session
  transport  `manyhands` — approval prompts go to the owner's DM (security.approval.transport)
  command    /many
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import hooks as _hooks
from . import identity, policy, store
from . import tools as _tools
from .runtime import Runtime
from .transport import make_transport

logger = logging.getLogger(__name__)

TOOLSET = "manyhands"

_runtime: Runtime | None = None


# --------------------------------------------------------------------------- identity for commands


def _source_identity(source: Any) -> tuple[str, str]:
    """(platform, sender_id) from a gateway SessionSource, or ('', '') when there is none."""
    if source is None:
        return "", ""
    platform = getattr(source, "platform", "")
    platform = getattr(platform, "value", platform) or ""
    sender = str(getattr(source, "user_id", "") or "")
    if not sender and str(getattr(source, "chat_type", "") or "") == "dm":
        sender = str(getattr(source, "chat_id", "") or "")
    return str(platform), sender


def _actor(rt: Runtime, source: Any, session_id: str) -> tuple[dict[str, Any], str]:
    """Who typed this slash command, and how we know.

    The gateway hands a command handler the ``SessionSource`` of the message that carried it, so a
    command is attributable to the human who sent it — the same identity the turn hook uses. A local
    CLI seat has no source at all and is the operator's own.
    """
    platform, sender = _source_identity(source)
    if sender:
        row = store.get_person_by_sender(rt.conn, platform, sender)
        if row:
            return row, "source"
    if session_id:
        person, how = rt.person_for_session(session_id)
        if person:
            return person, how
    if source is None and not sender:
        return dict(identity.OPERATOR_PERSON), "local"
    return (
        {
            "person_id": None,
            "role": policy.GUEST,
            "platform": platform,
            "sender_id": sender,
            "display": sender or None,
        },
        "unknown",
    )


def _role_of(person: dict[str, Any]) -> str:
    return policy.normalize_role(person.get("role"))


def _is_owner(person: dict[str, Any]) -> bool:
    return _role_of(person) == policy.OWNER


# --------------------------------------------------------------------------- /many


def _cmd(rt: Runtime, raw_args: str, source: Any = None, session_id: str = "") -> str:
    args = (raw_args or "").strip()
    lowered = args.lower()
    verb = lowered.split()[0] if lowered else "whoami"
    rest = args[len(verb):].strip()

    try:
        person, how = _actor(rt, source, session_id)
        role = _role_of(person)

        # ---- whoami ------------------------------------------------------------
        if verb in {"whoami", "who", "me"}:
            named = person.get("tools") or policy.ROLE_TOOLS.get(role, [])
            facts = (
                store.count_facts(rt.conn, person["person_id"])
                if person.get("person_id") and person["person_id"] != identity.OPERATOR
                else 0
            )
            return (
                f"**{identity.label(person)}** — {role} ({how})\n"
                f"tools: {'every tool' if '*' in named else ', '.join(named) or 'none'}\n"
                f"own facts: {facts} · enforcement: {'PAUSED' if rt.paused else 'on'}"
            )

        # ---- roster ------------------------------------------------------------
        if verb in {"roster", "people", "list"}:
            rows = store.list_people(rt.conn)
            if not rows:
                return (
                    "the roster is empty — `/many claim` from the bot's own terminal mints a claim "
                    "code, then `/many claim CODE` in this chat makes you the owner"
                )
            lines = [f"**roster** — {len(rows)} people"]
            for row in rows:
                lines.append(
                    f"- **{identity.label(row)}** · {policy.describe_role(row.get('role') or '', row.get('tools'))}"
                    f" · {store.count_facts(rt.conn, row['person_id'])} facts"
                    f" · `{row['person_id']}`"
                )
            invites = store.open_invites(rt.conn)
            if invites and _is_owner(person):
                lines.append(f"open invite codes: {', '.join(i['code'] for i in invites)}")
            return "\n".join(lines)

        # ---- claim / invite ----------------------------------------------------
        if verb == "claim":
            if identity.has_owner(rt.conn):
                owner = identity.pinned_owner(rt.conn)
                return f"this Hermes already has an owner ({identity.label(owner)}). Ask them for a role."
            if not rest:
                platform, sender = _source_identity(source)
                if source is None and not sender:
                    code = f"OWNER-{store.new_token()}"
                    store.mint_invite(rt.conn, code, role="owner")
                    return (
                        f"claim code: `{code}`\nSend `/many claim {code}` from the chat account that "
                        "should own this bot. It works once and only while no owner exists."
                    )
                return "usage: /many claim <code> — get a code by running `/many claim` in the bot's own terminal"
            platform, sender = _source_identity(source)
            if not sender:
                return "that has to be redeemed from a chat, so I know which account to make owner"
            person_id = store.person_id_for(platform, sender)
            claimed = store.redeem_invite(rt.conn, rest, person_id)
            if not claimed:
                return "that claim code is not valid, or it was already used"
            if identity.has_owner(rt.conn):
                return "somebody claimed this Hermes first, so that code changed nothing"
            created = store.add_person(
                rt.conn,
                platform=platform,
                sender_id=sender,
                role="owner",
                display=sender,
            )
            identity.pin_owner(rt.conn, created["person_id"])
            rt.audit(kind="policy", person_id=created["person_id"], note="claimed ownership")
            return (
                f"you own this Hermes now — **{identity.label(created)}** ({created['person_id']}).\n"
                "Next: `/many invite` to mint a code for somebody else, `/many roster` to see who is here."
            )

        if verb == "invite":
            if not _is_owner(person):
                return "only the owner can mint invite codes"
            wanted = (rest or "member").strip().lower()
            if wanted not in policy.ROLES:
                return f"role must be one of {', '.join(policy.ROLES)}"
            code = store.new_token(6)
            store.mint_invite(rt.conn, code, role=wanted)
            rt.audit(kind="policy", person_id=person.get("person_id"), note=f"minted {wanted} invite")
            return (
                f"invite code: `{code}` ({wanted}, single use)\n"
                f"They reply `join {code}` in this chat — or you can add them directly with "
                f"`/many add {wanted}`."
            )

        # ---- approve / deny ----------------------------------------------------
        if verb in {"approve", "yes", "allow", "deny", "no", "refuse"}:
            code = rest.split()[0].upper() if rest else ""
            choice = (rest.split()[1].lower() if len(rest.split()) > 1 else "once")
            if not code:
                pending = store.pending_approvals(rt.conn)
                if not pending:
                    return "nothing is waiting on an approval"
                lines = ["**waiting on you**"]
                for row in pending:
                    lines.append(f"- `{row['code']}` · {row.get('summary') or row.get('tool')}")
                lines.append("`/many approve CODE` or `/many deny CODE`")
                return "\n".join(lines)
            approving = verb in {"approve", "yes", "allow"}
            allowed = approving and (_is_owner(person) or person.get("person_id") == identity.OPERATOR)
            if not allowed and approving:
                return "only the owner can approve a tool call"
            if not approving and not (_is_owner(person) or person.get("person_id") == identity.OPERATOR):
                # the asker may always refuse their own request
                row = store.decision_for(rt.conn, code)
                if not row or row.get("person_id") != person.get("person_id"):
                    return "only the owner can decide this one"
            ok = store.decide_approval(
                rt.conn, code, approving, identity.label(person), choice="session" if choice == "session" else "once"
            )
            if not ok:
                return f"`{code}` is not waiting on a decision (expired, already answered, or wrong code)"
            rt.audit(
                kind="approval",
                person_id=person.get("person_id"),
                note=f"{'approved' if approving else 'denied'} {code}",
            )
            return f"`{code}` {'approved' if approving else 'denied'} — the tool call is on its way"

        # ---- role / tools / remove (owner) ------------------------------------
        if verb in {"role", "tools", "remove", "forget"}:
            if not _is_owner(person):
                return "only the owner can change the roster"
            parts = rest.split()
            if not parts:
                return f"usage: /many {verb} <name> " + (
                    "<role>" if verb == "role" else "<tool> [...]" if verb == "tools" else ""
                )
            matches = store.find_by_name(rt.conn, parts[0])
            if not matches:
                return f"no person matches {parts[0]!r} — `/many roster` lists them"
            target = matches[0]
            if verb == "role":
                if len(parts) < 2:
                    return "usage: /many role <name> <owner|member|guest|blocked>"
                new_role = parts[1].lower()
                if new_role not in policy.ROLES:
                    return f"role must be one of {', '.join(policy.ROLES)}"
                if _role_of(target) == policy.OWNER and new_role != policy.OWNER and len(store.owners(rt.conn)) <= 1:
                    return "that is the only owner — promote somebody else first"
                store.set_role(rt.conn, target["person_id"], new_role)
                rt.audit(kind="policy", person_id=target["person_id"], note=f"role -> {new_role}")
                return f"**{identity.label(target)}** is now {new_role} ({policy.describe_role(new_role)})"
            if verb == "tools":
                current = [t for t in (target.get("tools") or [])]
                words = parts[1:]
                if not words:
                    return f"**{identity.label(target)}** tools: {', '.join(current) or 'role default'}"
                mode = "set"
                if words[0].lower() in {"add", "remove", "set", "clear", "reset"}:
                    mode = words[0].lower()
                    words = words[1:]
                if mode == "add":
                    current = sorted(set(current) | set(words))
                elif mode == "remove":
                    current = [t for t in current if t not in set(words)]
                elif mode in {"clear", "reset"}:
                    current = []
                else:
                    current = words
                store.set_person_tools(rt.conn, target["person_id"], current)
                rt.audit(kind="policy", person_id=target["person_id"], note=f"tools -> {current}")
                return (
                    f"**{identity.label(target)}** tools: {', '.join(current) or 'role default'}"
                )
            if len(store.owners(rt.conn)) <= 1 and _role_of(target) == policy.OWNER:
                return "removing the only owner would lock everybody out"
            store.remove_person(rt.conn, target["person_id"])
            rt.audit(kind="policy", person_id=target["person_id"], note="removed from roster")
            return f"**{identity.label(target)}** is off the roster; their facts are gone too"

        # ---- audit -------------------------------------------------------------
        if verb in {"audit", "log", "whoasked"}:
            hours = 24.0
            target = None
            for part in rest.split():
                try:
                    hours = float(part)
                except ValueError:
                    target = part
            person_id = None
            if target:
                if not _is_owner(person):
                    return "only the owner can read another person's trail"
                matches = store.find_by_name(rt.conn, target)
                if not matches:
                    return f"no person matches {target!r}"
                person_id = matches[0]["person_id"]
            summary = store.audit_summary(rt.conn, person_id=person_id, hours=hours)
            rows = store.audit_rows(rt.conn, person_id=person_id, hours=hours, limit=12)
            head = (
                f"**audit** · last {hours:g}h · "
                f"{summary['turns']} turns, {summary['tool_calls']} tool calls, "
                f"{summary['refusals']} refusals, {summary['approvals']} approvals, "
                f"{summary['tokens']:,} tokens"
            )
            body = []
            for row in rows:
                bit = f"- {row.get('kind')}"
                if row.get("who") or row.get("person_id"):
                    bit += f" · {row.get('person_id')}"
                if row.get("tool"):
                    bit += f" · {row['tool']}"
                if row.get("allowed") is not None:
                    bit += " · allowed" if row["allowed"] else " · refused"
                if row.get("tokens"):
                    bit += f" · {row['tokens']} tok"
                if row.get("note"):
                    bit += f" · {row['note']}"
                body.append(bit)
            pending = store.pending_approvals(rt.conn)
            if pending:
                body.append("")
                body.append("waiting on approval: " + ", ".join(f"`{p['code']}`" for p in pending))
            return "\n".join([head] + body)

        # ---- pause / resume ----------------------------------------------------
        if verb in {"pause", "resume", "off", "on"}:
            if not _is_owner(person):
                return "only the owner can pause enforcement"
            want_paused = verb in {"pause", "off"}
            rt.set_paused(want_paused)
            rt.audit(
                kind="policy",
                person_id=person.get("person_id"),
                note=f"enforcement {'paused' if want_paused else 'resumed'} by owner",
            )
            return (
                "enforcement is **paused** — every tool is allowed and every turn says so in "
                "`/many audit`" if want_paused else "enforcement is **back on**"
            )

        return (
            f"unknown command `{verb}`\n"
            "`/many whoami · roster · audit [name] [hours] · invite [role] · claim [code] · "
            "role <name> <role> · tools <name> [add|remove] <tool…> · remove <name> · pause · resume`"
        )
    except Exception as exc:  # a slash command must never raise into the chat loop
        return f"manyhands: {exc.__class__.__name__}: {exc}"


# --------------------------------------------------------------------------- register


def register(ctx) -> None:
    global _runtime
    runtime = Runtime(ctx)
    _runtime = runtime

    def _handler(fn):
        def handle(args: dict[str, Any], session_id: str = "", **kwargs: Any) -> str:
            return fn(runtime, args if isinstance(args, dict) else {}, session_id)

        return handle

    for name, schema, fn, description, emoji in (
        ("manyhands_whoami", _tools.WHOAMI_SCHEMA, _tools.whoami,
         "Who is this person, and which tools does their role carry?", "🪪"),
        ("manyhands_people", _tools.PEOPLE_SCHEMA, _tools.people,
         "The roster of people this Hermes serves; owner-only roster changes.", "👥"),
        ("manyhands_remember", _tools.REMEMBER_SCHEMA, _tools.remember,
         "Store a fact in the caller's own memory namespace.", "🧠"),
        ("manyhands_recall", _tools.RECALL_SCHEMA, _tools.recall,
         "Search the caller's own facts plus facts shared with everybody.", "🔎"),
        ("manyhands_share", _tools.SHARE_SCHEMA, _tools.share,
         "Publish a fact everyone this Hermes serves can see.", "📣"),
        ("manyhands_audit", _tools.AUDIT_SCHEMA, _tools.audit,
         "Who asked for what: turns, tool calls, refusals, approvals, tokens.", "🧾"),
    ):
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=_handler(fn),
            description=description,
            emoji=emoji,
        )

    ctx.register_hook("pre_llm_call", _hooks.make_pre_llm_call(runtime))
    ctx.register_hook("pre_tool_call", _hooks.make_pre_tool_call(runtime))
    ctx.register_hook("post_api_request", _hooks.make_post_api_request(runtime))

    try:
        ctx.register_approval_transport("manyhands", make_transport(runtime))
    except Exception as exc:  # an already-registered transport must not stop the permission hooks
        logger.warning("manyhands: approval transport not registered: %s", exc)

    def command(raw_args: str = "", source: Any = None, session_id: str = "", **kwargs: Any) -> str:
        return _cmd(runtime, raw_args, source=source, session_id=session_id)

    ctx.register_command(
        "many",
        command,
        description="Who am I, who is on the roster, and what did I just refuse",
        args_hint="<whoami | roster | audit [name] [hours] | invite [role] | claim [code] | role | tools | pause | resume>",
    )

    logger.info(
        "manyhands: 6 tools, pre_llm_call + pre_tool_call + post_api_request hooks, "
        "approval transport `manyhands`, /many"
    )


def _describe() -> str:
    """Small helper for the tests and for `hermes plugins` output."""
    return json.dumps(
        {
            "tools": list(policy.ROLE_TOOLS),
            "roles": list(policy.ROLES),
            "hooks": ["pre_llm_call", "pre_tool_call", "post_api_request"],
        }
    )
