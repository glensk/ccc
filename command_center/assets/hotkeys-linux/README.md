# Linux hotkey samples for ccc

**Sample, suggested-only** key-daemon configs that bind a chord to `ccc peek` on
Linux. There is **no installer** — copy/paste and adapt these yourself. They are the
Linux counterpart of the macOS Karabiner samples (`../karabiner/`).

## Honest scope — what works on Linux

`ccc peek`'s **floating panel** and `ccc jump` are **macOS-only** today (they use
AppKit / iTerm AppleScript). On Linux, `ccc peek` automatically behaves as
`ccc peek --print` — it dumps the focused/selected session's prompts to **stdout**.
So the Linux "peek chord" idea is simply: bind a key to run **`ccc peek --print`** and
show its text somewhere (a terminal, a desktop notification, or a scratch file).

The cleanest experience is to run `ccc peek --print` **in your terminal** directly.
The samples below wire a global hotkey to it and route the output to `notify-send`
(falling back to `~/.cache/ccc-peek.txt`) because a global key daemon has no notion of
"the current terminal".

| File                 | Daemon | Chord                         | Runs               |
| :------------------- | :----- | :---------------------------- | :----------------- |
| `keyd-peek-s-p.conf` | keyd   | hold **s**, tap **p** (`s`+`p`) | `ccc peek --print` |
| `xremap-peek.yml`    | xremap | **Super**+**p**               | `ccc peek --print` |

### keyd — the true `s`+`p` chord

[keyd](https://github.com/rvaiya/keyd) does real tap-vs-hold discrimination, so it can
reproduce the macOS chord faithfully: **tapping** `s` still types `s`; only **holding**
`s` turns it into a layer while you tap `p`. Requires keyd **≥ 2.4.0** (the `command()`
action). Rough install:

```commands
sudo cp keyd-peek-s-p.conf /etc/keyd/ccc.conf
sudo keyd reload
```

Caveat: keyd's `command()` runs from the **keyd service context** (usually root, with no
`DISPLAY`/`DBUS`). The sample routes output via `notify-send` when present, else writes
`~/.cache/ccc-peek.txt`. To get a desktop notification you may need a wrapper that sets
`DISPLAY`/`DBUS_SESSION_BUS_ADDRESS` for your user, or just read the scratch file.

### xremap — a practical Super+p equivalent

[xremap](https://github.com/xremap/xremap) (X11 + Wayland) has no per-key overload, so it
cannot make a **letter** key like `s` a hold-to-activate modifier without hijacking `s`
for typing. The sample uses **Super+p** — a real modifier chord — as the practical
equivalent. Use keyd if you want the exact hold-`s` chord.

## Notes

- Both are **suggested-only**; `ccc init` does not wire them (it just prints a pointer to
  this folder on Linux). See `docs/linux.md`.
- Edit the `launch`/`command` line to taste — e.g. open a terminal
  (`x-terminal-emulator -e sh -c 'ccc peek --print; read'`) instead of `notify-send`.
