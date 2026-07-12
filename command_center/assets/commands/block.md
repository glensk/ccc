Record what this Claude Code session is blocked on / waiting for.

Usage: /block <what you're waiting on>   (e.g. /block check the tab at home)

Run exactly:

```
ccc set-blocked "$ARGUMENTS"
```

Then confirm in one short line. The blocked-on tag shows on the session's card in
`ccc` so parked work waiting on a manual/offline action is visible at a glance.

(To clear it: `/block` with no text, or `ccc set-blocked ""`.)
