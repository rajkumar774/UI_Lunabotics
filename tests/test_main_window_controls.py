"""Tests for the redesigned main window layout and its control wiring."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from mission_control.comms.mock_client import MockCommsClient
from mission_control.core.config import AppConfig, load_rover_config
from mission_control.core.event_bus import EventBus
from mission_control.core.telemetry_bridge import TelemetryBridge
from mission_control.telemetry.state import MissionMode, StateManager
from mission_control.ui.components.autonomy_control_widget import MissionAction
from mission_control.ui.main_window import MainWindow

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def window(qapp):
    """A fully wired window on the mock backend, torn down after each test."""
    client = MockCommsClient()
    client.start()
    state_manager = StateManager()
    event_bus = EventBus()
    bridge = TelemetryBridge(client, state_manager, event_bus, refresh_fps=30)

    win = MainWindow(
        app_config=AppConfig(),
        rover_config=load_rover_config(CONFIG_DIR),
        comms_client=client,
        event_bus=event_bus,
        state_manager=state_manager,
    )
    yield win, client, bridge, state_manager

    win.close()
    client.stop()


# --- Layout ---------------------------------------------------------------

def test_control_column_is_present(window):
    win, *_ = window
    assert win.controls is not None
    assert win.command_status is not None
    assert win.jetson_terminal is not None


def test_main_camera_uses_configured_name(window):
    win, *_ = window
    assert win.video_player.camera_title.text() == win.rover_config.cameras.main.name


def test_pip_inset_configured_from_rover_config(window):
    win, *_ = window
    assert win.video_player.canvas.pip_enabled is True
    assert win.video_player.canvas._pip_label == win.rover_config.cameras.pip.name


def test_terminal_targets_configured_jetson(window):
    win, *_ = window
    assert win.rover_config.ssh_host in win.jetson_terminal.target_label.text()


# --- Control wiring -------------------------------------------------------

def test_control_column_has_no_mode_buttons(window):
    """Modes were removed from the operator surface; actions remain."""
    win, *_ = window
    assert not hasattr(win.controls, "mode_buttons")
    assert set(win.controls.action_buttons) == set(MissionAction)


def test_reported_mode_still_shows_in_status(window):
    """The rover's mode is still displayed, just not operator-settable."""
    win, client, bridge, state_manager = window
    client.connect_rover()

    client.send_mission_mode(MissionMode.EXCAVATION.value)
    bridge.publish_snapshot()

    assert win.command_status.mode_row.value() == MissionMode.EXCAVATION.value


def test_action_button_dispatches_through_the_binding(window):
    """The shipped bindings now run real launch files over SSH.

    They were mocks until the autonomy launch files existed on the Jetson;
    this test was what said so, and it changes with them.
    """
    win, client, bridge, state_manager = window
    client.connect_rover()
    ran = []
    win.action_dispatcher.ssh_runner = lambda cmd: ran.append(cmd) or True

    win.controls.action_buttons[MissionAction.ONE_CYCLE_AUTO].click()

    # Acknowledged and shown to the operator...
    assert MissionAction.ONE_CYCLE_AUTO.value in win.command_status.command_row.value()
    # ...and no longer flagged MOCK, because it is not one.
    assert "MOCK" not in win.command_status.command_row.value()
    # ...and the launch file actually reached the Jetson's shell.
    assert ran == ["ros2 launch rover_navigation full_autonomy.launch.py"]


def test_panel_no_longer_warns_about_mocks(window):
    """The MOCK banner must disappear once the bindings are real.

    Leaving it up would train the operator to ignore it, which is exactly
    when it next needs to be believed.
    """
    win, *_ = window
    assert "MOCK" not in win.controls.actions_caption.text()
    assert win.controls.actions_caption.text() == "ACTIONS"


def test_switching_a_binding_to_topic_reaches_the_client(window):
    """Proves the migration path: flip the mode, nothing else changes."""
    from mission_control.core.config import ActionMode, MissionActionBinding

    win, client, bridge, state_manager = window
    client.connect_rover()
    win.action_dispatcher.bindings[MissionAction.DUMP.value] = MissionActionBinding(
        name=MissionAction.DUMP.value, mode=ActionMode.TOPIC, payload="DUMP"
    )

    win.controls.action_buttons[MissionAction.DUMP].click()
    assert client.last_mission_command == "DUMP"


def test_header_estop_stays_removed_sidebar_estop_returns(window):
    """Only the sidebar carries an E-Stop; the header badge stays gone."""
    win, *_ = window
    assert not hasattr(win, "estop_btn")
    assert not hasattr(win, "safety_badge")
    assert win.controls.estop_btn is not None


def test_estop_latch_comes_from_the_rover_not_the_button(window):
    """ENGAGED must reflect what the rover reports, never the press alone.

    The mock client echoes the stop back, which is why it latches here. On the
    real backend nothing is published at all, so nothing echoes and the button
    stays unlatched -- see test_station_publishes_nothing_to_the_rover, which
    pins that the publish path refuses.
    """
    from mission_control.telemetry.state import RoverState

    win, client, bridge, state_manager = window
    client.connect_rover()
    assert win.controls.estop_engaged is False

    # A rover-reported stop latches it...
    win._on_telemetry_snapshot(RoverState(telemetry_valid=True, is_estopped=True))
    assert win.controls.estop_engaged is True

    # ...and a rover-reported release clears it.
    win._on_telemetry_snapshot(RoverState(telemetry_valid=True, is_estopped=False))
    assert win.controls.estop_engaged is False


