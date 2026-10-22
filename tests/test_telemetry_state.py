"""Tests for rover state containers and StateManager."""

import pytest
from mission_control.telemetry.state import (
    RoverState,
    StateManager,
    ConnectionState,
    MissionMode,
    MissionPhase,
    IMUData,
    BatteryData,
)


def test_rover_state_defaults():
    """Verifies that RoverState initializes with safe disconnected defaults."""
    state = RoverState()
    assert state.connection_state == ConnectionState.DISCONNECTED
    assert state.mission_mode == MissionMode.IDLE
    assert state.is_estopped is False
    assert state.heartbeat_latency_ms == 0.0
    assert isinstance(state.imu, IMUData)
    assert isinstance(state.battery, BatteryData)


def test_state_manager_snapshots():
    """Verifies that StateManager returns independent state snapshots."""
    manager = StateManager()

    snap1 = manager.get_snapshot()
    assert snap1.connection_state == ConnectionState.DISCONNECTED

    # Modify state via manager
    manager.set_connection_state(ConnectionState.MOCK_MODE)
    manager.set_mission_mode(MissionMode.MANUAL_TELEOP)
    manager.set_estop(True)

    snap2 = manager.get_snapshot()
    assert snap2.connection_state == ConnectionState.MOCK_MODE
    assert snap2.mission_mode == MissionMode.EMERGENCY_STOP
    assert snap2.is_estopped is True

    # Ensure previous snapshot is unaffected
    assert snap1.connection_state == ConnectionState.DISCONNECTED
    assert snap1.is_estopped is False


def test_state_manager_tracks_mission_phase():
    """Mission phase is application state, readable through ordinary snapshots."""
    manager = StateManager()
    assert manager.get_snapshot().mission_phase == MissionPhase.PRE_CHECK

    manager.set_mission_phase(MissionPhase.AUTONOMOUS)
    assert manager.get_snapshot().mission_phase == MissionPhase.AUTONOMOUS


def test_telemetry_packets_do_not_overwrite_mission_phase():
    """An inbound packet carries sensor data only; it must not reset the timeline."""
    manager = StateManager()
    manager.set_mission_phase(MissionPhase.DUMP)

    manager.apply_telemetry(RoverState(mission_phase=MissionPhase.PRE_CHECK))

    assert manager.get_snapshot().mission_phase == MissionPhase.DUMP


def test_state_manager_heartbeat():
    """Verifies heartbeat updates."""
    manager = StateManager()
    manager.update_heartbeat(latency_ms=8.5)

    snap = manager.get_snapshot()
    assert snap.heartbeat_latency_ms == 8.5
    assert snap.last_heartbeat_time > 0.0
