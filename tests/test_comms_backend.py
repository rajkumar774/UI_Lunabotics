"""Tests for comms backend selection, control-plane state routing, and ROS config."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from mission_control.comms.mock_client import MockCommsClient
from mission_control.comms.ros2.client import ROS2CommsClient, is_ros2_available
from mission_control.core.app import MissionControlApp
from mission_control.core.config import (
    AppConfig,
    CommsBackend,
    CommsConfig,
    ROSTopicsConfig,
    load_app_config,
    load_rover_config,
)
from mission_control.core.event_bus import EventBus
from mission_control.core.telemetry_bridge import TelemetryBridge
from mission_control.telemetry.state import (
    ConnectionState,
    MissionMode,
    MissionPhase,
    StateManager,
)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def make_app(backend: CommsBackend, mock_mode: bool) -> MissionControlApp:
    """An app shell with configs preloaded, ready for _create_comms_client()."""
    app = MissionControlApp([])
    app.app_config = AppConfig(mock_mode=mock_mode, comms=CommsConfig(backend=backend))
    app.rover_config = load_rover_config(CONFIG_DIR)
    return app


# --- Configuration --------------------------------------------------------

def test_new_app_config_fields_load():
    cfg = load_app_config(CONFIG_DIR)
    assert isinstance(cfg.comms.backend, CommsBackend)
    assert cfg.telemetry.ros_publish_rate_hz > 0


def test_unknown_backend_falls_back_to_auto(tmp_path):
    """A typo in the config must not stop the station from starting."""
    (tmp_path / "app_config.yaml").write_text("comms:\n  backend: carrier-pigeon\n")
    assert load_app_config(tmp_path).comms.backend == CommsBackend.AUTO


def test_rover_config_exposes_new_topics():
    topics = load_rover_config(CONFIG_DIR).topics
    assert topics.battery.startswith("/")
    assert topics.mission_cmd.startswith("/")
    assert topics.autonomy_mode.startswith("/")


def test_camera_streams_load():
    cameras = load_rover_config(CONFIG_DIR).cameras
    assert cameras.main.name
    assert cameras.pip.name
    assert cameras.pip.corner in ("top_left", "top_right", "bottom_left", "bottom_right")
    assert 0.0 < cameras.pip.width_fraction <= 1.0


def test_blank_camera_uri_means_test_pattern():
    cameras = load_rover_config(CONFIG_DIR).cameras
    assert cameras.main.is_test_pattern == (not cameras.main.uri.strip())


def test_jetson_ssh_config_loads():
    rover = load_rover_config(CONFIG_DIR)
    assert rover.jetson_ssh.username
    assert rover.jetson_ssh.port > 0
    assert all(q.command for q in rover.jetson_ssh.quick_commands)


def test_ssh_host_falls_back_to_rover_ip(tmp_path):
    (tmp_path / "rover_config.yaml").write_text(
        "network:\n  rover_ip: 10.1.2.3\njetson_ssh:\n  host: ''\n  username: op\n"
    )
    rover = load_rover_config(tmp_path)
    assert rover.ssh_host == "10.1.2.3"


def test_malformed_quick_commands_are_skipped(tmp_path):
    (tmp_path / "rover_config.yaml").write_text(
        "jetson_ssh:\n"
        "  quick_commands:\n"
        "    - label: GOOD\n"
        "      command: ros2 topic list\n"
        "    - label: NO COMMAND\n"
        "    - 'just a string'\n"
    )
    commands = load_rover_config(tmp_path).jetson_ssh.quick_commands
    assert [c.label for c in commands] == ["GOOD"]


# --- Backend selection ----------------------------------------------------

def test_backend_mock_always_yields_mock_client(qapp):
    app = make_app(CommsBackend.MOCK, mock_mode=False)
    assert isinstance(app._create_comms_client(), MockCommsClient)


def test_auto_respects_mock_mode_flag(qapp):
    """mock_mode stays meaningful under 'auto' for back-compat."""
    app = make_app(CommsBackend.AUTO, mock_mode=True)
    assert isinstance(app._create_comms_client(), MockCommsClient)


def test_auto_uses_ros2_when_available_and_not_mocking(qapp):
    app = make_app(CommsBackend.AUTO, mock_mode=False)
    client = app._create_comms_client()
    expected = ROS2CommsClient if is_ros2_available() else MockCommsClient
    assert isinstance(client, expected)


def test_ros2_backend_degrades_to_mock_without_rclpy(qapp):
    """Without rclpy the station must still start, loudly, in mock mode."""
    app = make_app(CommsBackend.ROS2, mock_mode=False)
    client = app._create_comms_client()
    if is_ros2_available():
        assert isinstance(client, ROS2CommsClient)
    else:
        assert isinstance(client, MockCommsClient)


def test_ros2_client_receives_configured_topics(qapp):
    app = make_app(CommsBackend.ROS2, mock_mode=False)
    client = app._create_comms_client()
    if isinstance(client, ROS2CommsClient):
        assert client.topics.cmd_vel == app.rover_config.topics.cmd_vel
        assert client.topics.autonomy_mode == app.rover_config.topics.autonomy_mode


# --- Control-plane state routing -----------------------------------------

def bridge_for(client) -> tuple[TelemetryBridge, StateManager]:
    state_manager = StateManager()
    bridge = TelemetryBridge(client, state_manager, EventBus(), refresh_fps=30)
    return bridge, state_manager


def test_mission_mode_reaches_state_manager(qapp):
    """apply_telemetry refuses mode changes, so they must take the setter path."""
    client = MockCommsClient()
    bridge, state_manager = bridge_for(client)
    client.start()
    client.connect_rover()

    client.send_mission_mode(MissionMode.EXCAVATION.value)
    assert state_manager.get_snapshot().mission_mode == MissionMode.EXCAVATION

    client.stop()


def test_estop_reaches_state_manager_and_publishes_immediately(qapp):
    client = MockCommsClient()
    bridge, state_manager = bridge_for(client)
    published = []
    bridge.event_bus.telemetry_updated.connect(published.append)
    client.start()
    client.connect_rover()
    published.clear()

    client.send_estop(True)
    assert state_manager.get_snapshot().is_estopped is True
    assert published, "E-Stop must publish a snapshot without waiting for the refresh tick"

    client.send_estop(False)
    assert state_manager.get_snapshot().is_estopped is False

    client.stop()


def test_unknown_mode_value_is_ignored_by_bridge(qapp):
    client = MockCommsClient()
    bridge, state_manager = bridge_for(client)
    before = state_manager.get_snapshot().mission_mode

    client.mission_mode_changed.emit("WARP SPEED")
    assert state_manager.get_snapshot().mission_mode == before


def test_mission_phase_signal_routes_to_state(qapp):
    client = MockCommsClient()
    bridge, state_manager = bridge_for(client)

    client.mission_phase_changed.emit(MissionPhase.DUMP.value)
    assert state_manager.get_snapshot().mission_phase == MissionPhase.DUMP


# --- Mission actions ------------------------------------------------------

def test_mock_acknowledges_mission_actions(qapp):
    client = MockCommsClient()
    feedback = []
    client.command_feedback_received.connect(lambda c, ok: feedback.append((c, ok)))

    assert client.send_mission_command("EXCAVATE") is True
    assert ("EXCAVATE", True) in feedback
    assert client.last_mission_command == "EXCAVATE"


def test_ros2_client_refuses_commands_before_start(qapp):
    """A queued command with no node must fail loudly, not look dispatched."""
    client = ROS2CommsClient(topics=ROSTopicsConfig())
    feedback = []
    client.command_feedback_received.connect(lambda c, ok: feedback.append((c, ok)))

    assert client.send_mission_command("DUMP") is False
    assert client.send_drive_command(0.5, 0.0) is False
    assert ("DUMP", False) in feedback


def test_ros2_client_rejects_invalid_mission_mode(qapp):
    client = ROS2CommsClient(topics=ROSTopicsConfig())
    assert client.send_mission_mode("NOT A MODE") is False


def test_ros2_client_starts_disconnected(qapp):
    client = ROS2CommsClient(topics=ROSTopicsConfig())
    assert client.connection_state == ConnectionState.DISCONNECTED
    assert client.is_connected is False
    assert client.worker is None


# --- Sensor health: liveness vs diagnostics -------------------------------
#
# The SENSORS card reads /diagnostics. A perfectly healthy IMU publishing at
# 19 Hz still showed NO DATA, because nothing on this rover publishes an IMU
# DiagnosticStatus. Message liveness now fills that gap -- but only where the
# rover is silent, never over an explicit health report.


def _worker():
    """A worker instance without starting its ROS thread."""
    from mission_control.comms.ros2.worker import ROS2WorkerThread
    from mission_control.core.config import ROSTopicsConfig
    return ROS2WorkerThread(topics=ROSTopicsConfig())


def test_arriving_messages_mark_a_sensor_online(qapp):
    from mission_control.telemetry.state import SensorStatus

    w = _worker()
    assert w._state.sensors.imu is SensorStatus.UNKNOWN
    with w._lock:
        w._note_sensor_alive("imu")
    assert w._state.sensors.imu is SensorStatus.ONLINE


def test_a_reported_diagnostic_beats_liveness(qapp):
    """The rover saying DEGRADED must not be overwritten by traffic arriving.

    wheel_odom_node reports 'ESP32-2 encoders: no data received yet' while
    still publishing. Inferring ONLINE from that traffic would hide a fault
    the rover had explicitly flagged.
    """
    from mission_control.telemetry.state import SensorStatus

    w = _worker()
    w._diag_reported.add("encoders")
    w._state.sensors.encoders = SensorStatus.DEGRADED
    with w._lock:
        w._note_sensor_alive("encoders")
    assert w._state.sensors.encoders is SensorStatus.DEGRADED


def test_a_sensor_goes_offline_when_its_messages_stop(qapp):
    """One message must not keep a group ONLINE forever."""
    import time
    from mission_control.telemetry.state import SensorStatus

    w = _worker()
    with w._lock:
        w._note_sensor_alive("imu")
    assert w._state.sensors.imu is SensorStatus.ONLINE

    w._sensor_seen["imu"] = time.monotonic() - (w.SENSOR_LIVENESS_TIMEOUT_S + 1.0)
    w._check_sensor_liveness()
    assert w._state.sensors.imu is SensorStatus.OFFLINE


def test_station_publishes_nothing_to_the_rover(qapp):
    """Mission Control is subscribe-only: every publish path refuses.

    The four publishers (/cmd_vel, /safety/estop, /mission/cmd,
    /mission/autonomy_mode) were removed. _publish_command raises rather than
    dropping the command quietly, so the caller reports it as a FAILED command
    and the operator never assumes the rover acted.
    """
    import pytest as _pytest

    w = _worker()
    for kind, payload in (("cmd_vel", (0.5, 0.0)), ("estop", True),
                          ("mission_cmd", "EXCAVATE"), ("autonomy_mode", "IDLE")):
        with _pytest.raises(RuntimeError, match="subscribe-only"):
            w._publish_command(kind, payload)
