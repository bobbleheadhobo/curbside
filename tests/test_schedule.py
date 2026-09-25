"""Waking hours.

The window stops the whole pass, fetching included, which makes it the one
pause in this project that can lose data. So it has to be exactly as wide as it
says it is, it has to fail open, and it has to be visible: eight hours of
deliberate silence and a scraper that died on Tuesday look identical otherwise.
"""
import pathlib
from datetime import datetime, timedelta

import pytest

from curbside import schedule as sched_mod
from curbside.db import Store
from curbside.schedule import Schedule, ScheduleDefaults, fmt_clock, fmt_hhmm, parse_hhmm


def at(hour, minute=0):
    return datetime(2026, 9, 10, hour, minute)


def test_the_window_includes_its_start_and_excludes_its_end():
    s = Schedule(enabled=True, start_minute=12 * 60, end_minute=20 * 60)
    assert not s.is_open(at(11, 59))
    assert s.is_open(at(12, 0))
    assert s.is_open(at(19, 59))
    assert not s.is_open(at(20, 0))          # or 8pm to 8pm would overlap


def test_a_window_may_wrap_midnight():
    """8pm to 6am is one window, not two, and not none."""
    s = Schedule(enabled=True, start_minute=20 * 60, end_minute=6 * 60)
    assert [s.is_open(at(h)) for h in (5, 6, 12, 19, 20, 23)] == \
           [True, False, False, False, True, True]


def test_an_empty_window_means_always_on_not_never_on():
    """Fails OPEN, like every other filter here. A schedule that quietly took
    the bot off the air for good would look exactly like a broken scraper."""
    s = Schedule(enabled=True, start_minute=9 * 60, end_minute=9 * 60)
    assert s.always_on and s.is_open(at(3))


def test_disabled_means_always_on():
    assert Schedule(enabled=False, start_minute=12 * 60,
                    end_minute=20 * 60).is_open(at(3))


def test_times_parse_and_print_both_ways():
    assert parse_hhmm("20:00") == 1200
    assert parse_hhmm("07:30") == 450
    assert fmt_hhmm(1200) == "20:00"
    assert (fmt_clock(720), fmt_clock(1200), fmt_clock(0), fmt_clock(450)) == \
           ("12pm", "8pm", "12am", "7:30am")


@pytest.mark.parametrize("bad", [None, "", "nonsense", "25:00", "12:99", "12"])
def test_an_unreadable_time_is_ignored_rather_than_raised_on(bad):
    """This reads a settings row. A value that will not parse must fall back to
    the default, not take the bot off the air."""
    assert parse_hhmm(bad) is None


def test_the_window_round_trips_through_the_store(tmp_path):
    store = Store(tmp_path / "t.db")
    defaults = ScheduleDefaults(enabled=False, start_minute=8 * 60,
                                end_minute=18 * 60)
    assert sched_mod.load(store, defaults).start_minute == 8 * 60

    sched_mod.save(store, enabled=True, start_minute=12 * 60, end_minute=20 * 60)
    got = sched_mod.load(store, defaults)
    assert (got.enabled, got.start_minute, got.end_minute) == (True, 720, 1200)


def test_a_corrupt_settings_row_falls_back_to_the_file(tmp_path):
    store = Store(tmp_path / "t.db")
    store.set_setting("schedule.start", "banana")
    defaults = ScheduleDefaults(enabled=True, start_minute=12 * 60,
                                end_minute=20 * 60)
    assert sched_mod.load(store, defaults).start_minute == 12 * 60


def test_it_knows_when_the_current_window_began_and_the_next_one_starts():
    s = Schedule(enabled=True, start_minute=12 * 60, end_minute=20 * 60)
    assert s.opened_at(at(13)) == at(12)
    assert s.opened_at(at(9)) is None                  # asleep: no window open
    assert s.opens_at(at(9)) == at(12)
    assert s.opens_at(at(21)) == at(12) + timedelta(days=1)


def test_the_state_reads_as_a_sentence():
    s = Schedule(enabled=True, start_minute=12 * 60, end_minute=20 * 60)
    assert s.window_label == "12pm to 8pm"
    assert s.state_label(at(9)) == "Asleep until 12pm"
    assert s.state_label(at(13)) == "Awake until 8pm"


# --- the gate on an actual run ---------------------------------------------

def _args(tmp_path, **kw):
    """The real config, pointed at recorded responses and the stub scorer.
    Every test here is offline; nothing may reach a source."""
    import re
    text = pathlib.Path("config.yaml").read_text()
    text = re.sub(r"^sources:.*$", "sources: [fixture]", text, flags=re.M)
    text = re.sub(r"^(\s+)backend:.*$", r"\1backend: stub", text, flags=re.M)
    text = text.replace("recheck:\n", "recheck:\n  enabled: false\n", 1)
    (tmp_path / "config.yaml").write_text(text)
    base = dict(config=str(tmp_path / "config.yaml"), hunt=None, dry_run=False,
                no_score=True, no_images=True, due=True)
    base.update(kw)
    return type("A", (), base)()


def _run_count(cfg):
    store = Store(cfg.db_path)
    n = store.conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"]
    store.close()
    return n


