Set (or clear) a finish-by deadline for this Claude Code session.

Usage: /deadline <YYYY-MM-DD>   (ISO-8601; e.g. /deadline 2026-07-15)

Run exactly:

```
ccc set-deadline "$ARGUMENTS"
```

Then confirm in one short line. The deadline drives a colour-coded badge (green →
amber within a few days → red when overdue), sorting in `ccc`, and stale-goal alerts.

(To clear it: `/deadline` with no date, or `ccc set-deadline ""`.)
