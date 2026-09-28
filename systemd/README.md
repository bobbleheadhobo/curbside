# Deployment

User units — no root, no sudo. They run as your own user, which is what you want:
the `claude` OAuth credentials are yours, not the machine's.

```bash
cp systemd/*.service systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now curbside.timer      # the 15-minute poll
systemctl --user enable --now curbside-web.service # the dashboard
systemctl --user enable --now claude-update.timer # keeps Claude Code current
```

`loginctl enable-linger $USER` (already set on koda) is what makes user units
start at boot without a login session.

| unit | what it does |
|---|---|
| `curbside.timer` | fires every 15 min → `curbside once --due` |
| `curbside.service` | oneshot; each hunt decides whether its own interval has elapsed |
| `curbside-web.service` | dashboard on :8477, `Restart=always` |
| `claude-update.timer` | 04:00, 12:00, 20:00 Denver → `claude update` |

**Why the updater.** The scorer passes a model *family* (`--model sonnet`) and
Claude Code resolves it to that family's newest model. The alias only moves
when Claude Code does, so without updates a new model can ship and the bot
keeps running the old one. The settings page shows the version the last run
reported, and the Claude Code build.

A failing update is silent: it only reaches the journal
(`journalctl --user -u claude-update -n 5`). If an update breaks scoring, point
`~/.local/bin/claude` back at an older build in
`~/.local/share/claude/versions/` and
`systemctl --user disable --now claude-update.timer` until it is fixed.

**Template edits are live; Python edits are not.** Jinja re-reads templates per
request, uvicorn does not reload the module, so after changing anything under
`curbside/` you must `systemctl --user restart curbside-web` or the dashboard
keeps serving new markup on old code.

Watching it:

```bash
journalctl --user -u curbside -f
systemctl --user list-timers curbside
systemctl --user stop curbside.timer     # pause
```

**The dashboard has no authentication** and `/triage` is a POST that mutates
state. Whatever fronts it must require auth.
