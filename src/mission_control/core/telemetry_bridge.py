"""Bridge between the communication layer, the StateManager, and the UI EventBus.

Inbound telemetry can arrive far faster than an operator display needs to repaint.
This bridge separates the two rates:

    comms client signals  ->  StateManager        (every packet, full rate)
    StateManager snapshot ->  EventBus.telemetry_updated  (throttled, ~30 FPS)

The UI never touches the comms client for telemetry and never owns rover state; it
only consumes the snapshots published on the EventBus. Swapping MockCommsClient for
a real ROS 2 client therefore requires no UI change at all.
"""

from typing import Optional

from PySide6.QtCore import QObject, QTimer

from mission_control.core.event_bus import EventBus, get_event_bus
from mission_control.core.logger import get_logger
from mission_control.comms.base_client import BaseCommsClient
from mission_control.telemetry.state import (
    ConnectionState,
    MissionMode,
    MissionPhase,
    RoverState,
    StateManager,
    DEFAULT_STALE_TIMEOUT_S,
)

logger = get_logger("telemetry_bridge")

DEFAULT_REFRESH_FPS = 30


class TelemetryBridge(QObject):
    """Routes comms telemetry into the StateManager and publishes throttled snapshots."""

    def __init__(
        self,
        comms_client: BaseCommsClient,
        state_manager: Optional[StateManager] = None,
        event_bus: Optional[EventBus] = None,
        refresh_fps: int = DEFAULT_REFRESH_FPS,
        stale_timeout_seconds: float = DEFAULT_STALE_TIMEOUT_S,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.comms_client = comms_client
        self.state_manager = state_manager or StateManager(stale_timeout_seconds=stale_timeout_seconds)
        self.event_bus = event_bus if event_bus is not None else get_event_bus()

        self._refresh_fps = max(1, refresh_fps)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(int(1000 / self._refresh_fps))
        self._refresh_timer.timeout.connect(self.publish_snapshot)

        self._packets_received = 0
        self._snapshots_published = 0

        self._connect_client_signals()

    @property
    def refresh_fps(self) -> int:
        return self._refresh_fps

    @property
    def packets_received(self) -> int:
        """Number of telemetry packets written into the StateManager."""
        return self._packets_received

    @property
    def snapshots_published(self) -> int:
        """Number of throttled snapshots published on the EventBus."""
        return self._snapshots_published

    @property
    def is_running(self) -> bool:
        return self._refresh_timer.isActive()

    def start(self) -> None:
        """Starts publishing throttled state snapshots to the EventBus."""
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()
            logger.info(f"TelemetryBridge publishing state snapshots at {self._refresh_fps} FPS.")
        self.publish_snapshot()

    def stop(self) -> None:
        """Stops snapshot publication."""
        if self._refresh_timer.isActive():
            self._refresh_timer.stop()
            logger.info("TelemetryBridge stopped.")

    def publish_snapshot(self) -> RoverState:
        """Reads the current state and publishes it on the EventBus."""
        snapshot = self.state_manager.get_snapshot()
        self._snapshots_published += 1
        self.event_bus.telemetry_updated.emit(snapshot)
        return snapshot

    # -- Comms client signal handlers ----------------------------------------

    def _connect_client_signals(self) -> None:
        self.comms_client.telemetry_received.connect(self._on_telemetry_received)
        self.comms_client.heartbeat_received.connect(self._on_heartbeat_received)
        self.comms_client.connection_state_changed.connect(self._on_connection_state_changed)
        self.comms_client.mission_mode_changed.connect(self._on_mission_mode_changed)
        self.comms_client.mission_phase_changed.connect(self._on_mission_phase_changed)
        self.comms_client.estop_changed.connect(self._on_estop_changed)

    def _on_mission_mode_changed(self, mode_value: str) -> None:
        """Writes a rover-reported control mode through its dedicated setter."""
        try:
            mode = MissionMode(mode_value)
        except ValueError:
            logger.warning(f"TelemetryBridge received unknown mission mode: {mode_value}")
            return
        self.state_manager.set_mission_mode(mode)

    def _on_mission_phase_changed(self, phase_value: str) -> None:
        """Writes a rover-reported run phase through its dedicated setter."""
        try:
            phase = MissionPhase(phase_value)
        except ValueError:
            logger.warning(f"TelemetryBridge received unknown mission phase: {phase_value}")
            return
        self.state_manager.set_mission_phase(phase)

    def _on_estop_changed(self, engaged: bool) -> None:
        """Safety state is operator-critical: write it and publish immediately."""
        self.state_manager.set_estop(bool(engaged))
        self.event_bus.emergency_stop_triggered.emit(bool(engaged))
        self.publish_snapshot()

    def _on_telemetry_received(self, packet) -> None:
        """Writes an inbound telemetry packet into the single source of truth."""
        if not isinstance(packet, RoverState):
            logger.debug(f"TelemetryBridge ignored non-RoverState telemetry payload: {type(packet)}")
            return
        self.state_manager.apply_telemetry(packet)
        self._packets_received += 1

    def _on_heartbeat_received(self, latency_ms: float) -> None:
        self.state_manager.update_heartbeat(latency_ms)
        self.event_bus.heartbeat_received.emit(latency_ms)

    def _on_connection_state_changed(self, state_value: str) -> None:
        """Applies link transitions and immediately refreshes the UI."""
        try:
            state = ConnectionState(state_value)
        except ValueError:
            logger.warning(f"TelemetryBridge received unknown connection state: {state_value}")
            return

        self.state_manager.set_connection_state(state)
        if state in (ConnectionState.DISCONNECTED, ConnectionState.LOST_LINK):
            self.state_manager.invalidate_telemetry()

        is_connected = state in (ConnectionState.CONNECTED, ConnectionState.MOCK_MODE)
        self.event_bus.connection_changed.emit(is_connected, state.value)

        # Link changes are operator-critical: publish without waiting for the tick.
        self.publish_snapshot()
