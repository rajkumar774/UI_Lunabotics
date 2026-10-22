"""Rover link status card: connection, heartbeat health, latency, mission mode."""

from typing import Optional

from PySide6.QtWidgets import QWidget

from mission_control.telemetry.state import (
    ConnectionState,
    RoverState,
    Severity,
    connection_severity,
    latency_severity,
)
from mission_control.ui.components.telemetry_primitives import (
    NO_DATA_TEXT,
    MetricRow,
    TelemetryCard,
)


class ConnectionStatusWidget(TelemetryCard):
    """Displays link state, heartbeat health, link latency, and mission mode."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("Rover Link", parent)

        self.link_row = self.add_row(MetricRow("Connection", emphasis=True))
        self.heartbeat_row = self.add_row(MetricRow("Heartbeat", status=True))
        self.latency_row = self.add_row(MetricRow("Latency"))
        # "CTRL MODE" rather than "MODE": the header now shows the mission phase,
        # and this row reports the rover's control mode, which is a different thing.
        self.mode_row = self.add_row(MetricRow("Ctrl mode", status=True))

        self.show_no_data()

    def show_no_data(self) -> None:
        self.link_row.set_value(ConnectionState.DISCONNECTED.value, Severity.OFFLINE)
        self.heartbeat_row.set_value("OFFLINE", Severity.OFFLINE)
        self.latency_row.clear_value()
        self.mode_row.clear_value(NO_DATA_TEXT)

    def update_from_state(self, state: RoverState) -> None:
        link_severity = connection_severity(state)
        self.link_row.set_value(state.connection_state.value, link_severity)

        is_linked = state.connection_state in (
            ConnectionState.CONNECTED,
            ConnectionState.MOCK_MODE,
        )
        if not is_linked:
            self.heartbeat_row.set_value("OFFLINE", Severity.OFFLINE)
        elif state.is_stale:
            self.heartbeat_row.set_value("STALE", Severity.WARNING)
        elif state.telemetry_valid:
            self.heartbeat_row.set_value("HEALTHY", Severity.NORMAL)
        else:
            self.heartbeat_row.set_value("NO SIGNAL", Severity.WARNING)

        if state.has_telemetry and state.heartbeat_latency_ms > 0.0:
            self.latency_row.set_value(
                f"{state.heartbeat_latency_ms:.1f} ms",
                latency_severity(state.heartbeat_latency_ms),
            )
        else:
            self.latency_row.clear_value()

        if not is_linked:
            # Without a link the rover's mode is unknown, so report it as such
            # rather than leaving the last known mode on screen.
            self.mode_row.clear_value()
        else:
            mode_severity = Severity.ERROR if state.is_estopped else Severity.NORMAL
            self.mode_row.set_value(state.mission_mode.value, mode_severity)
