"""Deterministic Mock Communication Client for standalone Windows development."""

import time
from typing import Optional
from PySide6.QtCore import QObject, QTimer

from mission_control.comms.base_client import BaseCommsClient
from mission_control.core.logger import get_logger
from mission_control.telemetry.mock_generator import MockTelemetryGenerator
from mission_control.telemetry.state import ConnectionState, RoverState, MissionMode

logger = get_logger("mock_client")

DEFAULT_TELEMETRY_INTERVAL_MS = 50  # 20 Hz mock publish rate


class MockCommsClient(BaseCommsClient):
    """Mock client providing deterministic state and heartbeat updates for UI development
    without requiring a physical rover, Jetson, or ROS 2 network.

    Telemetry values come from MockTelemetryGenerator and are MOCK DATA — they are not
    measurements and do not represent any real rover subsystem.
    """

    def __init__(
        self,
        parent: Optional[QObject] = None,
        telemetry_interval_ms: int = DEFAULT_TELEMETRY_INTERVAL_MS,
        generator: Optional[MockTelemetryGenerator] = None,
    ):
        super().__init__(parent)
        self._heartbeat_timer: Optional[QTimer] = None
        self._telemetry_timer: Optional[QTimer] = None
        self._state = RoverState(connection_state=ConnectionState.DISCONNECTED)
        self._mock_latency_ms: float = 4.2
        self._telemetry_interval_ms = max(1, telemetry_interval_ms)
        self._generator = generator or MockTelemetryGenerator()
        self._last_mission_command: Optional[str] = None

    @property
    def generator(self) -> MockTelemetryGenerator:
        """The deterministic telemetry source backing this mock client."""
        return self._generator

    @property
    def telemetry_interval_ms(self) -> int:
        return self._telemetry_interval_ms

    def start(self) -> bool:
        """Initializes mock resources and timers."""
        logger.info("MockCommsClient started in standalone development mode.")
        self._ensure_timers()
        return True

    def stop(self) -> None:
        """Stops mock timers and clears active session."""
        logger.info("MockCommsClient stopping...")
        self._stop_timers()
        self._is_connected = False
        self._connection_state = ConnectionState.DISCONNECTED
        self.connection_state_changed.emit(self._connection_state.value)

    def connect_rover(self) -> bool:
        """Simulates establishing a connection in mock mode."""
        self._is_connected = True
        self._connection_state = ConnectionState.MOCK_MODE
        self._state.connection_state = ConnectionState.MOCK_MODE
        logger.info("MockCommsClient: Connected (MOCK MODE).")

        self.connection_state_changed.emit(self._connection_state.value)

        self._ensure_timers()
        self._generator.reset()

        if not self._heartbeat_timer.isActive():
            self._heartbeat_timer.start(500)  # 2 Hz heartbeat
        if not self._telemetry_timer.isActive():
            self._telemetry_timer.start(self._telemetry_interval_ms)

        return True

    def disconnect_rover(self) -> None:
        """Simulates disconnecting the session."""
        self._is_connected = False
        self._connection_state = ConnectionState.DISCONNECTED
        self._state.connection_state = ConnectionState.DISCONNECTED
        logger.info("MockCommsClient: Disconnected.")

        self._stop_timers()
        self.connection_state_changed.emit(self._connection_state.value)

    def send_drive_command(self, linear_x: float, angular_z: float) -> bool:
        """Simulates receiving a drive command."""
        logger.debug(f"MockCommsClient received drive command: linear_x={linear_x:.2f}, angular_z={angular_z:.2f}")
        self.command_feedback_received.emit("cmd_vel", True)
        return True

    def send_estop(self, enable: bool) -> bool:
        """Simulates emergency stop command dispatch."""
        self._state.is_estopped = enable
        if enable:
            self._state.mission_mode = MissionMode.EMERGENCY_STOP
            logger.warning("MockCommsClient: EMERGENCY STOP ACTIVATED.")
        else:
            self._state.mission_mode = MissionMode.IDLE
            logger.info("MockCommsClient: Emergency Stop reset.")

        # The mock stands in for the rover, so it reports the resulting state
        # back the same way a real rover would rather than the UI assuming it.
        self.estop_changed.emit(enable)
        self.mission_mode_changed.emit(self._state.mission_mode.value)
        self.command_feedback_received.emit("estop", True)
        return True

    def send_mission_mode(self, mode_name: str) -> bool:
        """Simulates requesting a mission mode transition."""
        try:
            mode = MissionMode(mode_name)
        except ValueError:
            logger.error(f"MockCommsClient: Invalid mission mode '{mode_name}'")
            self.command_feedback_received.emit("mission_mode", False)
            return False

        self._state.mission_mode = mode
        logger.info(f"MockCommsClient: Mission mode changed to {mode_name}")
        self.mission_mode_changed.emit(mode.value)
        self.command_feedback_received.emit("mission_mode", True)
        return True

    def send_mission_command(self, command: str) -> bool:
        """Records a discrete mission action without commanding anything.

        MOCK ONLY — the action is acknowledged and logged so the operator
        controls can be exercised, but no rover subsystem is driven.
        """
        self._last_mission_command = command
        logger.info(f"MockCommsClient: mission action '{command}' acknowledged (MOCK — no rover action).")
        self.command_feedback_received.emit(command, True)
        return True

    @property
    def last_mission_command(self) -> Optional[str]:
        """The most recent discrete action passed to send_mission_command()."""
        return self._last_mission_command

    def tick_telemetry(self, dt: Optional[float] = None) -> Optional[RoverState]:
        """Advances the mock generator by one step and emits a telemetry packet.

        Called by the internal timer while connected, and directly by tests so the
        telemetry profile can be stepped deterministically without waiting on timers.
        Returns the emitted packet, or None if the client is not connected.
        """
        if not self._is_connected:
            return None

        step = dt if dt is not None else self._telemetry_interval_ms / 1000.0
        packet = self._generator.advance(step)

        # Connection and safety fields stay owned by the client, not the generator.
        packet.connection_state = self._connection_state
        packet.mission_mode = self._state.mission_mode
        packet.is_estopped = self._state.is_estopped

        self._mock_latency_ms = packet.heartbeat_latency_ms
        self._state.heartbeat_latency_ms = packet.heartbeat_latency_ms
        self._state.last_heartbeat_time = packet.last_heartbeat_time

        self.telemetry_received.emit(packet)
        return packet

    def _ensure_timers(self) -> None:
        """Creates the heartbeat and telemetry timers if they do not yet exist."""
        if self._heartbeat_timer is None:
            self._heartbeat_timer = QTimer(self)
            self._heartbeat_timer.timeout.connect(self._on_heartbeat_tick)
        if self._telemetry_timer is None:
            self._telemetry_timer = QTimer(self)
            self._telemetry_timer.timeout.connect(self._on_telemetry_tick)

    def _stop_timers(self) -> None:
        for timer in (self._heartbeat_timer, self._telemetry_timer):
            if timer and timer.isActive():
                timer.stop()

    def _on_telemetry_tick(self) -> None:
        """Timer slot producing the periodic mock telemetry packet."""
        self.tick_telemetry()

    def _on_heartbeat_tick(self) -> None:
        """Simulates a periodic heartbeat packet from the rover."""
        if not self._is_connected:
            return
        self._state.last_heartbeat_time = time.time()
        self._state.heartbeat_latency_ms = self._mock_latency_ms
        self.heartbeat_received.emit(self._mock_latency_ms)