def test_rover_reported_estop_still_locks_the_actions(window):
    """Removing the button must not remove the latch: the rover reports
    E-Stop on /safety/estop, and the action buttons must stay locked out."""
    from mission_control.telemetry.state import RoverState

    win, client, bridge, state_manager = window
    client.connect_rover()
    assert any(b.isEnabled() for b in win.controls.action_buttons.values())

    win._on_telemetry_snapshot(RoverState(telemetry_valid=True, is_estopped=True))
    assert win.controls.estop_engaged is True
    assert all(not b.isEnabled() for b in win.controls.action_buttons.values())


def test_controls_disabled_until_connected(window):
    win, client, bridge, state_manager = window
    assert all(not b.isEnabled() for b in win.controls.action_buttons.values())

    client.connect_rover()
    assert all(b.isEnabled() for b in win.controls.action_buttons.values())

    client.disconnect_rover()
    assert all(not b.isEnabled() for b in win.controls.action_buttons.values())


def test_disconnect_clears_control_status(window):
    win, client, bridge, state_manager = window
    client.connect_rover()
    client.tick_telemetry(0.1)
    bridge.publish_snapshot()

    client.disconnect_rover()
    assert win.command_status.mode_row.value() == "--"


def test_malformed_snapshot_does_not_raise(window):
    win, *_ = window
    win._on_telemetry_snapshot(None)
    win._on_telemetry_snapshot({"not": "a state"})
    assert win.command_status.mode_row.value() == "--"


def test_close_shuts_down_terminal_session(window):
    win, client, bridge, state_manager = window
    win.close()
    assert win.jetson_terminal.connection is None


def test_header_has_a_jetson_indicator(window):
    """The shell link and the topic link fail independently, so both are shown."""
    win, *_ = window
    assert win.jetson_badge is not None
    assert win.jetson_badge.text() in ("OFFLINE", "WAITING", "CONNECTED")


def test_no_mission_phase_ui_remains(window):
    """The phase row and header badge were removed; the clock stayed."""
    win, *_ = window
    assert not hasattr(win.mission_status, "phase_badge")
    assert not hasattr(win.mission_controls, "phase_buttons")
    assert win.mission_status.clock_label.text() == "25:00"


def test_no_system_buttons_are_shipped(window):
    """SYSTEMS is empty: full_autonomy already brings the sensor stack up.

    LOCALIZATION used to live here, but full_autonomy.launch.py includes it
    (full_autonomy -> traverse -> rover_localization). Keeping both would let
    the operator start the stack twice and collide on /dev/video0.
    """
    win, *_ = window
    assert win.controls.system_buttons == {}


def test_no_camera_buttons_in_the_systems_row(window):
    win, *_ = window
    assert not any("CAM" in name.upper() for name in win.controls.system_buttons)


def test_localization_is_reached_through_full_autonomy_not_a_button(window):
    """Sensors come up via ONE CYCLE AUTO, which includes localization."""
    win, client, bridge, state_manager = window
    assert "LOCALIZATION" not in win.controls.system_buttons
    assert "LOCALIZATION" not in win.rover_config.mission_actions


# --- Localization indicator ----------------------------------------------

def test_localization_led_follows_tag_fixes(window):
    """The LED reports AprilTag fixes, and stays grey without telemetry."""
    import time
    from mission_control.telemetry.state import RoverState

    win, client, bridge, state_manager = window

    # No telemetry at all -> unknown, not a confident "not localized".
    win._on_telemetry_snapshot(RoverState())
    assert win.video_player._localized is None

    fresh = RoverState(telemetry_valid=True, last_tag_pose_time=time.time())
    win._on_telemetry_snapshot(fresh)
    assert win.video_player._localized is True

    stale = RoverState(telemetry_valid=True, last_tag_pose_time=time.time() - 60)
    win._on_telemetry_snapshot(stale)
    assert win.video_player._localized is False


def test_mission_actions_launch_the_real_files(window):
    """EXCAVATE / DUMP / ONE CYCLE AUTO run launch files over the SSH session."""
    win, client, bridge, state_manager = window
    client.connect_rover()
    ran = []
    win.action_dispatcher.ssh_runner = lambda cmd: ran.append(cmd) or True

    from mission_control.ui.components.autonomy_control_widget import MissionAction
    for action in (MissionAction.EXCAVATE, MissionAction.DUMP, MissionAction.ONE_CYCLE_AUTO):
        win.controls.action_buttons[action].click()

    assert ran == [
        "ros2 launch excavate_dump excavate_phase.launch.py",
        "ros2 launch excavate_dump dump_phase.launch.py",
        "ros2 launch rover_navigation full_autonomy.launch.py",
    ]
