# manyhands

**One Hermes, many people.** Every sender who talks to your bot gets their own identity, their own
memory, their own tool allowlist and their own approvals — and you can see who asked for what.

```
Kiter  (owner)   → every tool
Minh   (member)  → web_search, recall_search, manyhands_remember …
Guest  (guest)   → web_search, manyhands_whoami
```

A Hermes gateway is a single-operator agent. The platform already carries per-sender identity into
every turn (`pre_llm_call` receives `sender_id` + `platform`), but nothing acts on it: everybody in
the group talks to the same memory, the same toolset and the same approval authority. manyhands is
the plugin that acts on it.

- **Identity** comes from the platform event, never from message text.
- **Roles** are an allowlist: `owner`, `member`, `guest`, `blocked`. Anything a role does not name is
  refused — including tools that ship next year, third-party tools and MCP tools.
- **Approvals** for anything sensitive go to the **owner's DM**, not to whoever is in the room.
- **Memory** is per person. Their facts are injected into their turns and nobody else's.
- **Audit**: turns, tool calls, refusals, approvals and token usage, per person, in local SQLite.

Nothing leaves the machine. No network calls, no third-party dependencies, standard library only.

---

## The gap it fills (measured, not assumed)

The public plugin catalog (`https://hermes-agent.nousresearch.com/docs/api/plugin-catalog.json`,
1,063 entries) has **zero** entries named `manyhands`, `household`, `rbac`, `tenant`, `roster`,
`invite` or `permission`. Nineteen entries use per-person language and every one of them is either
memory-layer storage scoping (`severian`, `mnemostack`, `agent_memory_es`, `holographic`, `dexio`,
`hicortex`, `tam`, `xmemo`) or dashboard login (`dashboard-auth-feishu`). **No plugin in the catalog
registers an approval transport or a memory provider keyed on the sender.**

Closest neighbours, and how this differs:

| Neighbour | What it does | Why manyhands is not it |
|---|---|---|
| `airlock` | taint tracking: once a session reads untrusted content, outbound actions need approval | different axis — content provenance, not human identity. It cannot tell your accountant from your intern. |
| `approval-ledger` | records approval prompts per rule/surface/day | metrics about approvals, not *who may do what*. |
| `hermes-teammates`, `bot-forge`, `advanced-delegation`, `omh` | rosters of **subagents/bots** | those are agents the operator owns. manyhands is a roster of **humans** the operator serves. |
| `identity` | edits SOUL/MEMORY/USER files, switches profiles | single operator, manual, no per-sender enforcement. |
| memory providers (`severian`, `hicortex`, …) | store memory per tenant/profile | storage scoping only: no roles, no tool permissions, no approvals, nothing audited. |

The claim this repo defends is narrow and true: **human-side multi-tenancy inside one live gateway
conversation does not exist in the marketplace.**

---

## What it registers

| Surface | Name | What it does |
|---|---|---|
| tool | `manyhands_whoami` | the caller's name, role, exact tool list, fact count |
| tool | `manyhands_people` | the roster; `add`/`role`/`tools`/`remove` are owner-only |
| tool | `manyhands_remember` | write a fact into the **caller's own** namespace |
| tool | `manyhands_recall` | search the caller's facts + shared facts |
| tool | `manyhands_share` | publish a fact to everyone (owner/member) |
| tool | `manyhands_audit` | turns, tool calls, refusals, approvals, tokens |
| hook | `pre_llm_call` | resolve identity, apply the unknown-sender policy, enforce the daily budget, inject the person card, record `session_id → person_id` |
| hook | `pre_tool_call` | role → tool allowlist; escalate sensitive tools to approval; **fail closed** on unknown sessions |
| hook | `post_api_request` | attribute token usage to whoever owns the session |
| transport | `manyhands` | present approval prompts in the **owner's DM** under a short code |
| command | `/many` | `whoami · roster · audit [name] [hours] · invite [role] · claim [code] · role <name> <role> · tools <name> [add\|remove] <tool…> · remove <name> · pause · resume · approve <code> · deny <code>` |

## Install

