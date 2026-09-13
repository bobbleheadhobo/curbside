"""Waking hours.

The bot does not need to be awake at 3am. Nothing found at 3am can be collected
at 3am, and every hour it spends fetching is requests against two sources that
throttle and quota shared with otter.

So there is a window, and outside it the whole pass is skipped -- fetching
included. That is deliberately unlike the quota and rate-limit pauses, which
keep collecting and stop only the judging: those are *interruptions* the bot
recovers from, and losing data to one would be a bug. This is a decision, and
the same one the pause switches on `/runs` make.

The window lives in the `settings` table, not in `config.yaml`, for the reason
the pause switches do: the dashboard and a hand edit are never fighting over one
file. `config.yaml` supplies the starting values and the timezone, and stops
mattering the moment the window is set from the web.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

log = logging.getLogger("dealbot.schedule")

SETTING_ENABLED = "schedule.enabled"
SETTING_START = "schedule.start"
SETTING_END = "schedule.end"


def parse_hhmm(text: str | None) -> int | None:
    """"20:00" -> 1200 minutes past midnight. None if it is not a time.

    Tolerant on purpose: this reads a settings row, and a value that cannot be
    parsed must fall back to the default rather than take the bot off the air.
    """
    if not text:
        return None
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    # 24:00 is rejected rather than clamped: `fmt_hhmm` renders 1440 as
    # "00:00", so saving and reloading would silently turn "awake until
    # midnight" into "awake until midnight last night". `<input type=time>`
    # cannot produce it anyway.
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    return hh * 60 + mm


def fmt_hhmm(minute: int) -> str:
    """1200 -> "20:00". The form the settings row and the <input type=time> use."""
    return f"{minute // 60 % 24:02d}:{minute % 60:02d}"


def fmt_clock(minute: int) -> str:
    """1200 -> "8pm". For prose, where "20:00" reads like a log line."""
    h24, mm = minute // 60 % 24, minute % 60
    h12 = h24 % 12 or 12
    suffix = "am" if h24 < 12 else "pm"
    return f"{h12}:{mm:02d}{suffix}" if mm else f"{h12}{suffix}"


@dataclass(frozen=True)
class Schedule:
    """When the bot is allowed to run. Minutes past local midnight.

    `start == end` means always on, not never on. Every filter in this project
    fails open, and a schedule that quietly took the bot off the air for good
    would look exactly like a broken scraper -- which is the one failure mode
    the runs view exists to rule out.
    """
    enabled: bool = False
    start_minute: int = 12 * 60
    end_minute: int = 20 * 60
    tz: tzinfo | None = None          # None = the machine's local time
    tz_name: str | None = None

    @property
    def always_on(self) -> bool:
        return not self.enabled or self.start_minute == self.end_minute

    def now(self) -> datetime:
        return datetime.now(self.tz) if self.tz else datetime.now().astimezone()

    def is_open(self, now: datetime | None = None) -> bool:
        if self.always_on:
            return True
        now = now or self.now()
        minute = now.hour * 60 + now.minute
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute < self.end_minute
        # Wraps midnight -- 8pm to 6am is one window, not two.
        return minute >= self.start_minute or minute < self.end_minute

    def opens_at(self, now: datetime | None = None) -> datetime | None:
        """The next moment it is awake. None when it never sleeps."""
        if self.always_on:
            return None
        now = now or self.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        when = today + timedelta(minutes=self.start_minute)
        return when if when > now else when + timedelta(days=1)

    def opened_at(self, now: datetime | None = None) -> datetime | None:
        """When the CURRENT window began. None if always on, or asleep.

        The health pill needs this. Waking at noon after sleeping since 8pm, the
        last run is sixteen hours old and "Quiet 16h ago" is the warning for a
        broken scraper -- which it is not, for the first fifteen minutes.
        """
        if self.always_on:
            return None
        now = now or self.now()
        if not self.is_open(now):
            return None
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        when = today + timedelta(minutes=self.start_minute)
        return when if when <= now else when - timedelta(days=1)

    @property
    def window_label(self) -> str:
        if self.always_on:
            return "always"
        return f"{fmt_clock(self.start_minute)} to {fmt_clock(self.end_minute)}"

    @property
    def awake_minutes(self) -> int:
        """How long the window actually is. Wraps midnight, so this is modular
        arithmetic rather than `end - start`."""
        if self.always_on:
            return 24 * 60
        return (self.end_minute - self.start_minute) % (24 * 60) or 24 * 60

    @property
    def span_label(self) -> str:
        """How long, in words, because "11pm to 8pm" does not say.

        A window that runs past midnight reads at a glance like a SHORT one --
        11pm to 8pm looks like a night shift and is in fact 21 hours awake, the
        near-opposite. The interface let that be set and then described it in
        the one way that hides it. So the length is stated outright, and the
        wrap is called out, because an hours control the user misreads is the
        same class of problem as a pause switch nobody can see.
        """
        if self.always_on:
            return "Always on."
        mins = self.awake_minutes
        hrs, rem = divmod(mins, 60)
        span = f"{hrs}h{rem:02d}m" if rem else f"{hrs} hours"
        out = f"Awake {span} a day."
        if self.end_minute <= self.start_minute:
            out += " This window runs past midnight."
        return out

    def state_label(self, now: datetime | None = None) -> str:
        """One phrase for the health pill: awake until when, or asleep until when."""
        if self.always_on:
            return "Always on"
        now = now or self.now()
        if self.is_open(now):
            return f"Awake until {fmt_clock(self.end_minute)}"
        return f"Asleep until {fmt_clock(self.start_minute)}"


def load(store, defaults: "ScheduleDefaults") -> Schedule:
    """The window as it stands: the settings table, falling back to config.

    Read on every pass and on every page load rather than cached, so a change
    made on the phone takes effect on the next timer tick with nothing to
    restart.
    """
    raw_enabled = store.get_setting(SETTING_ENABLED)
    enabled = defaults.enabled if raw_enabled is None else raw_enabled == "1"
    start = parse_hhmm(store.get_setting(SETTING_START))
    end = parse_hhmm(store.get_setting(SETTING_END))
    return Schedule(
        enabled=enabled,
        start_minute=defaults.start_minute if start is None else start,
        end_minute=defaults.end_minute if end is None else end,
        tz=defaults.tz, tz_name=defaults.tz_name)


def save(store, *, enabled: bool, start_minute: int, end_minute: int) -> None:
    store.set_setting(SETTING_ENABLED, "1" if enabled else "0")
    store.set_setting(SETTING_START, fmt_hhmm(start_minute))
    store.set_setting(SETTING_END, fmt_hhmm(end_minute))


@dataclass(frozen=True)
class ScheduleDefaults:
    """What `config.yaml` says, used only until the window is set from the web."""
    enabled: bool = False
    start_minute: int = 12 * 60
    end_minute: int = 20 * 60
    tz: tzinfo | None = None
    tz_name: str | None = None
