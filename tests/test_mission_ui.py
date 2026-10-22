"""Tests for the mission clock UI and its inline start/pause/reset controls.

The mission *phase* UI (header badge and phase-selector row) was removed; the
phase is still carried in RoverState and written by the rover on /mission/state,
it simply has no operator-facing widget. These tests cover the clock that stayed.
"""

import pytest
from PySide6.QtWidgets import QApplication

from mission_control.comms.mock_client import MockCommsClient
from mission_control.core.config import AppConfig, RoverConfig
from mission_control.core.event_bus import EventBus
from mission_control.telemetry.state import MissionPhase, RoverState, StateManager
from mission_control.ui.components.mission_controls_widget import (
    PAUSE_TEXT,
    START_TEXT,
    MissionControlsWidget,
)
from mission_control.ui.components.mission_status_widget import (
    COMPLETE_CAPTION,
    CLOCK_CAPTION,
    MissionStatusWidget,
)
from mission_control.ui.main_window import MainWindow


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def window(qapp):
    """A main window with its own isolated state manager and event bus."""
    client = MockCommsClient()
    client.start()
    manager = StateManager()
    bus = EventBus()
    win = MainWindow(AppConfig(), RoverConfig(), client, event_bus=bus, state_manager=manager)

    yield win, manager, bus

    client.stop()
    win.close()


# --- Status widget -------------------------------------------------------


def test_status_widget_shows_configured_duration(qapp):
    widget = MissionStatusWidget(duration_seconds=1800)

    assert widget.clock_label.text() == "30:00"
    assert widget.caption_label.text() == CLOCK_CAPTION
    assert widget.clock_label.property("severity") == "NORMAL"


def test_status_widget_escalates_as_time_runs_out(qapp):
    widget = MissionStatusWidget(duration_seconds=1800)

    widget.set_remaining(900)
    assert widget.clock_label.text() == "15:00"
    assert widget.clock_label.property("severity") == "NORMAL"

    widget.set_remaining(240)  # under 5 minutes
    assert widget.clock_label.property("severity") == "WARNING"

    widget.set_remaining(45)  # under 1 minute
    assert widget.clock_label.text() == "00:45"
    assert widget.clock_label.property("severity") == "ERROR"


def test_status_widget_reports_mission_complete(qapp):
    widget = MissionStatusWidget(duration_seconds=1800)
    widget.set_remaining(0)
    widget.set_complete(True)

    assert widget.caption_label.text() == COMPLETE_CAPTION
    assert widget.clock_label.text() == "00:00"
    assert widget.caption_label.property("severity") == "ERROR"
    assert widget.is_complete is True


def test_status_widget_carries_its_clock_controls(qapp):
    """Controls are inline in the header, so the window needs no extra row."""
    widget = MissionStatusWidget(duration_seconds=1800)

    assert widget.controls is not None
    widget.set_running(True)
    assert widget.controls.start_pause_btn.text() == PAUSE_TEXT


def test_status_widget_has_no_phase_badge(qapp):
    widget = MissionStatusWidget(duration_seconds=1800)
    assert not hasattr(widget, "phase_badge")
    assert not hasattr(widget, "set_phase")


# --- Clock controls ------------------------------------------------------


def test_controls_reflect_clock_running_state(qapp):
    controls = MissionControlsWidget()
    assert controls.start_pause_btn.text() == START_TEXT

    controls.set_running(True)
    assert controls.start_pause_btn.text() == PAUSE_TEXT

    controls.set_running(False)
    assert controls.start_pause_btn.text() == START_TEXT


def test_controls_emit_clock_requests(qapp):
    controls = MissionControlsWidget()
    events = []
    controls.start_pause_requested.connect(lambda: events.append("toggle"))
    controls.reset_requested.connect(lambda: events.append("reset"))

    controls.start_pause_btn.click()
    controls.reset_btn.click()

    assert events == ["toggle", "reset"]


def test_controls_offer_no_phase_selection(qapp):
    controls = MissionControlsWidget()
    assert not hasattr(controls, "phase_buttons")
    assert not hasattr(controls, "phase_requested")


# --- Main window integration ---------------------------------------------


def test_window_starts_at_the_configured_duration(window):
    win, manager, _ = window

    # 25 minutes per Guidebook v3 section 1.3 (was 30 in v1/v2).
    assert win.mission_status.clock_label.text() == "25:00"
    assert win.mission_clock.duration_seconds == AppConfig().mission.run_duration_seconds
    assert manager.get_snapshot().mission_phase == MissionPhase.PRE_CHECK


