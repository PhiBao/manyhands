# Changelog

## 0.5.1 — the tool surface gets the rule the command surface already had

Found by applying 0.4.0's question to the six tools instead of just `/many`.

- **`manyhands_audit` with no person named read the whole household's trail** to any caller — tool
  names, token counts, refusal notes — and listed every pending approval code with its asker. That is
  the same disclosure 0.4.0 closed on `/many audit` and a bare `/many approve`. A member now gets
  their own trail (`subject: "you"`) and only their own parked asks; the owner and the host's own seat
  still read the house.
- 64 tests (+1, which fails against 0.5.0 with `'everyone' != 'you'`).

## 0.5.0 — a tool call belongs to whoever asked for it

Found while re-reading the tool path after the 0.4.0 command fix, on `30fa4f0`. The gate was right;
the **tool handlers** were not.

- **A tool handler is dispatched with `task_id`, never `turn_id`.** `model_tools._execute_tool` builds
  `dispatch_kwargs = {"task_id": ids.task_id, "session_id": ids.session_id}` and `tools/registry.py`
  narrows it to the handler's own signature, so `tools._caller` fell back to the session map — the
  last person whose turn started in that session. Two people in one shared session
  (`group_sessions_per_user: false`) whose turns interleave therefore got each other's identity on
  every tool call: a member's own `manyhands_recall` answered with the owner's private note, and
  `manyhands_share` sat open to a guest who happened to speak before the owner.
  `Runtime.remember` now records `task_id -> person_id` alongside the turn map (both bounded), and
  `person_for_session` resolves turn → task → session.
- **The session fallback is now conditional.** It is trusted only when exactly one person has ever
  spoken in the session (`store.session_person_count`, backed by a new `session_people` table, created
  with `IF NOT EXISTS` so an existing database upgrades in place). In a shared session an
  unattributable call is an unidentified guest and the refusal says "a shared session with more than
  one speaker" instead of blaming the asker's role.
- **The `llm_request` middleware withholds instead of failing open** on an unattributable request in a
  shared session — the one case where guessing which speaker a prompt belongs to hands somebody the
  operator's notes. Unknown sessions are still left alone.
- 63 tests (+4), `tests/probe_tool_identity.py` reproduces both halves against any checkout
  (`WRONG SPEAKER`/`LEAKED` at 0.4.0, `ATTRIBUTED`/`PRIVATE` here), and `tests/live_integration.py`
  now proves it through the real `tools.registry.dispatch`: in a session whose last speaker is the
  guest, the dispatch carrying `task_id` resolves to the real caller and the one without fails closed.

## 0.4.0 — a command belongs to whoever typed it

Found in the second catalog review, on `5976905`. The turn path was already correct; the **slash
command** path was not, and it is the one that can hand out approvals.

- **`/many` resolves identity from the sender the platform names.** `_actor` fell back to the
  session map — "whoever spoke last in this session" — when the sender had no roster row. In a shared
  session (`group_sessions_per_user: false`, the household setup) that is frequently the owner, so an
  unrostered sender the gateway let in could `/many approve CODE` (answering parked asks),
  `/many pause` enforcement and `/many invite owner`. A named sender now resolves roster row →
  attested local seat → **guest**; the session map answers only for a source that carries no sender.
- **The operator's own terminal works in `/many` too.** The same function never applied
  `identity.is_local_seat`, so `platform="cli"` + this machine's `uid:<uid>` was treated as an
  unknown guest by every command. It now matches `pre_llm_call`, including `local_seat_policy: roster`.
- **A bare `/many approve` is a listing, and listings are gated.** It printed every pending code plus
  each asker's tool arguments to whoever typed it. The owner reads the queue; anybody else sees only
  the asks that are theirs (code + tool), and nothing at all when nothing of theirs is waiting.
- **The same question asked of the neighbours.** `/many audit` with no name read the whole
  household's trail; `/many roster` printed every platform id; an unrostered sender got both. Now:
  owners read the house, members read themselves, and a stranger is told they are not on the roster.
  This matches the rule the `manyhands_people` tool already applied.
