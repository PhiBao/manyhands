#!/usr/bin/env python3
"""Reproduce the tool-identity finding against any checkout.

    python3 tests/probe_tool_identity.py                    # this tree
    python3 tests/probe_tool_identity.py /path/to/manyhands # any sha

`pre_tool_call` gets `turn_id`, so the policy gate is always attributed correctly. A **tool handler**
does not: `tools/registry.py` dispatches with `task_id` + `session_id` only (verified at
`model_tools.py:_execute_tool`), so `tools._caller` falls back to the session map — one entry per
session, written by the last person whose turn started there. Two people in one shared session
(`group_sessions_per_user: false`) whose turns interleave therefore get the other's identity on every
tool call: the owner's facts answered to a member, a member's note written into the owner's namespace,
`manyhands_share` open to a guest who happened to speak before the owner.

The host does hand `task_id` to the handler (the plugin's `**kwargs` already receives it and drops it),
and `task_id` is the per-turn discriminator. This probe drives the two interleaved turns and then makes
the first speaker's tool call, with the same keyword arguments the host passes.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def _load(plugin_dir: Path):
    stage = Path(tempfile.mkdtemp(prefix="mh-probe-"))
    os.symlink(plugin_dir, stage / "manyhands")
    if "plugins" not in sys.modules:
        pkg = types.ModuleType("plugins")
        pkg.__path__ = []
        sys.modules["plugins"] = pkg
    if "plugins.plugin_storage" not in sys.modules:
        storage = types.ModuleType("plugins.plugin_storage")
        storage.plugin_data_dir = lambda name: stage / "data" / name
        sys.modules["plugins.plugin_storage"] = storage
    sys.path.insert(0, str(stage))
    return importlib.import_module("manyhands")


class _Ctx:
    plugin_id = "manyhands"

    def get_config(self, key, default=None):
        return default

    def inject_message(self, *a, **k):
        return False


def _call(handler, rt, args, session, task):
    """Call a tool handler the way the host does: args, then the kwargs it declares."""
    try:
        return handler(rt, args, session_id=session, task_id=task)
    except TypeError:
        # an older build whose signature has no task_id: the host's kwargs are dropped and this is
        # exactly the session-map fallback being demonstrated
        return handler(rt, args, session_id=session)


def main(argv: list[str]) -> int:
    plugin_dir = Path(argv[1]).resolve() if len(argv) > 1 else HERE
    mh = _load(plugin_dir)
    store, identity, tools, hooks = mh.store, mh.identity, mh.tools, mh.hooks

    rt = mh.runtime.Runtime(_Ctx(), db_path=Path(tempfile.mkdtemp(prefix="mh-probe-db-")) / "p.sqlite")
    store.add_person(rt.conn, platform="telegram", sender_id="111", role="owner", name="Kiter")
    store.add_person(rt.conn, platform="telegram", sender_id="222", role="member", name="Minh")
    store.add_person(rt.conn, platform="telegram", sender_id="333", role="guest", name="Zara")
    identity.pin_owner(rt.conn, "telegram:111")
    print(f"plugin: {plugin_dir}")

    turn = hooks.make_pre_llm_call(rt)
    shared = "household-chat"  # group_sessions_per_user: false

    # the owner's own private note, stored on the operator's turn
    turn(user_message="remember my passport expires in March", session_id="owner-dm",
         turn_id="t-own", task_id="task-own", platform="telegram", sender_id="111")
    _call(tools.remember, rt, {"text": "passport expires in March", "topic": "personal"},
          "owner-dm", "task-own")
    print("  premise: the owner's note is in telegram:111's namespace")

    # two people, one session, turns interleaved: Minh's turn starts, then Kiter's, then Minh's
    # turn continues and its tool call arrives
    turn(user_message="what do you know about me?", session_id=shared,
         turn_id="t-minh", task_id="task-minh", platform="telegram", sender_id="222")
    turn(user_message="and my passport?", session_id=shared,
         turn_id="t-kiter", task_id="task-kiter", platform="telegram", sender_id="111")

    who = json.loads(_call(tools.whoami, rt, {}, shared, "task-minh"))
    print("\nMinh's tool call, made inside Minh's own turn:")
    print(f"  whoami -> {who.get('role')} ({who.get('person')})")
    got = json.loads(_call(tools.recall, rt, {"query": "passport"}, shared, "task-minh"))
    texts = [r["text"] for r in got.get("results") or []]
    print(f"  recall 'passport' -> {texts or 'nothing'}")

    wrong = who.get("person_id") != "telegram:222"
    leaked = any("passport" in t for t in texts)
    print(
        f"  verdict: {'WRONG SPEAKER — the call acted as ' + str(who.get('person_id')) if wrong else 'ATTRIBUTED — the call acted as telegram:222'}"
    )
    print(
        f"  verdict: {'LEAKED — the owner’s own note was answered to a member' if leaked else 'PRIVATE — nothing of the owner’s reached a member'}"
    )
    rt.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
