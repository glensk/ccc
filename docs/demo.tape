# ccc demo — terminal recording script for vhs (https://github.com/charmbracelet/vhs)
#
# Render the GIF with:   vhs docs/demo.tape
# (writes docs/img/demo.gif). vhs is a standalone tool; nothing here needs it at
# build time — the screenshots in the README come from tools/gen_screenshots.py.

Output docs/img/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1400
Set Height 900
Set Padding 20
Set Theme "Catppuccin Mocha"

# --- the flat list, no wiring needed ---------------------------------------
Type "ccc demo --ls"
Enter
Sleep 3s

# --- the interactive TUI against the same fake data ------------------------
Type "ccc demo"
Enter
Sleep 2s

# browse the sessions
Down@500ms 4
Sleep 1s
Up@500ms 2
Sleep 1s

# peek at the AIM history of the highlighted row (a … then h chord)
Type "a"
Type "h"
Sleep 2s
Escape
Sleep 1s

# open the full key reference
Type "?"
Sleep 2s
Escape
Sleep 1s

# quit
Type "q"
Sleep 1s

# tidy up the throwaway demo home
Type "ccc demo --clean"
Enter
Sleep 1s
