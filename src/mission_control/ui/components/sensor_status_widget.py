"""Sensor health card: IMU, encoders, LiDAR, plus onboard-computer diagnostics."""

from typing import Optional

from PySide6.QtWidgets import QLabel, QWidget

from mission_control.telemetry.state import (
    RoverState,
    SensorStatus,
    Severity,
    sensor_severity,
)
from mission_control.ui.components.telemetry_primitives import (
    NO_DATA_TEXT,
    SensorRow,
    TelemetryCard,
    apply_severity,
)


class SensorStatusWidget(TelemetryCard):
    """Displays reported sensor-group health and a compact Jetson diagnostics line."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__("Sensors", parent)

        self.imu_row = self.add_row(SensorRow("IMU"))
        self.encoder_row = self.add_row(SensorRow("Encoders"))
        self.lidar_row = self.add_row(SensorRow("LiDAR"))

        self.diagnostics_label = QLabel(NO_DATA_TEXT)
        self.diagnostics_label.setObjectName("CardSubtext")
        self.add_row(self.diagnostics_label)

        self.show_no_data()

    def show_no_data(self) -> None:
        for row in (self.imu_row, self.encoder_row, self.lidar_row):
            row.set_status("NO DATA", Severity.NO_DATA)
        self.diagnostics_label.setText("JETSON --°C · CPU --%")
        apply_severity(self.diagnostics_label, Severity.NO_DATA)

    def update_from_state(self, state: RoverState) -> None:
        if not state.has_telemetry:
            self.show_no_data()
            return

        sensors = state.sensors
        for row, status in (
            (self.imu_row, sensors.imu),
            (self.encoder_row, sensors.encoders),
            (self.lidar_row, sensors.lidar),
        ):
            severity = sensor_severity(status)
            text = status.value if status != SensorStatus.UNKNOWN else "NO DATA"
            row.set_status(text, severity)

        diagnostics = state.diagnostics
        self.diagnostics_label.setText(
            f"JETSON {diagnostics.jetson_temp_c:.1f}°C · CPU {diagnostics.cpu_usage_pct:.0f}%"
        )
        apply_severity(self.diagnostics_label, Severity.NORMAL)