def test_window_clock_duration_follows_configuration(qapp):
    config = AppConfig()
    config.mission.run_duration_seconds = 600
    client = MockCommsClient()
    client.start()
    win = MainWindow(config, RoverConfig(), client, event_bus=EventBus())

    assert win.mission_status.clock_label.text() == "10:00"

    client.stop()
    win.close()


def test_start_button_runs_the_mission_clock(window):
    win, _, _ = window

    win.mission_controls.start_pause_btn.click()
    assert win.mission_clock.is_running is True
    assert win.mission_controls.start_pause_btn.text() == PAUSE_TEXT

    win.mission_controls.start_pause_btn.click()
    assert win.mission_clock.is_running is False
    assert win.mission_controls.start_pause_btn.text() == START_TEXT


def test_countdown_updates_the_header_display(window):
    win, _, _ = window
    win.mission_controls.start_pause_btn.click()

    win.mission_clock.set_remaining_seconds(1234)

    assert win.mission_status.clock_label.text() == "20:34"
    win.mission_clock.pause()


def test_expired_clock_shows_mission_complete_and_closes_the_run(window):
    win, manager, _ = window
    win.mission_controls.start_pause_btn.click()

    win.mission_clock.set_remaining_seconds(0)

    assert win.mission_status.caption_label.text() == COMPLETE_CAPTION
    assert win.mission_status.clock_label.text() == "00:00"
    # The phase still closes out in state even though nothing renders it.
    assert manager.get_snapshot().mission_phase == MissionPhase.POST_RUN
    assert win.mission_clock.is_running is False


def test_reset_returns_the_window_to_a_fresh_run(window):
    win, manager, _ = window
    win.mission_controls.start_pause_btn.click()
    win.mission_clock.set_remaining_seconds(0)

    win.mission_controls.reset_btn.click()

    assert win.mission_status.clock_label.text() == "25:00"
    assert win.mission_status.caption_label.text() == CLOCK_CAPTION
    assert win.mission_status.clock_label.property("severity") == "NORMAL"
    assert manager.get_snapshot().mission_phase == MissionPhase.PRE_CHECK
    assert win.mission_clock.is_finished is False


def test_phase_still_travels_in_state_without_a_widget(window):
    """The rover can still report a phase; it simply has no display."""
    win, manager, bus = window

    manager.set_mission_phase(MissionPhase.AUTONOMOUS)
    bus.telemetry_updated.emit(manager.get_snapshot())

    assert manager.get_snapshot().mission_phase == MissionPhase.AUTONOMOUS


def test_mission_ui_does_not_disturb_camera_or_telemetry(window):
    """Removing the phase row must leave camera and telemetry intact."""
    win, _, bus = window

    assert win.video_player is not None
    assert win.telemetry_panel is not None

    bus.telemetry_updated.emit(RoverState(mission_phase=MissionPhase.DUMP))

    assert win.telemetry_panel.power.charge_row.value() == "--"
    assert win.video_player.toggle_stream_btn.text() == "START STREAM"


# --- Per-run logging ------------------------------------------------------


def test_finished_run_writes_a_log_folder(qapp, tmp_path):
    """When the 25-minute clock reaches zero, the run is written to disk."""
    from mission_control.telemetry.state import RoverState

    config = AppConfig()
    config.mission.run_log_dir = str(tmp_path)
    client = MockCommsClient()
    client.start()
    win = MainWindow(config, RoverConfig(), client, event_bus=EventBus())

    win.mission_controls.start_pause_btn.click()       # starts recording
    win._on_telemetry_snapshot(RoverState(telemetry_valid=True))
    win.mission_clock.set_remaining_seconds(0)         # clock hits zero

    runs = list(tmp_path.glob("run_*"))
    assert len(runs) == 1, "a finished run must leave exactly one folder"
    assert (runs[0] / "summary.txt").exists()
    assert (runs[0] / "telemetry.csv").exists()

    client.stop()
    win.close()


def test_reset_before_the_end_writes_nothing(qapp, tmp_path):
    """An abandoned run must not leave a folder that looks like a real one."""
    from mission_control.telemetry.state import RoverState

    config = AppConfig()
    config.mission.run_log_dir = str(tmp_path)
    client = MockCommsClient()
    client.start()
    win = MainWindow(config, RoverConfig(), client, event_bus=EventBus())

    win.mission_controls.start_pause_btn.click()
    win._on_telemetry_snapshot(RoverState(telemetry_valid=True))
    win.mission_controls.reset_btn.click()

    assert list(tmp_path.glob("run_*")) == []

    client.stop()
    win.close()
