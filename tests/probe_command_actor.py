#!/usr/bin/env python3
"""Reproduce the two `/many` findings from the 5976905 review against any checkout.

    python3 tests/probe_command_actor.py                    # this tree
    python3 tests/probe_command_actor.py /path/to/manyhands # the sha the reviewer read

The probe stages the plugin directory inside a temp parent so it imports as a package whatever the
directory is called, drives `/many` through the same handler the gateway calls, and prints one
verdict per finding: ESCALATED/LEAKED is the bug, BLOCKED/PRIVATE is the fix. Stdlib only, no
Hermes install required.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def _load(plugin_dir: Path):
    """Import *plugin_dir* as a package named ``manyhands`` (whatever the folder is called)."""
    stage = Path(tempfile.mkdtemp(prefix="mh-probe-"))
    link = stage / "manyhands"
    os.symlink(plugin_dir, link)
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
    """The slice of PluginContext the Runtime reads settings from."""

    plugin_id = "manyhands"

    def get_config(self, key, default=None):
        return default

    def inject_message(self, *a, **k):  # the probe never needs a real DM
        return False


def _source(uid, chat):
    return types.SimpleNamespace(
        platform=types.SimpleNamespace(value="telegram"),
        user_id=uid,
        chat_id=chat,
        chat_type="group",
    )


def main(argv: list[str]) -> int:
    plugin_dir = Path(argv[1]).resolve() if len(argv) > 1 else HERE
    mh = _load(plugin_dir)
    store, identity = mh.store, mh.identity

    rt = mh.runtime.Runtime(_Ctx(), db_path=Path(tempfile.mkdtemp(prefix="mh-probe-db-")) / "p.sqlite")
    store.add_person(rt.conn, platform="telegram", sender_id="111", role="owner", name="Kiter")
    store.add_person(rt.conn, platform="telegram", sender_id="222", role="member", name="Minh")
    identity.pin_owner(rt.conn, store.person_id_for("telegram", "111"))

    shared = "household-chat"
    turn = mh.hooks.make_pre_llm_call(rt)
    turn(user_message="hi", session_id=shared, turn_id="t-owner", platform="telegram", sender_id="111")
    person, _how = rt.person_for_session(shared)
    print(f"plugin: {plugin_dir}")
    print(f"premise: the shared session's last speaker is {person['person_id']} (the owner)")

    code = "AB12"
    store.open_approval(
        rt.conn,
        code=code,
        person_id="telegram:222",
        tool="terminal",
        summary='Minh wants terminal {"command": "cat ~/.ssh/id_rsa"}',
        expires_at=time.time() + 300,
    )
    stranger = _source("999", shared)  # the gateway let them in; the roster never saw them

    print("\nfinding 1 — an unrostered sender in the owner's own session:")
    approve = mh._cmd(rt, f"approve {code}", source=stranger, session_id=shared)
    pause = mh._cmd(rt, "pause", source=stranger, session_id=shared)
    invite = mh._cmd(rt, "invite owner", source=stranger, session_id=shared)
    for name, reply in (("approve", approve), ("pause", pause), ("invite owner", invite)):
        print(f"  /many {name:<12} -> {reply.splitlines()[0][:90]}")
    took_owner_seat = (
        "approved — the tool call is on its way" in approve
        or "enforcement is **paused**" in pause
        or "invite code: `" in invite
    )
    print(f"  verdict: {'ESCALATED — the stranger acted as the owner' if took_owner_seat else 'BLOCKED — every owner verb refused'}")

    print("\nfinding 2 — a bare /many approve while somebody else has an ask parked:")
    # a fresh ask, so finding 1 cannot have consumed it
    store.open_approval(
        rt.conn,
        code="EF56",
        person_id="telegram:222",
        tool="terminal",
        summary='Minh wants terminal {"command": "cat ~/.aws/credentials"}',
        expires_at=time.time() + 300,
    )
    listing = mh._cmd(rt, "approve", source=_source("333", shared), session_id=shared)
    print("  -> " + listing.replace("\n", " ⏎ ")[:150])
    leaked = "EF56" in listing or "credentials" in listing
    print(f"  verdict: {'LEAKED — a third party reads the code and the asker’s arguments' if leaked else 'PRIVATE — nobody else’s code or arguments are shown'}")

    rt.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
