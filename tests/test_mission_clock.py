"""Tests for the mission run countdown clock."""

import time

import pytest
from PySide6.QtWidgets import QApplication

from mission_control.core.mission_clock import MissionClock, format_mmss


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_format_mmss_covers_the_run_range():
    assert format_mmss(1500) == "25:00"  # official 25-minute run (Guidebook v3)
    assert format_mmss(1800) == "30:00"
    assert format_mmss(605) == "10:05"
    assert format_mmss(59) == "00:59"
    assert format_mmss(0) == "00:00"
    assert format_mmss(-5) == "00:00"


def test_clock_starts_at_the_configured_duration(qapp):
    clock = MissionClock(duration_seconds=1800)

    assert clock.duration_seconds == 1800
    assert clock.remaining_seconds == 1800
    assert clock.formatted() == "30:00"
    assert clock.is_running is False
    assert clock.is_finished is False


def test_start_and_pause_toggle_running_state(qapp):
    clock = MissionClock(duration_seconds=60)
    running_states = []
    clock.running_changed.connect(running_states.append)

    assert clock.start() is True
    assert clock.is_running is True

    assert clock.pause() is True
    assert clock.is_running is False
    assert running_states == [True, False]

    # Redundant calls are no-ops rather than errors.
    assert clock.pause() is False


def test_toggle_alternates_between_running_and_paused(qapp):
    clock = MissionClock(duration_seconds=60)

    clock.toggle()
    assert clock.is_running is True
    clock.toggle()
    assert clock.is_running is False


def test_pause_preserves_remaining_time(qapp):
    clock = MissionClock(duration_seconds=1800)
    clock.start()
    clock.set_remaining_seconds(742)
    clock.pause()

    assert clock.remaining_seconds == 742
    assert clock.formatted() == "12:22"


def test_clock_counts_down_in_real_time(qapp):
    """The timer must actually drive the countdown, not just hold a value."""
    clock = MissionClock(duration_seconds=3, tick_interval_ms=20)
    ticks = []
    clock.tick.connect(ticks.append)
    clock.start()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and clock.remaining_seconds == 3:
        qapp.processEvents()
        time.sleep(0.02)

    assert clock.remaining_seconds < 3
    assert ticks, "expected at least one countdown tick"
    clock.pause()


def test_reaching_zero_finishes_the_run_once(qapp):
    clock = MissionClock(duration_seconds=1800)
    finished_count = []
    clock.finished.connect(lambda: finished_count.append(1))
    clock.start()

    clock.set_remaining_seconds(0)

    assert clock.remaining_seconds == 0
    assert clock.formatted() == "00:00"
    assert clock.is_finished is True
    assert clock.is_running is False
    assert len(finished_count) == 1

    # An exhausted clock cannot be restarted without a reset.
    assert clock.start() is False
    assert len(finished_count) == 1


def test_reset_restores_a_finished_clock(qapp):
    clock = MissionClock(duration_seconds=1800)
    clock.start()
    clock.set_remaining_seconds(0)

    clock.reset()

    assert clock.remaining_seconds == 1800
    assert clock.formatted() == "30:00"
    assert clock.is_finished is False
    assert clock.is_running is False
    assert clock.start() is True
    clock.pause()


def test_ticks_are_emitted_only_when_the_displayed_second_changes(qapp):
    """A 4 Hz timer must not push four identical repaints per second."""
    clock = MissionClock(duration_seconds=1800)
    ticks = []
    clock.tick.connect(ticks.append)

    clock.set_remaining_seconds(120)
    clock.set_remaining_seconds(120)
    clock.set_remaining_seconds(119)

    assert ticks == [120, 119]
