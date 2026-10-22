"""Tests for communication abstraction and mock client behavior."""

import pytest
from PySide6.QtWidgets import QApplication

from mission_control.comms.mock_client import MockCommsClient
from mission_control.comms.ros2.client import ROS2CommsClient, is_ros2_available
from mission_control.telemetry.state import ConnectionState, MissionMode

# Ensure QApplication exists for QObject signal testing
@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_mock_comms_lifecycle(qapp):
    """Verifies that MockCommsClient starts, connects, updates state, and disconnects."""
    client = MockCommsClient()
    assert client.is_connected is False
    assert client.connection_state == ConnectionState.DISCONNECTED

    # Start client
    started = client.start()
    assert started is True

    # Track emitted connection state signals
    emitted_states = []
    client.connection_state_changed.connect(lambda s: emitted_states.append(s))

    # Connect in mock mode
    client.connect_rover()
    assert client.is_connected is True
    assert client.connection_state == ConnectionState.MOCK_MODE
    assert ConnectionState.MOCK_MODE.value in emitted_states

    # Disconnect
    client.disconnect_rover()
    assert client.is_connected is False
    assert client.connection_state == ConnectionState.DISCONNECTED
    assert ConnectionState.DISCONNECTED.value in emitted_states

    # Clean stop
    client.stop()


def test_mock_commands(qapp):
    """Verifies that mock command dispatches succeed and trigger expected feedback."""
    client = MockCommsClient()
    client.start()
    client.connect_rover()

    # Track command feedback
    feedbacks = []
    client.command_feedback_received.connect(lambda cmd, success: feedbacks.append((cmd, success)))

    # Drive command
    drive_ok = client.send_drive_command(1.0, -0.5)
    assert drive_ok is True
    assert ("cmd_vel", True) in feedbacks

    # E-Stop command
    estop_ok = client.send_estop(True)
    assert estop_ok is True
    assert ("estop", True) in feedbacks

    # Mission Mode command
    mode_ok = client.send_mission_mode(MissionMode.MANUAL_TELEOP.value)
    assert mode_ok is True
    assert ("mission_mode", True) in feedbacks

    client.stop()


def test_ros2_client_graceful_degradation(qapp):
    """Verifies that ROS2CommsClient instantiates without crashing when rclpy is absent."""
    client = ROS2CommsClient()
    assert isinstance(client.ros_available, bool)

    # In environment without rclpy, start() should gracefully return False
    if not is_ros2_available():
        assert client.ros_available is False
        assert client.start() is False
        assert client.connect_rover() is False
