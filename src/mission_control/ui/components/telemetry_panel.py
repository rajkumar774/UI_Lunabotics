"""Telemetry strip assembling the operator telemetry cards into one row."""

from typing import Optional

from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QWidget

from mission_control.telemetry.state import RoverState
from mission_control.ui.components.attitude_widget import AttitudeWidget
from mission_control.ui.components.connection_widget import ConnectionStatusWidget
from mission_control.ui.components.motion_widget import MotionWidget
from mission_control.ui.components.power_widget import PowerWidget
from mission_control.ui.components.sensor_status_widget import SensorStatusWidget


class TelemetryPanel(QWidget):
    """Single entry point the main window uses to render a rover state snapshot.

    The panel owns no state of its own: every repaint is driven by a snapshot handed
    in from the StateManager via the EventBus.
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("TelemetryPanel")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.power = PowerWidget()
        self.motion = MotionWidget()
        self.attitude = AttitudeWidget()
        self.sensors = SensorStatusWidget()

        # The ROVER LINK card was removed. Link state still reaches the
        # operator: the JETSON badge covers the shell, and every card here
        # falls back to NO DATA when telemetry stops -- which says the same
        # thing in the place the operator is already looking.
        self._cards = (self.power, self.motion, self.attitude, self.sensors)
        for card in self._cards:
            layout.addWidget(card, stretch=1)

    @property
    def cards(self) -> tuple:
        return self._cards

    def update_from_state(self, state: Optional[RoverState]) -> None:
        """Refreshes every card from a state snapshot; a missing snapshot is safe."""
        if not isinstance(state, RoverState):
            self.show_no_data()
            return
        for card in self._cards:
            card.update_from_state(state)

    def show_no_data(self) -> None:
        """Renders the safe fallback appearance for every card."""
        for card in self._cards:
            card.show_no_data()