```bash
hermes plugins install https://github.com/PhiBao/manyhands   # or: git clone … into your plugins dir
hermes plugins enable manyhands
```

Then, once:

1. **Claim ownership from the bot's own terminal.** `hermes chat`, then `/many claim`. It prints a
   one-time code. Send `/many claim <code>` from the chat account that should own the bot. Claiming
   requires local access to the machine — a stranger in the group cannot make themselves owner.
2. **Invite everybody else.** `/many invite member` mints a single-use code. They reply
   `join <code>` in the chat. Nobody self-registers silently.
3. **Route approvals to your DM** (optional but this is the point):
   ```yaml
   security:
     approval:
       transport: manyhands
   plugins:
     entries:
       manyhands:
         allow_gateway_injection: true    # the DM channel
   ```

Without step 3 the roles and memory still work; approval prompts then go to the host's own surface
instead of your DM.

## The safety contract

These are rules, not intentions. Each one has a test that fails if it stops being true.

1. **Identity comes from the platform event, never from message text.** A message saying "I'm the
   owner" changes nothing (`test_identity_never_comes_from_message_text`).
2. **Fail closed on unknown state.** A session with no human turn on it — a restart, a subagent, a
   tool driven from the socket — gets the guest allowlist, not the owner's
   (`test_cold_session_map_blocks_restricted_tools`).
3. **Refusals are polite, specific and non-disclosing.** A blocked tool returns a plain-language
   message naming the person's own role and never what somebody else has access to.
4. **Nothing is silent.** Every turn, tool decision, refusal, approval and token count is an audit
   row; `/many audit` shows them.
5. **The owner can always act**, and can switch enforcement off with `/many pause` — which writes a
   loud audit row instead of quietly forgetting.
6. **Per-person memory is never merged.** No query in this plugin can read another person's facts:
   the namespace is a mandatory argument, not a filter.
7. **Only a human can approve.** `/many approve CODE` is a slash command on the owner's own message;
   there is no tool, hook or prompt path that can approve an escalation. An agent cannot talk itself
   into permission.
8. **Machine-generated turns are the operator, not a stranger.** Cron prompts, internal notices and
   auto-continuations are recorded and not policed — otherwise the plugin would lock the owner out
   of their own scheduled jobs. The `[OUT-OF-BAND USER MESSAGE]` marker is deliberately **not** on
   that list: the host uses it for mid-turn steering from the same human, and trusting it as the
   operator would let a guest's steer run with owner tools.
9. **The allowlist is the whole policy.** A tool nobody named is refused. A denylist fails open on
   every tool nobody thought of; an allowlist fails closed.

## How the pieces fit (the two gotchas that shaped it)

**`pre_tool_call` has no sender identity.** Its payload is `tool_name, args, task_id, session_id,
tool_call_id, turn_id, api_request_id, middleware_trace`
(`hermes_cli/plugins.py:1907`, the `_PreToolCallDirective` gate). `pre_llm_call` is the only hook that
receives `sender_id` + `platform` (`agent/turn_context.py`). So identity is carried across in a
session map, written on every turn and **persisted in SQLite** so a restart does not lose it. When the
map and the database both come up empty, the call fails closed.

**The approval transport does not know who asked.** It receives a redacted `ApprovalRequest`
(`hermes_cli/approval_transport.py`) — command, description, pattern_key, timeout, allowed choices —
and nothing about the person. `pre_tool_call` therefore parks the escalation (who, which tool) in the
`approvals` table; the transport binds the host's `request_id`/`digest` onto the newest unclaimed
escalation for that tool and turns it into a prompt with a short code. Requests that did not come
from manyhands (a stock dangerous-command prompt) are still routed to the owner's DM, they just
cannot name the asker.

