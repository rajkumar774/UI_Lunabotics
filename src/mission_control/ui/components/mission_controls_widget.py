"""Mission clock controls: start / pause and reset.

These drive the Mission Control UI's own run countdown. The clock is operator
station time, not rover telemetry, so these command nothing on the rover.

Compact enough to sit inline in the header beside the countdown, which is why
there is no separate controls row in the window.
"""

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

START_TEXT = "▶"
PAUSE_TEXT = "❚❚"
RESET_TEXT = "↺"


class MissionControlsWidget(QWidget):
    """Start/pause and reset buttons for the mission clock."""

    start_pause_requested = Signal()
    reset_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("MissionClockControls")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.start_pause_btn = QPushButton(START_TEXT)
        self.start_pause_btn.setObjectName("ClockButton")
        self.start_pause_btn.setToolTip("Start / pause the mission clock")
        self.start_pause_btn.clicked.connect(self.start_pause_requested.emit)
        layout.addWidget(self.start_pause_btn)

        self.reset_btn = QPushButton(RESET_TEXT)
        self.reset_btn.setObjectName("ClockButton")
        self.reset_btn.setToolTip("Reset the mission clock to the full run duration")
        self.reset_btn.clicked.connect(self.reset_requested.emit)
        layout.addWidget(self.reset_btn)

    def set_running(self, running: bool) -> None:
        self.start_pause_btn.setText(PAUSE_TEXT if running else START_TEXT)
