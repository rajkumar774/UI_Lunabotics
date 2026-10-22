"""Centralized Qt Signal-based Event Bus for thread-safe decoupled event distribution."""

from PySide6.QtCore import QObject, Signal
from typing import Any, Dict


class EventBus(QObject):
    """Singleton-friendly Qt EventBus.
    
    Provides strongly typed Qt Signals for cross-thread event dispatching
    between communication workers, telemetry state, and UI components.
    """

    # Connection & Link signals
    connection_changed = Signal(bool, str)  # (is_connected, status_message)
    heartbeat_received = Signal(float)       # (timestamp_unix)
    heartbeat_lost = Signal()

    # Telemetry & State signals
    telemetry_updated = Signal(object)      # (RoverState snapshot)
    emergency_stop_triggered = Signal(bool) # (is_estopped)
    mission_state_changed = Signal(str)     # (state_name)

    # Diagnostic & Log signals
    log_message_emitted = Signal(str, str)  # (level, message)
    alert_triggered = Signal(str, str)      # (severity, alert_text)

    def __init__(self, parent=None):
        super().__init__(parent)


# Global instance for convenient application-wide access
_global_event_bus = None


def get_event_bus() -> EventBus:
    """Returns the application-wide EventBus instance."""
    global _global_event_bus
    if _global_event_bus is None:
        _global_event_bus = EventBus()
    return _global_event_bus
