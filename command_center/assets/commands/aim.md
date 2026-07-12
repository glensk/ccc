Set this Claude Code session's done-condition (its "AIM") in the command center.

Usage: /aim <what "done" looks like for this session>

Run this exact command (`ccc` resolves the current session from the working directory):

```
ccc set-aim "$ARGUMENTS"
```

Then, in one short line, confirm what the AIM is now set to. If the session has no
sub-goals yet, propose 3-6 concrete, checkable sub-goals toward that AIM and offer to
save them so progress can be tracked:

```
ccc subgoals "first step" "second step" "third step"
```

The AIM and a progress bar then appear on the status line and in `ccc` / `ccc ls`.
