"""Minimal .env loader.

Secrets live here rather than in config.yaml so the config stays shareable --
you can paste it into a chat or commit it without leaking a webhook.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path = ".env") -> dict[str, str]:
    """Read KEY=VALUE lines. Existing environment variables win, so a systemd
    `Environment=` or an exported shell value can override the file."""
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            values[key] = value
            os.environ.setdefault(key, value)
    return values
