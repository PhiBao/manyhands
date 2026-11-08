# Changelog

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