def test_a_timer_tick_outside_the_window_does_nothing_at_all(tmp_path, monkeypatch):
    """Not "fetches but does not judge" -- nothing. Unlike the quota pause,
    this one stops collecting too, so it must leave no runs row behind."""
    from curbside.cli import cmd_once
    from curbside.config import load

    monkeypatch.setattr(Schedule, "now", lambda self: at(3))
    args = _args(tmp_path)
    assert cmd_once(args) == 0
    assert _run_count(load(args.config)) == 0


def test_a_hand_run_ignores_the_window(tmp_path, monkeypatch):
    """`--due` is the timer. A person at a keyboard asking for a pass gets one,
    because refusing would be obstinate rather than thrifty."""
    from curbside.cli import cmd_once
    from curbside.config import load

    monkeypatch.setattr(Schedule, "now", lambda self: at(3))
    args = _args(tmp_path, due=False)
    assert cmd_once(args) == 0
    assert _run_count(load(args.config)) > 0


def test_the_hours_control_says_how_long_the_window_actually_is():
    """"11pm to 8pm" is 21 hours awake, and reads like a night shift.

    A window that runs past midnight looks SHORT written down and is the
    near-opposite, so the one description the interface gave was the one that
    hid it. This is the same class of problem as a pause switch nobody can
    see: a control the user misreads is a control that lies.
    """
    from zoneinfo import ZoneInfo
    from curbside.schedule import Schedule
    tz = ZoneInfo("America/Denver")

    wrapped = Schedule(enabled=True, start_minute=23 * 60, end_minute=20 * 60, tz=tz)
    assert wrapped.awake_minutes == 21 * 60
    assert "21 hours" in wrapped.span_label
    assert "past midnight" in wrapped.span_label

    plain = Schedule(enabled=True, start_minute=11 * 60, end_minute=20 * 60, tz=tz)
    assert plain.awake_minutes == 9 * 60
    assert "9 hours" in plain.span_label
    assert "past midnight" not in plain.span_label

    # the two that are easy to get wrong at the boundary
    night = Schedule(enabled=True, start_minute=20 * 60, end_minute=6 * 60, tz=tz)
    assert night.awake_minutes == 10 * 60
    same = Schedule(enabled=True, start_minute=9 * 60, end_minute=9 * 60, tz=tz)
    assert same.awake_minutes == 24 * 60, "start == end is all day, not zero"

    always = Schedule(enabled=False, start_minute=0, end_minute=0, tz=tz)
    assert always.span_label == "Always on."


def test_the_settings_page_shows_the_span(tmp_path):
    """It has to reach the page, not just exist on the object."""
    import shutil
    from fastapi.testclient import TestClient
    from curbside.config import load
    from curbside.db import Store
    from curbside.web.app import create_app
    from curbside import schedule as sched_mod

    shutil.copy("config.yaml", tmp_path / "config.yaml")
    cfg = load(tmp_path / "config.yaml")
    client = TestClient(create_app(cfg))
    sched_mod.save(Store(cfg.db_path), enabled=True,
                   start_minute=23 * 60, end_minute=20 * 60)
    body = client.get("/settings").text
    assert "Awake 21 hours a day." in body
    assert "runs past midnight" in body


# --- what a "day" is, for the spend ceiling and for the page ---------------

def test_a_day_starts_where_the_user_is_not_where_utc_is():
    """The daily spend ceiling used UTC midnight, which in Albuquerque is 6pm
    and so fell INSIDE the waking window every day of the year. The counter
    meant to cap a day's spending reset in the middle of the evening and
    handed the bot a second full allowance for the rest of it."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from curbside.schedule import local_day_start

    abq = ZoneInfo("America/Denver")
    # 10:25 on a Monday morning, which is when this was noticed.
    now = datetime(2026, 9, 14, 16, 25, tzinfo=timezone.utc)
    start = local_day_start(abq, now)

    assert start == datetime(2026, 9, 14, 6, 0, tzinfo=timezone.utc)
    assert start.astimezone(abq).hour == 0
    # the old boundary was 6pm the previous evening, local
    old = now.replace(hour=0, minute=0)
    assert old.astimezone(abq).hour == 18
    assert old.astimezone(abq).day == 13


def test_the_day_boundary_follows_daylight_saving():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from curbside.schedule import local_day_start

    abq = ZoneInfo("America/Denver")
    summer = local_day_start(abq, datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc))
    winter = local_day_start(abq, datetime(2026, 12, 1, 20, 0, tzinfo=timezone.utc))
    assert summer.hour == 6            # MDT, UTC-6
    assert winter.hour == 7            # MST, UTC-7
    for start, zone in ((summer, abq), (winter, abq)):
        assert start.astimezone(zone).hour == 0


def test_no_timezone_falls_back_to_the_machine_rather_than_to_utc():
    """Failing open, like everything else here. A bot with no zone configured
    is one running on the machine's clock, which is what it did before the
    zone was configurable at all."""
    from datetime import datetime, timezone
    from curbside.schedule import local_day_start
    now = datetime(2026, 9, 14, 16, 25, tzinfo=timezone.utc)
    assert local_day_start(None, now).astimezone().hour == 0
