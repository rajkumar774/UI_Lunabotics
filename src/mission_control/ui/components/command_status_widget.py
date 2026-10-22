"""Command and safety status block beneath the operator controls.

Answers the question the mode and action buttons cannot: what the rover is
actually in, whether the last command was acknowledged, and whether the link
carrying those commands is alive. Every value is rendered from a snapshot or
from an explicit command acknowledgement — the widget never infers success from
the fact that a button was pressed.
"""

import time
from typing import Optional

from PySide6.QtWidgets import QSizePolicy, QWidget

from mission_control.telemetry.state import (
    ConnectionState,
    MissionMode,
    RoverState,
    Severity,
    connection_severity,
)
from mission_control.ui.components.telemetry_primitives import (
    NO_DATA_TEXT,
    MetricRow,
    TelemetryCard,
)

#: An acknowledgement older than this is no longer described as recent.
ACK_FRESH_WINDOW_S = 5.0


class CommandStatusWidget(TelemetryCard):
    """CONTROL MODE / E-STOP / LAST COMMAND / COMMAND LINK readout."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("CONTROL STATUS", parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

        self.mode_row = self.add_row(MetricRow("CTRL MODE", emphasis=True))
        self.estop_row = self.add_row(MetricRow("E-STOP", status=True))
        self.command_row = self.add_row(MetricRow("LAST COMMAND", status=True))
        self.link_row = self.add_row(MetricRow("COMMAND LINK", status=True))

        self._last_command: Optional[str] = None
        self._last_command_ok: bool = False
        self._last_command_time: float = 0.0

        self.show_no_data()

    # -- Rendering -----------------------------------------------------------

    def update_from_state(self, state: RoverState) -> None:
        """Renders a rover state snapshot.

        Control mode and E-Stop are control-plane state written through
        StateManager's own setters, not values carried on the telemetry stream,
        so they are gated on the link rather than on telemetry freshness.
        """
        if not isinstance(state, RoverState):
            self.show_no_data()
            return

        self.link_row.set_value(state.connection_state.value, connection_severity(state))

        linked = state.connection_state in (ConnectionState.CONNECTED, ConnectionState.MOCK_MODE)
        self._render_estop(state, linked)
        self._render_mode(state, linked)
        self._render_last_command()

    def _render_estop(self, state: RoverState, linked: bool) -> None:
        """An engaged stop always shows, even stale — the reverse is unsafe."""
        if state.is_estopped:
            self.estop_row.set_value("ENGAGED", Severity.ERROR)
        elif not linked:
            self.estop_row.clear_value(NO_DATA_TEXT)
        elif state.has_telemetry:
            self.estop_row.set_value("CLEAR", Severity.NORMAL)
        else:
            # Linked but nothing fresh to confirm against: say so rather than
            # asserting CLEAR, which an operator would act on.
            self.estop_row.set_value("UNCONFIRMED", Severity.WARNING)

    def _render_mode(self, state: RoverState, linked: bool) -> None:
        if not linked:
            self.mode_row.clear_value(NO_DATA_TEXT)
            return
        severity = (
            Severity.ERROR
            if state.mission_mode == MissionMode.EMERGENCY_STOP
            else Severity.NORMAL
        )
        self.mode_row.set_value(state.mission_mode.value, severity)

    def record_command(self, command: str, success: bool) -> None:
        """Records a command acknowledgement from the comms client."""
        self._last_command = command
        self._last_command_ok = bool(success)
        self._last_command_time = time.time()
        self._render_last_command()

    def _render_last_command(self) -> None:
        if self._last_command is None:
            self.command_row.clear_value("NONE")
            return

        age = time.time() - self._last_command_time
        if self._last_command_ok:
            suffix = "SENT" if age <= ACK_FRESH_WINDOW_S else "OK"
            self.command_row.set_value(f"{self._last_command} · {suffix}", Severity.NORMAL)
        else:
            self.command_row.set_value(f"{self._last_command} · FAILED", Severity.ERROR)

    def show_no_data(self) -> None:
        """Resets to the safe no-telemetry presentation."""
        self.mode_row.clear_value(NO_DATA_TEXT)
        self.estop_row.clear_value(NO_DATA_TEXT)
        self.link_row.set_value(ConnectionState.DISCONNECTED.value, Severity.OFFLINE)
        self._render_last_command()

    @property
    def last_command(self) -> Optional[str]:
        return self._last_command
