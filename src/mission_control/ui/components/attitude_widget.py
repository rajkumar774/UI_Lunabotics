"""Attitude card: roll, pitch, and heading."""

from typing import Optional

from PySide6.QtWidgets import QWidget

from mission_control.telemetry.state import (
    RoverState,
    Severity,
    attitude_severity,
)
from mission_control.ui.components.telemetry_primitives import (
    MetricRow,
    TelemetryCard,
)

DEGREE = "°"


class AttitudeWidget(TelemetryCard):
    """Displays IMU roll/pitch tilt and heading, with tilt severity levels."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("Attitude", parent)

        self.roll_row = self.add_row(MetricRow("Roll", emphasis=True))
        self.pitch_row = self.add_row(MetricRow("Pitch", emphasis=True))
        self.yaw_row = self.add_row(MetricRow("Yaw"))

        self.show_no_data()

    def show_no_data(self) -> None:
        self.roll_row.clear_value()
        self.pitch_row.clear_value()
        self.yaw_row.clear_value()

    def update_from_state(self, state: RoverState) -> None:
        if not state.has_telemetry:
            self.show_no_data()
            return

        imu = state.imu
        self.roll_row.set_value(f"{imu.roll_deg:+.1f}{DEGREE}", attitude_severity(imu.roll_deg))
        self.pitch_row.set_value(f"{imu.pitch_deg:+.1f}{DEGREE}", attitude_severity(imu.pitch_deg))
        self.yaw_row.set_value(f"{imu.yaw_deg % 360.0:.1f}{DEGREE}", Severity.NORMAL)
