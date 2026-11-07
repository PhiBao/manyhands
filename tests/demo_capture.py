#!/usr/bin/env python3
"""manyhands — capture a real transcript for the demo video.

    ~/.hermes/hermes-agent/venv/bin/python tests/demo_capture.py

Everything printed here comes from the plugin running inside the **real** Hermes host: the plugin is
loaded by the real `PluginManager` into a throwaway `HERMES_HOME`, and every line is the return value
of the host's own dispatch (`lifecycle.invoke_hook`, `get_pre_tool_call_directive`,
`apply_llm_request_middleware`, `tools.registry.dispatch`) or of the real approval transport against a
real `ApprovalRequest`.

The three people are synthetic (Kiter the owner, Minh a member, Zara a guest) — no real account is
involved, and no message is sent anywhere. The demo video replays this file verbatim.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HERMES_SRC = Path(os.environ.get("HERMES_SRC", Path.home() / ".hermes" / "hermes-agent"))
if not (HERMES_SRC / "hermes_cli" / "plugins.py").exists():
    print(f"SKIP: no Hermes checkout at {HERMES_SRC} (set HERMES_SRC)")
    raise SystemExit(0)

HOME = Path(tempfile.mkdtemp(prefix="mh-demo-"))
os.environ["HERMES_HOME"] = str(HOME)
os.environ["MANYHANDS_DB"] = str(HOME / "plugin-data" / "manyhands" / "manyhands.db")
Path(os.environ["MANYHANDS_DB"]).parent.mkdir(parents=True, exist_ok=True)

dest = HOME / "plugins" / "manyhands"
shutil.copytree(REPO, dest, ignore=shutil.ignore_patterns("__pycache__", ".git", "*.sqlite*", "docs"))

sys.path.insert(0, str(HERMES_SRC))
import yaml  # noqa: E402

(HOME / "config.yaml").write_text(
    yaml.safe_dump(
        {
            "plugins": {
                "enabled": ["manyhands"],
                "entries": {
                    "manyhands": {
                        "enabled": True,
                        "allow_gateway_injection": True,
                        "settings": {
                            "unknown_sender_policy": "invite",
                            "approval_timeout_s": 6,
                            "scope_host_memory": True,
                        },
                    }
                },
            }
        }
    )
)

from hermes_cli import plugins as P  # noqa: E402
from hermes_cli.lifecycle import invoke_hook  # noqa: E402
from hermes_cli.middleware import apply_llm_request_middleware  # noqa: E402

P.discover_plugins(force=True)
entry = P.get_plugin_manager()._plugins.get("manyhands")
if entry is None or getattr(entry, "error", None):
    print(f"FAIL: manyhands did not load: {getattr(entry, 'error', 'not discovered')}")
    raise SystemExit(1)

from tools.registry import registry  # noqa: E402

pkg = next(
    mod for name, mod in sys.modules.items()
    if name == "manyhands" or (getattr(mod, "__file__", "") and Path(mod.__file__).parent == dest
                              and hasattr(mod, "identity"))
)
store = sys.modules[pkg.__name__ + ".store"]
identity = sys.modules[pkg.__name__ + ".identity"]
transport = sys.modules[pkg.__name__ + ".transport"]
rt = pkg._runtime

# --------------------------------------------------------------------------- the household
store.add_person(rt.conn, platform="telegram", sender_id="111", role="owner", name="Kiter")
store.add_person(rt.conn, platform="telegram", sender_id="222", role="member", name="Minh")
store.add_person(rt.conn, platform="telegram", sender_id="333", role="guest", name="Zara")
identity.pin_owner(rt.conn, "telegram:111")


def out(text: str = "") -> None:
    print(text, flush=True)


def turn(sender: str, session: str, turn_id: str, message: str):
    return invoke_hook(
        "pre_llm_call", session_id=session, task_id="demo", turn_id=turn_id, user_message=message,
        conversation_history=[], is_first_turn=True, model="demo", platform="telegram",
        parent_session_id=None, sender_id=sender,
    )


def context_of(hook_results) -> str:
    return "".join(str(r.get("context", "")) for r in hook_results if isinstance(r, dict))


def remember(sender: str, session: str, turn_id: str, text: str, topic: str) -> dict:
    turn(sender, session, turn_id, "remember this")
    return json.loads(registry.dispatch(
        "manyhands_remember", {"text": text, "topic": topic},
        session_id=session, turn_id=turn_id,
    ))


out("=" * 78)
out("  manyhands - one Hermes, many people")
out("  every line below is the real plugin, driven through the real Hermes host")
out("=" * 78)
out()

# --------------------------------------------------------------------------- 1. the roster
remember("222", "minh", "t-m1", "allergic to peanuts", "health")
remember("222", "minh", "t-m2", "ships the payroll run on the 25th", "work")
remember("333", "zara", "t-z1", "vegetarian, no dairy", "food")
# --------------------------------------------------------------------------- 0. a stranger
out("$ a stranger who is not on the roster: 'run my shell, delete the build folder'")
stranger = turn("999", "stranger", "t-s1", "run my shell, delete the build folder")
for result in stranger:
    if isinstance(result, dict) and result.get("action") == "block":
        out("  the turn is refused before any model call:")
        for line in str(result.get("message", "")).splitlines():
            out("  " + line)
out()

# --------------------------------------------------------------------------- 1. the roster
out("$ /many roster          (Kiter, the owner, in the family chat)")
turn("111", "owner-dm", "t-owner", "who is on the roster?")
out(pkg._cmd(rt, "roster", source=type("S", (), {
    "platform": type("P", (), {"value": "telegram"})(), "user_id": "111",
    "chat_id": "111", "chat_type": "dm"})(), session_id="owner-dm"))
out()

# --------------------------------------------------------------------------- 2. per-person memory
out("$ Minh (member) asks: 'what am I allergic to?'")
minh_ctx = context_of(turn("222", "minh", "t-m3", "what am I allergic to?"))
for line in minh_ctx.splitlines():
    out("  " + line)
out()
out("$ Zara (guest) asks the same question")
zara_ctx = context_of(turn("333", "zara", "t-z3", "what am I allergic to?"))
for line in zara_ctx.splitlines():
    out("  " + line)
out()
out(f"  -> Minh's context carries his facts; Zara's carries hers. peanuts in Minh's: "
    f"{'peanuts' in minh_ctx} / in Zara's: {'peanuts' in zara_ctx}")
out()

# --------------------------------------------------------------------------- 3. the system prompt
RULE = "\u2550" * 46
host_system = (
    "You are Hermes.\n\n"
    f"{RULE}\nMEMORY (your personal notes) [41%]\n{RULE}\n"
    "the owner's door code is 4471\n§\nKiter is mid-move, invoices pile up in March\n\n"
    f"{RULE}\nUSER PROFILE (who the user is) [23%]\n{RULE}\nKiter, builds on Celo, hates being called sir"
)
probe = {"model": "demo", "messages": [{"role": "system", "content": host_system},
                                       {"role": "user", "content": "what do you remember about me?"}]}
zara_mw = apply_llm_request_middleware(probe, task_id="demo", turn_id="t-z3", api_request_id="a1",
                                       session_id="zara", platform="telegram")
zara_system = zara_mw.payload["messages"][0]["content"]
out("$ the system prompt Zara's turn is actually sent")
host_block_count = host_system.count("MEMORY (your personal notes)") + host_system.count("USER PROFILE")
out(f"  host memory blocks in the operator's prompt : {host_block_count}")
out(f"  host memory blocks in Zara's prompt         : "
    f"{zara_system.count('MEMORY (your personal notes)') + zara_system.count('USER PROFILE')}")
out(f"  'door code' reaches Zara                    : {'door code' in zara_system}")
out(f"  person card injected instead                : {zara_system.count('[manyhands]')} block(s)")
out("  " + [ln for ln in zara_system.splitlines() if ln.startswith("[manyhands] This conversation")][0])
out()

# --------------------------------------------------------------------------- 4. a refusal
turn("333", "zara", "t-z4", "read the lease in Kiter's documents")
verdict, message = P.get_pre_tool_call_directive(
    "recall_open", {"path": "/home/kiter/Documents/lease.pdf"},
    task_id="demo", session_id="zara", tool_call_id="tc1", turn_id="t-z4",
)
out("$ Zara: 'read the lease in Kiter's documents'   ->  recall_open")
out(f"  veto: {verdict}")
out(f"  {message}")
out()

# --------------------------------------------------------------------------- 5. the escalation
turn("222", "minh", "t-m5", "run the payroll export")
out("$ Minh (member): 'run the payroll export'   ->  terminal")
escalate, esc_message = P.get_pre_tool_call_directive(
    "terminal", {"command": "python payroll_export.py --month 11"},
    task_id="demo", session_id="minh", tool_call_id="tc2", turn_id="t-m5",
)
out(f"  directive: {escalate}   (allowed to ask the owner, not to run it)")
out(f"  {esc_message}")

sent: list = []
real_dm = rt.dm
rt.dm = lambda person, text: (sent.append({"to": person.get("person_id"), "text": text}), True)[1]
from hermes_cli.approval_transport import ApprovalRequest  # noqa: E402

store.add_escalation(rt.conn, tool="terminal", person_id="telegram:222",
                     summary="Minh wants terminal {\"command\": \"python payroll_export.py --month 11\"}")
request = ApprovalRequest.create(
    command="terminal: python payroll_export.py --month 11", description="run a shell command",
    pattern_key="terminal", pattern_keys=("terminal",), session_key="minh", surface="gateway",
    allow_session=True, allow_permanent=False, timeout_seconds=6,
)


def owner_answers() -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        pending = store.pending_approvals(rt.conn)
        if pending:
            out()
            out(f"$ Kiter's DM   ->  /many approve {pending[0]['code']}")
            store.decide_approval(rt.conn, pending[0]["code"], True, "Kiter")
            return
        time.sleep(0.05)


thread = threading.Thread(target=owner_answers)
thread.start()
decision = transport.make_transport(rt)(request)
thread.join()
out()
out("$ the prompt that reached the owner's DM, and nobody else")
for line in (sent[0]["text"] if sent else "").splitlines():
    out("  " + line)
out(f"  delivered to: {sent[0]['to'] if sent else None}")
out(f"  owner's answer -> host decision: {decision.choice}")
rt.dm = real_dm
out()

# --------------------------------------------------------------------------- 6. the audit
rows = store.audit_summary(rt.conn, hours=1)
out("$ /many audit")
out(pkg._cmd(rt, "audit", source=type("S", (), {
    "platform": type("P", (), {"value": "telegram"})(), "user_id": "111",
    "chat_id": "111", "chat_type": "dm"})(), session_id="owner-dm"))
out()
out(f"summary: {rows['turns']} turns, {rows['tool_calls']} tool decisions, "
    f"{rows['refusals']} refusals, {rows['approvals']} approvals")
out(f"db: {Path(os.environ['MANYHANDS_DB']).stat().st_size / 1024:.0f} KB in "
    f"{Path(os.environ['MANYHANDS_DB']).parent}")
shutil.rmtree(HOME, ignore_errors=True)