**Return values are directives, not booleans.** `pre_llm_call` accepts `{"context": …}` (appended to
the user message) or `{"action": "block", "message": …}` (turn ends, no inference). `pre_tool_call`
accepts `block` / `approve` (escalate to the host's gate) / `modify`, precedence `block > approve >
modify`.

## Data model

One SQLite file under `plugin-data/manyhands/`. WAL, no ORM.

```
people        person_id (platform:sender_id), platform, sender_id, display, name, role,
              tools_json, daily_tokens, created_at, last_seen
facts         per-person memory: person_id, topic, text, created_at, source
shared_facts  text, added_by, created_at
audit         ts, person_id, session_id, turn_id, kind (turn|tool|refusal|approval|policy|memory|tokens),
              tool, allowed, needs_approval, approved, tokens, note
sessions      session_id → person_id (survives a restart)
invites       single-use codes with the role they grant
approvals     code, person_id, tool, summary, request_id, digest, status, decided_by, choice, expires_at
settings      key/value (pause flag, pinned owner, db path)
```

`daily_tokens` is enforced against the UTC day, from the `post_api_request` usage the host reports —
not an estimate. 0 means unlimited.

## Honest limits

- **Permissions are for the model-driven tool path.** A tool call that never passes
  `pre_tool_call` (a plugin dispatching straight into another tool, a subagent with its own scope)
  is not seen by this plugin. It is a policy layer, not a kernel.
- **The owner's DM is Telegram-shaped.** `origin.chat_id` is built from the sender id, which is the
  private-chat id on Telegram. On Discord/Slack the DM channel has to be resolved first; where the
  plugin cannot address a person directly it **denies** rather than falling back to the room. One
  transport, one honest platform.
- **Memory is this plugin's own store.** It is injected as context and exposed through tools; it is
  not registered as the host's memory provider, so the host's own MEMORY.md is untouched and
  unscoped. Registering a per-person provider is the natural next step, and was left out on purpose
  to keep v1's blast radius small.
- **The token budget is per UTC day**, and a single long turn can overshoot before the next check.
- **`/many approve` is only as strong as the platform's identity.** If an attacker controls the
  owner's chat account, they are the owner.
- **A guest can still read anything the *model* says.** Refusing a tool is not the same as refusing
  a sentence; the shared facts are shared on purpose, per-person facts are not in a guest's context.

## Disclosure — every read, write and network path

- **Reads:** the plugin config (`plugins.entries.manyhands.settings.*`), the hook payloads listed
  above, and its own SQLite file. It does not read your documents, your shell, your keys or the
  host's memory store.
- **Writes:** its own SQLite file under `plugin-data/manyhands/`, and — only when
  `security.approval.transport: manyhands` is selected — one gateway message per approval prompt,
  sent to the owner's DM through `ctx.inject_message`. Nothing else. No files outside that directory.
- **Network:** none. No HTTP client is imported anywhere in this plugin.
- **Data at rest:** sender ids (`platform:sender_id`), roster names you set, the facts people store
  and the audit trail. `/many remove <name>` deletes that person and their facts; `manyhands_people`
  cannot delete the audit trail, on purpose.

## Tests

```bash
python3 tests/selftest.py     # 28 tests, stdlib unittest, no Hermes install required
hermes plugins validate ./    # the catalog CI gate
```

The suite covers, among others: unknown sender refused under `closed`; invite admitted once and only
once; guest tools blocked with a role-naming refusal; a member's sensitive tool escalated to the
owner's DM and denied on timeout; the prompt going to `chat_id == owner`, not the asker; an approved
code letting the tool run once; delivery failure failing closed; the session map surviving a restart;
a cold session blocked; per-person memory never leaking across people; shared facts visible to all; a
guest refused when publishing; machine-generated turns skipping identity resolution; the audit
counting turns/tools/refusals/tokens; and the daily budget blocking the next turn.

## Development

```
__init__.py     register(): the tools, three hooks, the transport, /many
policy.py       role → tool decisions, as pure functions (no I/O at all)
identity.py     (platform, sender_id) → person; unknown-sender policy; machine-turn detection
hooks.py        the three hook bodies, each wrapped so it can never raise into a turn
store.py        the SQLite schema and every query
tools.py        the six tool handlers + schemas
transport.py    the owner-DM approval transport
tests/selftest.py
```

MIT. Written for the Nous Research Hermes Agent Plugin Hackathon, November 2026.
