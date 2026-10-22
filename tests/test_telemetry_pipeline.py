"""Tests for the telemetry data path: MockCommsClient -> StateManager -> EventBus."""

import time

import pytest
from PySide6.QtWidgets import QApplication

from mission_control.comms.mock_client import MockCommsClient
from mission_control.core.event_bus import EventBus
from mission_control.core.telemetry_bridge import TelemetryBridge
from mission_control.telemetry.state import (
    ConnectionState,
    RoverState,
    StateManager,
)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def wired(qapp):
    """A mock client wired to an isolated StateManager/EventBus through the bridge."""
    client = MockCommsClient()
    client.start()
    bus = EventBus()
    manager = StateManager(stale_timeout_seconds=2.0)
    bridge = TelemetryBridge(
        comms_client=client,
        state_manager=manager,
        event_bus=bus,
    )
    snapshots: list[RoverState] = []
    bus.telemetry_updated.connect(snapshots.append)

    yield client, manager, bus, bridge, snapshots

    bridge.stop()
    client.stop()


def test_mock_client_emits_populated_telemetry(wired):
    """A connected mock client publishes RoverState packets carrying real values."""
    client, _, _, _, _ = wired
    received: list[RoverState] = []
    client.telemetry_received.connect(received.append)

    client.connect_rover()
    packet = client.tick_telemetry(0.5)

    assert isinstance(packet, RoverState)
    assert received and received[-1] is packet
    assert packet.battery.percentage > 0.0
    assert packet.drive.linear_velocity != 0.0
    assert packet.connection_state == ConnectionState.MOCK_MODE


def test_mock_client_publishes_nothing_while_disconnected(wired):
    client, _, _, bridge, _ = wired
    assert client.tick_telemetry(0.5) is None
    assert bridge.packets_received == 0


def test_telemetry_reaches_state_manager(wired):
    """Every packet must land in the StateManager, the single source of truth."""
    client, manager, _, bridge, _ = wired
    client.connect_rover()

    for _ in range(3):
        client.tick_telemetry(0.5)

    assert bridge.packets_received == 3
    snapshot = manager.get_snapshot()
    assert snapshot.telemetry_valid is True
    assert snapshot.has_telemetry is True
    assert snapshot.battery.percentage > 0.0
    assert snapshot.sensors.imu.value == "ONLINE"


def test_state_manager_snapshots_are_independent(wired):
    """A snapshot handed to the UI must not alias the managed state."""
    client, manager, _, _, _ = wired
    client.connect_rover()
    client.tick_telemetry(0.5)

    snapshot = manager.get_snapshot()
    original = snapshot.battery.percentage
    snapshot.battery.percentage = 1.0

    assert manager.get_snapshot().battery.percentage == pytest.approx(original)


def test_event_bus_publishes_state_snapshots(wired):
    """The UI-facing EventBus signal carries the merged state, not the raw packet."""
    client, _, _, bridge, snapshots = wired
    client.connect_rover()
    client.tick_telemetry(0.5)

    published = bridge.publish_snapshot()

    assert snapshots and snapshots[-1] is published
    assert isinstance(published, RoverState)
    assert published.connection_state == ConnectionState.MOCK_MODE
    assert published.battery.voltage > 0.0


def test_ui_updates_are_throttled_below_the_telemetry_rate(wired):
    """Many inbound packets must not translate into many UI refreshes."""
    client, _, _, bridge, snapshots = wired

    client.connect_rover()  # link changes publish immediately
    baseline = len(snapshots)

    for _ in range(40):
        client.tick_telemetry(0.05)

    # 40 packets stored, zero extra repaints until the refresh tick fires.
    assert bridge.packets_received == 40
    assert len(snapshots) == baseline

    bridge.publish_snapshot()
    assert len(snapshots) == baseline + 1


def test_disconnect_invalidates_telemetry(wired):
    """Dropping the link must clear values rather than freeze stale ones on screen."""
    client, manager, _, _, snapshots = wired
    client.connect_rover()
    client.tick_telemetry(0.5)
    assert manager.get_snapshot().has_telemetry is True

    client.disconnect_rover()

    snapshot = manager.get_snapshot()
    assert snapshot.connection_state == ConnectionState.DISCONNECTED
    assert snapshot.telemetry_valid is False
    assert snapshot.has_telemetry is False
    assert snapshot.battery.percentage == 0.0
    assert snapshot.heartbeat_latency_ms == 0.0
    # The operator sees the change immediately, without waiting for a tick.
    assert snapshots[-1].connection_state == ConnectionState.DISCONNECTED


def test_stale_telemetry_is_detected_without_new_packets(qapp):
    """Telemetry that stops arriving must be reported stale on the next read."""
    manager = StateManager(stale_timeout_seconds=0.05)
    client = MockCommsClient()
    client.start()
    client.connect_rover()
    manager.apply_telemetry(client.tick_telemetry(0.5))
    client.stop()

    assert manager.get_snapshot().is_stale is False

    time.sleep(0.12)
    stale = manager.get_snapshot()
    assert stale.is_stale is True
    assert stale.has_telemetry is False


def test_bridge_ignores_malformed_payloads(wired):
    """A non-RoverState payload must be dropped, not crash the pipeline."""
    client, manager, _, bridge, _ = wired
    client.connect_rover()

    client.telemetry_received.emit("not-a-rover-state")
    client.telemetry_received.emit(None)

    assert bridge.packets_received == 0
    assert manager.get_snapshot().telemetry_valid is False


def test_heartbeat_latency_reaches_the_state_manager(wired):
    client, manager, _, _, _ = wired
    client.connect_rover()

    client.heartbeat_received.emit(12.5)

    assert manager.get_snapshot().heartbeat_latency_ms == pytest.approx(12.5)
