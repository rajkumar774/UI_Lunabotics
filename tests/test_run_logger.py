"""Tests for per-run logging.

A run is 25 minutes of a team's year. These cover the parts that decide
whether the log is worth having afterwards: that a paused clock does not split
one run into several, that a reset leaves nothing behind to be mistaken for a
real run, and that a disk failure at the end does not take the UI with it.
"""

from pathlib import Path

import pytest

from mission_control.core.run_logger import RunLogger
from mission_control.telemetry.state import (
    BatteryData, ConnectionState, DriveData, IMUData, RoverState,
)


def make_state(pct=80.0, volts=24.5, amps=0.8, watts=19.6, wh=200.0, speed=0.4):
    return RoverState(
        connection_state=ConnectionState.CONNECTED,
        telemetry_valid=True,
        battery=BatteryData(voltage=volts, current_amps=amps, percentage=pct,
                            power_watts=watts, energy_wh=wh),
        drive=DriveData(linear_velocity=speed, angular_velocity=0.1),
        imu=IMUData(roll_deg=-1.2, pitch_deg=2.0, yaw_deg=130.0),
    )


class FakeClock:
    """A clock the test drives, so throttling is deterministic."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def rl(tmp_path, clock):
    return RunLogger(output_dir=tmp_path, sample_hz=2.0, time_source=clock)


def test_a_finished_run_writes_its_three_files(rl, tmp_path, clock):
    rl.start()
    for pct in (90.0, 80.0, 70.0):
        rl.record_state(make_state(pct=pct))
        clock.advance(1.0)
    folder = rl.finish("test")

    assert folder is not None and folder.exists()
    assert (folder / "telemetry.csv").exists()
    assert (folder / "events.log").exists()
    assert (folder / "summary.txt").exists()
    assert folder.name.startswith("run_")

    summary = (folder / "summary.txt").read_text()
    assert "90% -> 70%" in summary, "battery start/end must be in the summary"


def test_pausing_does_not_split_one_run_in_two(rl, clock):
    """The operator may pause and resume; that is still ONE run."""
    rl.start()
    rl.record_state(make_state(pct=90.0))
    clock.advance(1.0)
    rl.start()                      # resume -- must NOT reset the timeline
    rl.record_state(make_state(pct=50.0))
    folder = rl.finish("test")

    rows = (folder / "telemetry.csv").read_text().strip().splitlines()
    assert len(rows) == 3, "header + both samples; the first must survive resume"


def test_reset_leaves_nothing_behind(rl, tmp_path):
    """A discarded run must not leave a folder to be mistaken for a real one."""
    rl.start()
    rl.record_state(make_state())
    rl.discard()

    assert rl.finish("test") is None
    assert list(tmp_path.iterdir()) == []


def test_a_run_with_no_telemetry_writes_nothing(rl, tmp_path):
    rl.start()
    assert rl.finish("test") is None
    assert list(tmp_path.iterdir()) == []


def test_repeated_alerts_are_recorded_once(rl, clock):
    """The rover republishes the same DiagnosticArray continuously; 3000
    copies of one warning would bury everything else."""
    rl.start()
    for _ in range(5):
        state = make_state()
        state.diagnostics.active_alerts = ["power: battery low"]
        rl.record_state(state)
        clock.advance(1.0)
    folder = rl.finish("test")

    events = (folder / "events.log").read_text()
    assert events.count("power: battery low") == 1


def test_commands_are_recorded_with_their_outcome(rl, clock):
    rl.start()
    rl.record_state(make_state())
    rl.record_command("EXCAVATE", True)
    rl.record_command("DUMP", False)
    folder = rl.finish("test")

    events = (folder / "events.log").read_text()
    assert "EXCAVATE -> OK" in events
    assert "DUMP -> FAILED" in events


def test_an_unwritable_folder_does_not_raise(tmp_path, clock):
    """A disk problem at the end of a run must not take the UI down."""
    target = tmp_path / "blocked"
    target.write_text("I am a file, not a directory")

    rl = RunLogger(output_dir=target / "runs", sample_hz=2.0, time_source=clock)
    rl.start()
    rl.record_state(make_state())
    assert rl.finish("test") is None       # reported, not raised


def test_sampling_is_throttled(tmp_path, clock):
    """45,000 rows nobody reads is not a log."""
    rl = RunLogger(output_dir=tmp_path, sample_hz=2.0, time_source=clock)
    rl.start()
    for _ in range(50):          # 50 snapshots inside one second
        rl.record_state(make_state())
        clock.advance(0.02)
    folder = rl.finish("test")

    rows = (folder / "telemetry.csv").read_text().strip().splitlines()
    assert len(rows) <= 3, "50 rapid snapshots must collapse to about one sample"
