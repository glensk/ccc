Set/override the "next step" for this Claude Code session in the command center.

Usage: /next-step <the next concrete action>

This overrides any auto-generated next step (and marks it as user-authored, so the
daemon will not overwrite it). Run exactly:

```
ccc set-next "$ARGUMENTS"
```

Then confirm the next step in one short line. It appears on status-line row 3
("/next-step: …") and on the session's card in `ccc`.
