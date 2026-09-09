# Deployment

User units — no root, no sudo. They run as your own user, which is what you want:
the `claude` OAuth credentials are yours, not the machine's.

```bash
cp systemd/*.service systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now dealbot.timer      # the 15-minute poll
systemctl --user enable --now dealbot-web.service # the dashboard
```

`loginctl enable-linger $USER` (already set on koda) is what makes user units
start at boot without a login session.

| unit | what it does |
|---|---|
| `dealbot.timer` | fires every 15 min → `dealbot once --due` |
| `dealbot.service` | oneshot; each hunt decides whether its own interval has elapsed |
| `dealbot-web.service` | dashboard on :8477, `Restart=always` |

Watching it:

```bash
journalctl --user -u dealbot -f
systemctl --user list-timers dealbot
systemctl --user stop dealbot.timer     # pause
```

**The dashboard has no authentication** and `/triage` is a POST that mutates
state. Whatever fronts it must require auth.
