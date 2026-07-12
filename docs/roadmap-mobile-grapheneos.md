# Roadmap: following & starting jobs from a phone

A recurring goal for `ccc` is to make your Claude Code work **followable and steerable from
a phone** — including a privacy-focused Android such as [GrapheneOS](https://grapheneos.org)
— so you can keep sessions moving while away from your main machine. This is a roadmap
note, not a shipped feature; parts of it already work today, and the rest is a direction.

## The goal

You are away from your development machine. You want to:

- **see** what every session is doing — its AIM, progress, status, what it's blocked on;
- **queue** new work as future jobs, from wherever you are;
- **start** a parked or future job so it runs on your (more capable) home machine;
- do all of this over a small screen and a flaky connection, without exposing anything
  private.

## What already works today

The vault side of `ccc` is phone-ready right now, because Obsidian has a first-class mobile
app and syncs your vault:

- **Read everything on mobile.** With the mirror flags on (see
  [obsidian.md](obsidian.md)), every session — running, parked, done — is a markdown note
  in your synced vault: AIM, sub-goals, next step, the full conversation. Open the Obsidian
  mobile app and your entire Claude Code history is there, searchable.
- **Queue future jobs from the phone.** Create or edit a future-job note in Obsidian mobile
  (or with the capture pad) — it's just markdown. The next sync registers it.
- **Start a job by tapping a checkbox.** Every job file carries a bare-boolean
  `launch: true/false` frontmatter key that Obsidian mobile renders as a one-tap checkbox.
  Flip it, and the sync daemon on your home machine consumes the flag and launches the job
  (falling back to a tmux window when GUI scripting isn't available). This is the working
  phone-flip → sync → launch chain.

So today, from a phone with nothing but the Obsidian app, you can **watch** your sessions
and **trigger** a queued job on your home machine.

## What's planned

The missing half is **live control of the terminal side** from the phone — driving the
`ccc` TUI and Claude Code sessions directly, not just flipping a checkbox and waiting for
the sync. This is deliberately left as a *your-choice* integration rather than something
`ccc` bundles, because it is about network access, not about `ccc` itself:

- **Remote access to your home machine.** A private overlay network (e.g. WireGuard or
  [Tailscale](https://tailscale.com)) plus SSH gives you a real terminal on your home
  machine from the phone. The `ccc` TUI renders fine in a mobile terminal (e.g. Termux),
  and `ccc serve` exposes the same UI over a browser for an even lighter client. With the
  host's sessions living in a persistent tmux session (`launcher = "tmux"`), a dropped
  connection loses nothing. *Verified working — see the README's
  "Mobile access (GrapheneOS)" section for the recipe.*
- **Push + steer.** Claude Code's own remote-control feature fronts the sessions your home
  machine runs, with `ccc` as the at-a-glance overview: a `claude remote-control
  --spawn same-dir --name <host>` server kept alive in tmux appears in the mobile app's
  Code tab and spawns fresh host-side sessions on demand — no terminal needed. Every
  session it spawns shows up in `ccc ls` like any other. *Also verified — recipe in the
  README section above.*
- **A backup rig on the phone itself.** Running Claude Code natively on Android is
  unofficial, but a Linux userland (e.g. a proot distro inside Termux) can host the CLI,
  `uv`, your repos and `ccc` as a fallback when the home machine is unreachable. This is
  fragile by nature — pin and test before relying on it.

## Principles

- **Zero personal data in this repo.** This document intentionally contains no trip dates,
  locations, hardware models, carrier/data-plan details, or personal security scripts. The
  mobile story is a capability, not a diary.
- **Your network, your choice.** `ccc` does not ship or require Tailscale, SSH config, or a
  particular Android setup. It ships the vault mirrors and the `launch:` toggle; the remote
  transport is yours to wire.
- **Privacy-preserving by default.** The remote path is designed around a private overlay
  network and your own credentials — nothing is proxied through a third party.
