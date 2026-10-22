"""Shared building blocks for the telemetry panel.

Text and frame styling lives in stylesheet.qss and is selected with the Qt dynamic
property ``severity`` (e.g. ``QLabel#MetricValue[severity="WARNING"]``). Only the two
custom-painted widgets in this module need colours in Python, because QPainter cannot
read QSS rules; those come from the single SEVERITY_COLORS table below.
"""

from typing import Optional

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from mission_control.telemetry.state import Severity

#: Fallback text shown wherever a value is unavailable.
NO_DATA_TEXT = "--"

#: Painter colours per severity (kept in sync with the QSS severity rules).
SEVERITY_COLORS = {
    Severity.NORMAL: QColor("#34d399"),
    Severity.WARNING: QColor("#fbbf24"),
    Severity.ERROR: QColor("#ff5f5f"),
    Severity.OFFLINE: QColor("#8a7079"),
    Severity.NO_DATA: QColor("#6b525a"),
}

TRACK_COLOR = QColor("#261820")
TRACK_BORDER_COLOR = QColor("#3d2730")


def apply_severity(widget: QWidget, severity: Severity) -> None:
    """Sets the ``severity`` dynamic property and re-polishes the widget."""
    widget.setProperty("severity", severity.name)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


class TelemetryCard(QFrame):
    """Compact titled container used for each telemetry section."""

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("TelemetryCard")
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 9, 12, 9)
        self._layout.setSpacing(5)

        self.title_label = QLabel(title.upper())
        self.title_label.setObjectName("TelemetryCardTitle")
        self._layout.addWidget(self.title_label)

        self.body_layout = QVBoxLayout()
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(4)
        self._layout.addLayout(self.body_layout)
        self._layout.addStretch()

    def add_row(self, widget: QWidget) -> QWidget:
        """Appends a row widget to the card body and returns it."""
        self.body_layout.addWidget(widget)
        return widget


class MetricRow(QWidget):
    """One ``LABEL   value`` line inside a telemetry card."""

    def __init__(
        self,
        label: str,
        value: str = NO_DATA_TEXT,
        emphasis: bool = False,
        status: bool = False,
        parent: Optional[QWidget] = None,
    ):
        """``emphasis`` marks a headline reading; ``status`` marks a state word
        (HEALTHY / LOW / OFFLINE), which is coloured by condition rather than
        rendered as a neutral measurement."""
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.label = QLabel(label.upper())
        self.label.setObjectName("MetricLabel")
        layout.addWidget(self.label)

        layout.addStretch()

        if emphasis:
            object_name = "MetricValueStrong"
        elif status:
            object_name = "StatusValue"
        else:
            object_name = "MetricValue"

        self.value_label = QLabel(value)
        self.value_label.setObjectName(object_name)
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.value_label)

        apply_severity(self.value_label, Severity.NO_DATA)

    def set_value(self, text: str, severity: Severity = Severity.NORMAL) -> None:
        self.value_label.setText(text)
        apply_severity(self.value_label, severity)

    def clear_value(self, text: str = NO_DATA_TEXT) -> None:
        """Resets the row to its no-data appearance."""
        self.set_value(text, Severity.NO_DATA)

    def value(self) -> str:
        return self.value_label.text()


class StatusDot(QWidget):
    """Small painted indicator; always paired with a text label, never used alone."""

    def __init__(self, diameter: int = 10, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._diameter = diameter
        self._severity = Severity.NO_DATA
        self.setFixedSize(diameter + 2, diameter + 2)

    def set_severity(self, severity: Severity) -> None:
        self._severity = severity
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        color = SEVERITY_COLORS.get(self._severity, SEVERITY_COLORS[Severity.NO_DATA])
        painter.setBrush(color)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(1, 1, self._diameter, self._diameter)
        painter.end()


class SensorRow(QWidget):
    """``NAME  ● STATE`` row used by the sensor health card."""

    def __init__(self, name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.name_label = QLabel(name.upper())
        self.name_label.setObjectName("MetricLabel")
        layout.addWidget(self.name_label)

        layout.addStretch()

        self.dot = StatusDot()
        layout.addWidget(self.dot)

        self.state_label = QLabel(NO_DATA_TEXT)
        self.state_label.setObjectName("StatusValue")
        self.state_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.state_label)

        apply_severity(self.state_label, Severity.NO_DATA)

    def set_status(self, text: str, severity: Severity) -> None:
        self.state_label.setText(text)
        self.dot.set_severity(severity)
        apply_severity(self.state_label, severity)

    def status_text(self) -> str:
        return self.state_label.text()


class BatteryBar(QWidget):
    """Segmented charge bar. Severity is also written as text by the owning card."""

    SEGMENTS = 16

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._percentage: Optional[float] = None
        self._severity = Severity.NO_DATA
        self.setMinimumHeight(14)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(140, 14)

    def set_percentage(self, percentage: Optional[float], severity: Severity) -> None:
        """Sets the fill level; pass None to render the unknown/no-data state."""
        self._percentage = percentage
        self._severity = severity
        self.update()

    def percentage(self) -> Optional[float]:
        return self._percentage

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)

        width = self.width()
        height = self.height()
        gap = 2
        seg_width = max(1, (width - gap * (self.SEGMENTS - 1)) // self.SEGMENTS)

        pct = 0.0 if self._percentage is None else max(0.0, min(100.0, self._percentage))
        filled = int(round(pct / 100.0 * self.SEGMENTS))
        fill_color = SEVERITY_COLORS.get(self._severity, SEVERITY_COLORS[Severity.NO_DATA])

        painter.setPen(Qt.NoPen)
        for index in range(self.SEGMENTS):
            x = index * (seg_width + gap)
            if index < filled:
                painter.setBrush(fill_color)
            else:
                painter.setBrush(TRACK_COLOR)
            painter.drawRect(x, 0, seg_width, height)
        painter.end()
