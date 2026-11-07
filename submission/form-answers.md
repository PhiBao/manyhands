# Entry form answers

Paste-ready. Two fields can only come from you: **Discord username** and the **X post link** (the
latter exists only after the video is published). Email and repo answers are filled in.

## 1. Plugin title

```
manyhands
```

## 2. Plugin description — elevator pitch

Short version (use this unless the field plainly wants more):

```
Every person who messages your Hermes bot gets their own agent: their own memory, their own tool
permissions, and approvals that reach the owner's DM instead of whoever is in the room. Strangers are
refused until you invite them, and your own MEMORY.md is withheld from everyone else's prompt. Local
SQLite, standard library only, no network.
```

Longer version (~450 characters, for a field that wants more):

```
One Hermes, many people. A bot in a group chat hands everybody the same memory, the same tools and
the same approval authority. manyhands resolves each sender into a role — owner, member, guest,
blocked — gives them their own facts and their own tool permissions, withholds your own MEMORY.md
from everyone else's prompt, and parks anything dangerous under a short code in your DM. One
approval runs it once. Local SQLite, standard library only, no network calls.
```

## 3. Email

```
kiter2509@gmail.com
```

## 4. Discord username

<!-- TODO from user: needs the Discord handle on the hackathon server -->

## 5. X demo post link

<!-- TODO from user: the post URL once the video is up -->

## 6. GitHub link to the catalog PR

```
https://github.com/NousResearch/hermes-agent/pull/150807
```

## Extra context, if a field asks for it

- Plugin repo: `https://github.com/PhiBao/manyhands` (pinned in the entry at `ab06705`)
- Demo video: 76 s, `~/hackathon/manyhands-demo.mp4`; every frame's terminal text is real output from
  `tests/demo_capture.py`
- What is verified: `python3 tests/selftest.py` → 47 tests; `tests/live_integration.py` → PASS through
  the real host; `hermes plugins validate ./` → passed; plus a live two-account Telegram run
  (stranger refused → invite → guest → member → parked ask → owner DM → one-shot grant → ran once →
  next ask parked again)
- Honest limits, if asked: identity fails closed on sessions with no human turn (delegation,
  subagents); DM routing is Telegram-shaped; a third-party memory provider's block is not stripped;
  and a plugin escalation cannot use Hermes' approval-transport seam (`_ACTION_GATE` is
  `transport=False`), which is why the plugin parks and DMs the ask itself
