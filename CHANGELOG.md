# Changelog

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
