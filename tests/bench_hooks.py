#!/usr/bin/env python3
"""manyhands — measure what the hooks cost per turn, because they run on every turn.

    python3 tests/bench_hooks.py            # 200 iterations, prints p50/p95/max in ms

Both hooks are on the hot path of every session, so "fast enough" is not a claim, it is a number.
This drives the real hook bodies against a real SQLite file with a roster and a populated per-person
memory, on this machine, and reports the distribution.

The interesting number is ``pre_llm_call``: it does the identity resolve, the last-seen update, the
audit insert, the fact/shared reads and the context build, and it is the one every turn pays.
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))

_TMP = tempfile.mkdtemp(prefix="manyhands-bench-")
if "plugins" not in sys.modules:
    pkg = types.ModuleType("plugins")
    pkg.__path__ = []
    sys.modules["plugins"] = pkg
storage = types.ModuleType("plugins.plugin_storage")
storage.plugin_data_dir = lambda name: Path(_TMP) / name
sys.modules["plugins.plugin_storage"] = storage

import importlib  # noqa: E402

manyhands = importlib.import_module(ROOT.name)
store = manyhands.store
identity = manyhands.identity
hooks = manyhands.hooks
runtime_mod = manyhands.runtime


class Ctx:
    plugin_id = "manyhands"

    def get_config(self, key, default=None):
        return default

    def inject_message(self, content, role="assistant", origin=None):
        return True


def build(people: int = 8, facts_per_person: int = 12, shared: int = 4):
    rt = runtime_mod.Runtime(Ctx(), db_path=Path(_TMP) / "bench.sqlite")
    for i in range(people):
        role = "owner" if i == 0 else ("member" if i < people - 1 else "guest")
        store.add_person(rt.conn, platform="telegram", sender_id=str(1000 + i), role=role, name=f"P{i}")
        for f in range(facts_per_person):
            store.add_fact(rt.conn, store.person_id_for("telegram", str(1000 + i)), f"fact {f} for P{i}")
    for s in range(shared):
        store.add_shared(rt.conn, f"shared fact {s}", added_by="owner")
    identity.pin_owner(rt.conn, "telegram:1000")
    return rt


def timeit(fn, iterations: int) -> dict:
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    samples.sort()
    return {
        "n": len(samples),
        "p50": round(statistics.median(samples), 2),
        "p95": round(samples[int(len(samples) * 0.95) - 1], 2),
        "max": round(samples[-1], 2),
        "mean": round(statistics.fmean(samples), 2),
    }


def main() -> int:
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    rt = build()
    counts = store.counts(rt.conn)
    print(f"fixture: {counts['people']} people, {counts['facts']} facts, {counts['shared']} shared")

    pre_llm = hooks.make_pre_llm_call(rt)
    pre_tool = hooks.make_pre_tool_call(rt)
    post_api = hooks.make_post_api_request(rt)

    counter = {"n": 0}

    def turn():
        counter["n"] += 1
        n = counter["n"]
        pre_llm(
            user_message="what am I allergic to?",
            session_id=f"sess-{n % 5}",
            turn_id=f"turn-{n}",
            platform="telegram",
            sender_id="1001",
        )

    def tool():
        counter["n"] += 1
        pre_tool(
            tool_name="web_search",
            args={"query": "x"},
            session_id=f"sess-{counter['n'] % 5}",
            turn_id=f"turn-{counter['n']}",
        )

    def tokens():
        post_api(usage={"total_tokens": 1200}, session_id="sess-0", turn_id="turn-0", model="m")

    turn()  # warm the roster lookup + sqlite page cache
    tool()

    results = {
        "pre_llm_call (identity + policy + audit + context)": timeit(turn, iterations),
        "pre_tool_call (turn map + allowlist + audit)": timeit(tool, iterations),
        "post_api_request (token attribution)": timeit(tokens, iterations),
    }
    print(f"\niterations: {iterations} per hook, milliseconds\n")
    for label, stats in results.items():
        print(f"{label:52s} p50 {stats['p50']:6.2f}   p95 {stats['p95']:6.2f}   max {stats['max']:7.2f}")
    total = results["pre_llm_call (identity + policy + audit + context)"]["p50"] + results[
        "pre_tool_call (turn map + allowlist + audit)"
    ]["p50"]
    print(f"\na turn that calls one tool pays roughly {total:.2f} ms of manyhands overhead (p50).")
    print(f"db: {rt.db_path} ({rt.db_path.stat().st_size / 1024:.0f} KB)")
    rt.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