- 59 tests (+4), plus `tests/probe_command_actor.py`, which reproduces both findings against any
  checkout (`ESCALATED`/`LEAKED` at `5976905b`, `BLOCKED`/`PRIVATE` here) and a `/many` section in
  `tests/live_integration.py` that drives the real `invoke_plugin_command(…, source=SessionSource(…))`
  boundary the gateway uses.

## 0.3.1 — a machine marker is not a role

Found in catalog review: `pre_llm_call` decided "the host is talking to itself" from the message text
*before* it looked at who sent the message, so anybody could type `[cron]` and be recorded as the
virtual `operator` — owner tools, `terminal` and `execute_code` included, memory scoping skipped.

- **A machine turn is a marker AND no sender.** When the event carries a `sender_id`, that person is
  resolved and their role applied, whatever the message starts with; `operator` is granted only when
  there is no sender at all (which is exactly how the host's cron turns arrive — no `platform`, no
  `user_id`). A marker attached to a real sender now writes a `policy` audit row so an owner can see
  the attempt.
- **The operator's own terminal is recognised without a chat.** Hermes hands local turns
  `platform="cli"` with `uid:<uid>` as the sender — the subject the gateway control socket reports
  only for a peer whose uid is this process's own. That is the same trust boundary as "no sender",
  so it is the operator's seat again instead of being refused as an unknown sender (which is what the
  audit trail showed happening on the machine this was developed on). Exact match on the attested
  subject, `local_seat_policy: roster` to opt out.
- **A call with no identity is no longer blamed on a role.** Tools called from inside `execute_code`
  reach `pre_tool_call` with an empty session and turn id, so the refusal used to tell the *owner* to
  "ask the owner to make you a member". It now says the call carried no identity and names the knob.
- **`db_path` is a `config_schema` setting** instead of a `MANYHANDS_DB` env override; `.env` is for
  secrets. The env var stays as the test-script escape hatch and never wins over `config.yaml`.
- 55 tests (+8), including one per marker spoof and one that pins the real cron path.

## 0.3.0 — the owner's approval is a one-shot grant

Found by testing on a live two-account Telegram bot, not in unit tests.

- **Plugin escalations no longer rely on the host's approval transport.** `request_tool_approval`
  routes them through `_ACTION_GATE` (`transport=False`), so a registered transport is never
  consulted and the prompt lands on the asking session's own surface. manyhands now parks the ask
  under a short code, DMs the owner itself, and blocks the call.
- **`/many approve CODE` opens a one-shot grant** bound to that person and that tool, with its own
  window (`GRANT_TTL_S`). The asker's next attempt spends it (`consumed_at`) and runs; the one after
  asks again.
- **An expired code is not consent.** The deadline is enforced when listing pending codes and when
  deciding one, even if nothing swept the row.
- **One live code per person and tool.** A second ask supersedes the first instead of littering the
  owner's chat with prompts that disagree.
- **Undeliverable prompt ⇒ deny**, and the row is expired so it never shows as waiting.
- The person card states that calling a gated tool is the right move — the model was answering
  "no shell" instead of attempting the call that raises the prompt.
- `approvals.consumed_at` (schema 3, `PRAGMA user_version` + idempotent `ALTER TABLE`).

## 0.2.0 — three states, per-turn identity, the operator's memory withheld

- Three-state permissions (`allow` / `ask` / never) instead of allow/deny.
- Identity keyed on `turn_id` first with a persisted session fallback; fail closed when cold.
- `llm_request` middleware withholds the operator's `MEMORY.md`/`USER.md` blocks from everybody else
  and injects the person card into the system prompt.
- Member defaults no longer include the tools that index the operator's disk.
- `/many pause`, `/many audit` per person, `/many role`, `/many invite`.

## 0.1.0 — one Hermes, many people

First release: roster, invites, per-person facts, role → tool allowlists, owner-DM approvals, local
SQLite audit, six tools, `/many`, stdlib only.
