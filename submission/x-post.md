# X post kit

Canonical copy lives here. The video is `docs/`-adjacent — deliver it as an attachment
(`~/hackathon/manyhands-demo.mp4`, 76 s, 1080p).

## The post (attach the video)

```
Your Hermes bot in a group chat gives everyone in it one agent: same memory, same tools, same
permissions.

manyhands gives each person their own. Own notes, own tools, your memory withheld. A dangerous ask
lands in your DM as a code, and runs once.

github.com/PhiBao/manyhands
```

X counts every URL as 23 characters, so the effective length is well under 280.

## One-tap intent link

Open this, attach the video, tap Post. Text is percent-encoded already.

https://x.com/intent/post?text=Your%20Hermes%20bot%20in%20a%20group%20chat%20gives%20everyone%20in%20it%20one%20agent%3A%20same%20memory%2C%20same%20tools%2C%20same%20permissions.%0A%0Amanyhands%20gives%20each%20person%20their%20own.%20Own%20notes%2C%20own%20tools%2C%20your%20memory%20withheld.%20A%20dangerous%20ask%20lands%20in%20your%20DM%20as%20a%20code%2C%20and%20runs%20once.%0A%0Agithub.com%2FPhiBao%2Fmanyhands

## The thread, if you want to build it out

**1/5**
```
A Hermes bot in a group chat answers to everybody the same way. Same memory, same toolset, same
approval authority. Anyone in that room can ask it to run shell commands.

I built manyhands to fix that. 76 second demo 👇
```

**2/5**
```
Every sender gets a roster record: owner, member, guest, blocked. Resolved from the platform event,
never from what the message says.

Three states per tool, not two:
· allow — it runs
· ask — your DM decides
· never — a polite no that names only their own role
```

**3/5**
```
The part I care about most: your own memory is withheld.

Hermes builds every system prompt from MEMORY.md and USER.md. A guest used to receive your notes
verbatim. manyhands strips those blocks for non-owners and puts a card about that person in their
place.
```

**4/5**
```
The bug that only a live run finds: Hermes' action gate never consults a registered approval
transport (transport=False), so a plugin escalation cannot reach your DM through that seam.

So manyhands owns the hop: park the ask under a code, DM the owner, block the call. Your
/many approve CODE opens a one-shot grant bound to that person and that tool. Silence is not consent.
```

**5/5**
```
Everything is local: one SQLite file, standard library only, no network.

Every turn, refusal, parked ask, spent grant and token count is an audit row, so you can read back
who asked for what.

github.com/PhiBao/manyhands
```

## What is honest about the video

- Every terminal line is **real output** from `tests/demo_capture.py`, which drives the real
  `PluginManager`, the real hook dispatch, the real `get_pre_tool_call_directive` and a real
  `ApprovalRequest` in a throwaway `HERMES_HOME`. The script is in the repo and runnable.
- The three people are synthetic (Kiter / Minh / Zara). No live Telegram account is in the video and
  it does not claim one.
- The approval beat is the real mechanism, including the retry: the owner's `/many approve CODE`
  spends a grant the asker's next attempt consumes.
- The latency footnote (0.53 ms `pre_llm_call`, 0.15 ms `pre_tool_call`) comes from
  `python3 tests/bench_hooks.py 500` on this machine.
- Separately from the video, the whole flow was also run on a live two-account Telegram bot; say so
  if asked, and the audit rows are in `/many audit`.
