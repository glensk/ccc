Show how this Claude Code session's sub-goal checklist evolved — every version, with its trigger, the AIM revision it tracked, and the impartial drift checker's verdict (a blue ● means it flagged drift from the AIM).

Usage: /subgoal-history

Run this exact command (`ccc` resolves the current session from the working directory):

```
ccc subgoal-history
```

Then, in one short line, summarize the progression — how the checklist tracked the AIM over time and whether any version drifted. If a version is flagged as drift, say what was dropped/weakened. Do not change the sub-goals; this is read-only. (To resolve a flagged drift after reconciling, run `ccc ack-drift`.)
