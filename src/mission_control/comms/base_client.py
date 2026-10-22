"""Abstract Base Communication Client definition."""

from abc import ABC, abstractmethod
from PySide6.QtCore import QObject, Signal
from typing import Any, Dict, Optional

from mission_control.telemetry.state import ConnectionState, RoverState


class BaseCommsClient(QObject):
    """Abstract interface defining the communication contract between
    Mission Control and the Rover.
    
    Subclasses (MockCommsClient, ROS2CommsClient) must implement the abstract methods
    and emit Qt signals for connection, heartbeat, and telemetry events.
    """

    # Signals emitted to the UI / StateManager
    connection_state_changed = Signal(str)       # ConnectionState enum value
    heartbeat_received = Signal(float)          # Latency in milliseconds
    telemetry_received = Signal(object)         # RoverState snapshot or update dict
    command_feedback_received = Signal(str, bool) # (command_name, success)
    error_occurred = Signal(str)                # Error description string

    # Control-plane state. These travel separately from telemetry because
    # StateManager.apply_telemetry() deliberately refuses to let a telemetry
    # packet rewrite mode or safety state; the bridge routes these through the
    # dedicated set_mission_mode / set_mission_phase / set_estop setters.
    mission_mode_changed = Signal(str)          # MissionMode enum value
    mission_phase_changed = Signal(str)         # MissionPhase enum value
    estop_changed = Signal(bool)                # Latched E-Stop state

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._is_connected: bool = False
        self._connection_state: ConnectionState = ConnectionState.DISCONNECTED

    @property
    def is_connected(self) -> bool:
        """Returns True if a live communication session is active."""
        return self._is_connected

    @property
    def connection_state(self) -> ConnectionState:
        """Returns the current connection state."""
        return self._connection_state

    @abstractmethod
    def start(self) -> bool:
        """Initializes client resources and background workers."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stops client workers and releases all communication resources."""
        pass

    @abstractmethod
    def connect_rover(self) -> bool:
        """Initiates a connection attempt to the rover."""
        pass

    @abstractmethod
    def disconnect_rover(self) -> None:
        """Disconnects the active rover session."""
        pass

    @abstractmethod
    def send_drive_command(self, linear_x: float, angular_z: float) -> bool:
        """Sends velocity setpoints to the rover motion controller."""
        pass

    @abstractmethod
    def send_estop(self, enable: bool) -> bool:
        """Sends emergency stop command with highest transmission priority."""
        pass

    @abstractmethod
    def send_mission_mode(self, mode_name: str) -> bool:
        """Requests a mission mode transition on the rover."""
        pass

    def send_mission_command(self, command: str) -> bool:
        """Dispatches a discrete mission action (EXCAVATE / DUMP / CYCLE).

        Concrete rather than abstract so an existing client implementation keeps
        working without change; a backend that cannot dispatch actions reports
        the failure rather than silently appearing to succeed.
        """
        self.command_feedback_received.emit(command, False)
        return False
