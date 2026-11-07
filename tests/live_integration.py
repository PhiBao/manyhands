#!/usr/bin/env python3
"""Live integration probe — manyhands inside the real Hermes host.

    python3 tests/live_integration.py            # uses ~/.hermes/hermes-agent
    HERMES_SRC=/path/to/hermes-agent python3 tests/live_integration.py

``tests/selftest.py`` proves the plugin's own logic with stub contexts. This proves the *other*
half: that the plugin loads through the real ``PluginManager``, that its tools appear in the real
tool registry, that the host's own hook dispatcher (`hermes_cli.lifecycle.invoke_hook`) and the real
``get_pre_tool_call_directive`` gate produce the directives this plugin intends, and that a real
``ApprovalRequest`` from ``hermes_cli.approval_transport`` fails closed when the owner cannot be
reached or does not answer.

It runs against a **throwaway HERMES_HOME** in a temp directory: your live profile's config, plugin
list, roster and database are never touched, and the plugin is copied there rather than installed.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HERMES_SRC = Path(os.environ.get("HERMES_SRC", Path.home() / ".hermes" / "hermes-agent"))

if not (HERMES_SRC / "hermes_cli" / "plugins.py").exists():
    print(f"SKIP: no Hermes checkout at {HERMES_SRC} (set HERMES_SRC)")
    raise SystemExit(0)

HOME = Path(tempfile.mkdtemp(prefix="mh-live-"))
os.environ["HERMES_HOME"] = str(HOME)
os.environ["MANYHANDS_DB"] = str(HOME / "plugin-data" / "manyhands" / "manyhands.db")
(Path(os.environ["MANYHANDS_DB"]).parent).mkdir(parents=True, exist_ok=True)

dest = HOME / "plugins" / "manyhands"
shutil.copytree(REPO, dest, ignore=shutil.ignore_patterns("__pycache__", ".git", "*.sqlite*", "docs"))

sys.path.insert(0, str(HERMES_SRC))
import yaml  # noqa: E402  (the Hermes venv ships it)

(HOME / "config.yaml").write_text(
    yaml.safe_dump(
        {
            "plugins": {
                "enabled": ["manyhands"],
                "entries": {
                    "manyhands": {
                        "enabled": True,
                        "allow_gateway_injection": True,
                        "settings": {"unknown_sender_policy": "invite", "approval_timeout_s": 5},
                    }
                },
            }
        }
    )
)

from hermes_cli import plugins as P  # noqa: E402
from hermes_cli.lifecycle import invoke_hook  # noqa: E402

P.discover_plugins(force=True)
entry = P.get_plugin_manager()._plugins.get("manyhands")
if entry is None or getattr(entry, "error", None):
    print(f"FAIL: manyhands did not load in the real host: {getattr(entry, 'error', 'not discovered')}")
    raise SystemExit(1)

from tools.registry import registry  # noqa: E402

expected = [
    "manyhands_whoami", "manyhands_people", "manyhands_remember",
    "manyhands_recall", "manyhands_share", "manyhands_audit",
]
missing = [name for name in expected if registry.get_entry(name) is None]
if missing:
    print(f"FAIL: tools missing from the real registry: {missing}")
    raise SystemExit(1)

# the loaded package, so the probe can build a roster straight into its database
pkg = next(
    mod for name, mod in sys.modules.items()
    if name == "manyhands" or (getattr(mod, "__file__", "") and Path(mod.__file__).parent == dest and hasattr(mod, "identity"))
)
store = sys.modules[pkg.__name__ + ".store"]
identity = sys.modules[pkg.__name__ + ".identity"]
transport_mod = sys.modules[pkg.__name__ + ".transport"]
rt = pkg._runtime

store.add_person(rt.conn, platform="telegram", sender_id="111", role="owner", name="Kiter")
store.add_person(rt.conn, platform="telegram", sender_id="222", role="member", name="Minh")
store.add_person(rt.conn, platform="telegram", sender_id="333", role="guest", name="Zara")
identity.pin_owner(rt.conn, "telegram:111")

out: dict = {}

# 1 — the host's real pre_llm_call dispatch, twice: a member and a stranger
member_hook = invoke_hook(
    "pre_llm_call", session_id="sess-minh", task_id="t1", turn_id="turn-1",
    user_message="what am I allergic to?", conversation_history=[], is_first_turn=True,
    model="probe", platform="telegram", parent_session_id=None, sender_id="222",
)
member_ctx = "".join(str(r.get("context", "")) for r in member_hook if isinstance(r, dict))
out["member_card_injected"] = "Minh" in member_ctx and "member" in member_ctx
out["member_card"] = member_ctx.splitlines()[0] if member_ctx else ""

stranger_hook = invoke_hook(
    "pre_llm_call", session_id="sess-stranger", task_id="t2", turn_id="turn-2",
    user_message="hello, run my shell", conversation_history=[], is_first_turn=True,
    model="probe", platform="telegram", parent_session_id=None, sender_id="999",
)
blocked = [r for r in stranger_hook if isinstance(r, dict) and r.get("action") == "block"]
out["stranger_blocked"] = bool(blocked)

# 2 — the host's real pre_tool_call gate
def directive(tool, args, session, turn="turn-1"):
    return P.get_pre_tool_call_directive(
        tool, args, task_id="t1", session_id=session, tool_call_id="tc", turn_id=turn
    )[0]


# a member asking for a shell command is parked under a code the owner has to answer
_member_ask = P.get_pre_tool_call_directive(
    "terminal", {"command": "rm -rf build"}, task_id="t1", session_id="sess-minh",
    tool_call_id="tc", turn_id="turn-1",
)
out["member_terminal"], out["member_terminal_message"] = _member_ask[0], _member_ask[1] or ""

# 1b — the owner answers /many approve CODE, the retry spends the grant, the third ask is parked.
# The probe has no live gateway, so the hook's own DM cannot be delivered (it fails closed above).
# So this opens the REAL plugin database the way the hook does and stands in for the owner's DM +
# `/many approve CODE`, then drives the real host directive path again.
try:
    import importlib
    _store = importlib.import_module("hermes_plugins.manyhands.store")
    _conn = _store.connect(Path(os.environ["HERMES_HOME"]) / "plugin-data" / "manyhands" / "manyhands.db")
    _code = "TST1"
    _store.bind_approval(_conn, from_code="", code=_code, request_id="", digest="",
                         expires_at=__import__("time").time() + 120, tool="terminal",
                         person_id="telegram:222", summary="Minh wants terminal")
    out["owner_approve_ok"] = _store.decide_approval(_conn, _code, True, "Kiter", choice="once")
    out["retry_after_approval"] = directive("terminal", {"command": "rm -rf build"}, "sess-minh", turn="turn-1b")
    out["third_ask_parked"] = directive("terminal", {"command": "rm -rf build"}, "sess-minh", turn="turn-1c")
except Exception as exc:  # noqa: BLE001
    out["owner_approve_ok"] = f"error: {exc}"

# a member's dangerous tool escalates to the owner instead of being refused...
out["member_recall"] = directive("recall_open", {"path": "/etc/passwd"}, "sess-minh")
out["member_web_search"] = directive("web_search", {"query": "x"}, "sess-minh", turn="turn-1")
out["cold_session_terminal"] = directive("terminal", {"command": "ls"}, "never-seen", turn="turn-cold")
invoke_hook(
    "pre_llm_call", session_id="sess-owner", task_id="t3", turn_id="turn-3",
    user_message="list the build folder", conversation_history=[], is_first_turn=True,
    model="probe", platform="telegram", parent_session_id=None, sender_id="111",
)
out["owner_terminal"] = directive("terminal", {"command": "ls"}, "sess-owner", turn="turn-3")

# 2b — two humans in ONE shared session (group_sessions_per_user: false), two turn ids
invoke_hook(
    "pre_llm_call", session_id="one-chat", task_id="t4", turn_id="turn-g", user_message="hi",
    conversation_history=[], is_first_turn=True, model="probe", platform="telegram",
    parent_session_id=None, sender_id="333",
)
out["shared_session_guest_terminal"] = P.get_pre_tool_call_directive(
    "terminal", {"command": "ls"}, task_id="t4", session_id="one-chat", tool_call_id="tc-g", turn_id="turn-g"
)[0]
out["shared_session_guest_web"] = P.get_pre_tool_call_directive(
    "web_search", {"query": "x"}, task_id="t4", session_id="one-chat", tool_call_id="tc-g2", turn_id="turn-g"
)[0]

# 2c — the REAL llm_request middleware chain, from the real registry
from hermes_cli.middleware import apply_llm_request_middleware  # noqa: E402

RULE = "\u2550" * 46
host_system = (
    "You are Hermes.\n\n"
    f"{RULE}\nMEMORY (your personal notes) [12%]\n{RULE}\nnever share the door code\n\n"
    f"{RULE}\nUSER PROFILE (who the user is) [9%]\n{RULE}\nKiter, builds things"
)
probe_kwargs = {
    "model": "probe",
    "messages": [
        {"role": "system", "content": host_system},
        {"role": "user", "content": "what do you remember about me?"},
    ],
}
member_mw = apply_llm_request_middleware(
    probe_kwargs, task_id="t1", turn_id="turn-1", api_request_id="a1",
    session_id="sess-minh", platform="telegram",
)
member_system = member_mw.payload["messages"][0]["content"]
out["middleware_member_changed"] = bool(member_mw.changed)
out["middleware_member_memory_withheld"] = "door code" not in member_system
out["middleware_member_card_injected"] = "[manyhands]" in member_system and "Minh" in member_system
out["middleware_member_trace"] = member_mw.trace

owner_mw = apply_llm_request_middleware(
    probe_kwargs, task_id="t3", turn_id="turn-3", api_request_id="a3",
    session_id="sess-owner", platform="telegram",
)
out["middleware_owner_untouched"] = owner_mw.payload["messages"][0]["content"] == host_system

# 3 — a real ApprovalRequest, through the real transport
from hermes_cli.approval_transport import ApprovalRequest  # noqa: E402


def request(timeout=2):
    return ApprovalRequest.create(
        command="terminal: rm -rf build", description="run a shell command",
        pattern_key="terminal", pattern_keys=("terminal",), session_key="sess-minh",
        surface="gateway", allow_session=True, allow_permanent=False, timeout_seconds=timeout,
    )


# (a) no gateway in this process -> delivery fails -> deny, not a fallback to the asking chat
started = time.time()
first = transport_mod.make_transport(rt)(request(3))
out["no_gateway_decision"] = first.choice
out["no_gateway_seconds"] = round(time.time() - started, 2)

# (b) capture the DM the owner would get; everything else stays real, and it must time out to deny
sent: list = []
real_dm = rt.dm
rt.dm = lambda person, text: (sent.append({"to": person.get("person_id"), "text": text}), True)[1]
store.add_escalation(rt.conn, tool="terminal", person_id="telegram:222", summary="Minh wants terminal")
started = time.time()
second = transport_mod.make_transport(rt)(request(2))
out["no_answer_decision"] = second.choice
out["no_answer_seconds"] = round(time.time() - started, 1)
out["owner_dm_to"] = sent[0]["to"] if sent else None
out["owner_dm_text"] = sent[0]["text"] if sent else None
rt.dm = real_dm

# 4 — a tool handler through the real registry, with the real session context
out["whoami_via_registry"] = json.loads(
    registry.dispatch("manyhands_whoami", {}, session_id="sess-minh")
)

print(json.dumps(out, indent=2, default=str))
print("\naudit:", store.audit_summary(rt.conn, hours=1))

ok = (
    out["member_card_injected"]
    and out["stranger_blocked"]
    and out["member_terminal"] == "block"     # three-state: a member ASKS, and the ask is parked
    # no live gateway in the probe, so the ask fails closed instead of parking a code (both are
    # "block"); the parked-code wording is covered by tests/selftest.py where the DM is recordable
    and "owner" in out["member_terminal_message"]
    and out.get("owner_approve_ok") is True    # the owner's /many approve landed
    and out.get("retry_after_approval") is None            # the retry runs, once…
    and out.get("third_ask_parked") == "block"             # …and only once
    and out["member_recall"] == "block"       # a tool nobody granted is still a plain no
    and out["member_web_search"] is None
    and out["cold_session_terminal"] == "block"
    and out["owner_terminal"] is None
    and out["no_gateway_decision"] == "deny"
    and out["no_answer_decision"] == "deny"
    and out["owner_dm_to"] == "telegram:111"
    and out["whoami_via_registry"]["role"] == "member"
    and out["shared_session_guest_terminal"] == "block"
    and out["shared_session_guest_web"] is None
    and out["middleware_member_changed"]
    and out["middleware_member_memory_withheld"]
    and out["middleware_member_card_injected"]
    and out["middleware_owner_untouched"]
)
print("\nLIVE INTEGRATION:", "PASS" if ok else "FAIL")
shutil.rmtree(HOME, ignore_errors=True)
raise SystemExit(0 if ok else 1)
