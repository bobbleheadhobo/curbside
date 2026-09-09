"""Can we reach the API at all?

A `claude -p` run launched without connectivity does NOT fail fast -- it burns
roughly ten minutes of retry backoff and then exits having accomplished nothing.
On a 15-minute poll interval that is worse than useless: the subprocess is still
spinning when the next poll fires and runs stack up behind it.

So we spend ~200ms on a TCP probe before spending ten minutes finding out.
(Borrowed wholesale from otter, which learned this the expensive way.)
"""
from __future__ import annotations

import socket

API_HOST = "api.anthropic.com"
API_PORT = 443
TIMEOUT_SECONDS = 3.0


def api_reachable(host: str = API_HOST, port: int = API_PORT,
                  timeout: float = TIMEOUT_SECONDS) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
