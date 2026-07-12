Mark this Claude Code session's AIM as achieved (done) in the command center.

Usage: /done

Run exactly:

```
ccc mark-done
```

Then confirm in one short line that the session is marked done. It will show as ✓ in
`ccc` / `ccc ls` and is exempt from the idle reaper's stale-goal alerts.

(To reopen a session marked done by mistake: `ccc mark-done --undo`.)
