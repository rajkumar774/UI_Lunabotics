"""Header mission block: the prominent run countdown and its clock controls."""

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from mission_control.core.mission_clock import (
    CLOCK_ERROR_SECONDS,
    CLOCK_WARNING_SECONDS,
    format_mmss,
)
from mission_control.telemetry.state import Severity
from mission_control.ui.components.mission_controls_widget import MissionControlsWidget
from mission_control.ui.components.telemetry_primitives import apply_severity

CLOCK_CAPTION = "MISSION CLOCK"
COMPLETE_CAPTION = "MISSION COMPLETE"


def clock_severity(remaining_seconds: int, is_complete: bool = False) -> Severity:
    """Severity of the remaining run time, used for the clock's styling."""
    if is_complete or remaining_seconds <= 0:
        return Severity.ERROR
    if remaining_seconds <= CLOCK_ERROR_SECONDS:
        return Severity.ERROR
    if remaining_seconds <= CLOCK_WARNING_SECONDS:
        return Severity.WARNING
    return Severity.NORMAL


class MissionStatusWidget(QWidget):
    """Displays the mission countdown and its start/pause/reset controls."""

    def __init__(self, duration_seconds: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("MissionStatus")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # Countdown block
        clock_block = QVBoxLayout()
        clock_block.setSpacing(0)
        self.caption_label = QLabel(CLOCK_CAPTION)
        self.caption_label.setObjectName("MissionClockCaption")
        self.caption_label.setAlignment(Qt.AlignCenter)
        self.clock_label = QLabel(format_mmss(duration_seconds))
        self.clock_label.setObjectName("MissionClockValue")
        self.clock_label.setAlignment(Qt.AlignCenter)
        clock_block.addWidget(self.caption_label)
        clock_block.addWidget(self.clock_label)
        layout.addLayout(clock_block)

        # Clock controls sit inline, so the window needs no separate controls row.
        self.controls = MissionControlsWidget(self)
        layout.addWidget(self.controls)

        self._is_complete = False
        self.set_remaining(duration_seconds)

    @property
    def is_complete(self) -> bool:
        return self._is_complete

    def set_remaining(self, seconds: int) -> None:
        """Updates the countdown display and its severity styling."""
        self.clock_label.setText(format_mmss(seconds))
        apply_severity(self.clock_label, clock_severity(seconds, self._is_complete))

    def set_running(self, running: bool) -> None:
        """Reflects the clock's run state on the start/pause button."""
        self.controls.set_running(running)

    def set_complete(self, complete: bool) -> None:
        """Switches the caption between MISSION CLOCK and MISSION COMPLETE."""
        self._is_complete = complete
        self.caption_label.setText(COMPLETE_CAPTION if complete else CLOCK_CAPTION)
        apply_severity(
            self.caption_label,
            Severity.ERROR if complete else Severity.NORMAL,
        )
        # Re-apply clock styling so it reflects the completed run.
        self.set_remaining(0 if complete else self._current_seconds())

    def _current_seconds(self) -> int:
        text = self.clock_label.text()
        try:
            minutes, seconds = text.split(":")
            return int(minutes) * 60 + int(seconds)
        except (ValueError, AttributeError):
            return 0
