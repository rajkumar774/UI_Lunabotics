"""Tests for the telemetry UI widgets and their failure/fallback states."""

import pytest
from PySide6.QtWidgets import QApplication

from mission_control.core.config import AppConfig, RoverConfig
from mission_control.core.event_bus import EventBus
from mission_control.comms.mock_client import MockCommsClient
from mission_control.telemetry.state import (
    BatteryData,
    ConnectionState,
    DiagnosticsData,
    DriveData,
    IMUData,
    MissionMode,
    RoverState,
    SensorHealthData,
    SensorStatus,
)
from mission_control.ui.components.telemetry_panel import TelemetryPanel
from mission_control.ui.components.telemetry_primitives import NO_DATA_TEXT
from mission_control.ui.main_window import MainWindow


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def make_state(
    battery_pct: float = 82.0,
    voltage: float = 27.4,
    speed: float = 0.42,
    roll: float = -1.6,
    pitch: float = 2.4,
    yaw: float = 137.5,
    latency_ms: float = 4.0,
    lidar: SensorStatus = SensorStatus.ONLINE,
    connection: ConnectionState = ConnectionState.MOCK_MODE,
    telemetry_valid: bool = True,
    is_stale: bool = False,
) -> RoverState:
    """Builds a fully populated snapshot for widget rendering tests."""
    return RoverState(
        connection_state=connection,
        mission_mode=MissionMode.IDLE,
        heartbeat_latency_ms=latency_ms,
        imu=IMUData(roll_deg=roll, pitch_deg=pitch, yaw_deg=yaw),
        battery=BatteryData(voltage=voltage, current_amps=8.3, percentage=battery_pct),
        drive=DriveData(
            linear_velocity=speed,
            angular_velocity=0.12,
            left_wheel_rpm=38.0,
            right_wheel_rpm=44.0,
        ),
        diagnostics=DiagnosticsData(cpu_usage_pct=38.0, jetson_temp_c=46.2),
        sensors=SensorHealthData(
            imu=SensorStatus.ONLINE,
            encoders=SensorStatus.ONLINE,
            lidar=lidar,
        ),
        telemetry_valid=telemetry_valid,
        last_telemetry_time=1.0,
        is_stale=is_stale,
    )


def severity_of(label) -> str:
    return label.property("severity")


# --- Power ---------------------------------------------------------------


def test_power_widget_displays_battery_state(qapp):
    panel = TelemetryPanel()
    panel.update_from_state(make_state(battery_pct=82.0, voltage=27.4))

    assert panel.power.charge_row.value() == "82%"
    assert panel.power.voltage_row.value() == "27.4 V"
    assert panel.power.state_row.value() == "NORMAL"
    assert severity_of(panel.power.charge_row.value_label) == "NORMAL"
    assert panel.power.battery_bar.percentage() == pytest.approx(82.0)


def test_power_widget_escalates_severity_as_charge_falls(qapp):
    panel = TelemetryPanel()

    panel.update_from_state(make_state(battery_pct=35.0))
    assert panel.power.state_row.value() == "LOW"
    assert severity_of(panel.power.charge_row.value_label) == "WARNING"

    panel.update_from_state(make_state(battery_pct=11.0))
    assert panel.power.state_row.value() == "CRITICAL"
    assert severity_of(panel.power.charge_row.value_label) == "ERROR"


# --- Attitude ------------------------------------------------------------


def test_attitude_widget_formats_signed_angles(qapp):
    panel = TelemetryPanel()
    panel.update_from_state(make_state(roll=-3.4, pitch=2.0, yaw=359.9))

    assert panel.attitude.roll_row.value() == "-3.4°"
    assert panel.attitude.pitch_row.value() == "+2.0°"
    assert panel.attitude.yaw_row.value() == "359.9°"
    assert severity_of(panel.attitude.roll_row.value_label) == "NORMAL"


def test_attitude_widget_flags_excessive_tilt(qapp):
    panel = TelemetryPanel()
    panel.update_from_state(make_state(roll=18.0, pitch=-27.0))

    assert severity_of(panel.attitude.roll_row.value_label) == "WARNING"
    assert severity_of(panel.attitude.pitch_row.value_label) == "ERROR"


# --- Motion --------------------------------------------------------------


def test_motion_widget_displays_speed_and_wheel_speeds(qapp):
    panel = TelemetryPanel()
    panel.update_from_state(make_state(speed=0.42))

    assert panel.motion.speed_row.value() == "0.42 m/s"
    assert panel.motion.rpm_row.value() == "41 RPM"  # mean of 38 / 44
    assert panel.motion.wheels_row.value() == "38 / 44"


# --- Sensors -------------------------------------------------------------


def test_sensor_widget_reflects_status_changes(qapp):
    panel = TelemetryPanel()

    panel.update_from_state(make_state(lidar=SensorStatus.ONLINE))
    assert panel.sensors.lidar_row.status_text() == "ONLINE"
    assert severity_of(panel.sensors.lidar_row.state_label) == "NORMAL"

    panel.update_from_state(make_state(lidar=SensorStatus.DEGRADED))
    assert panel.sensors.lidar_row.status_text() == "DEGRADED"
    assert severity_of(panel.sensors.lidar_row.state_label) == "WARNING"

    panel.update_from_state(make_state(lidar=SensorStatus.OFFLINE))
    assert panel.sensors.lidar_row.status_text() == "OFFLINE"
    assert severity_of(panel.sensors.lidar_row.state_label) == "ERROR"
    # Unaffected sensors keep reporting independently.
    assert panel.sensors.imu_row.status_text() == "ONLINE"


def test_sensor_widget_shows_onboard_diagnostics(qapp):
    panel = TelemetryPanel()
    panel.update_from_state(make_state())

    text = panel.sensors.diagnostics_label.text()
    assert "46.2" in text and "38%" in text


# --- Connection ----------------------------------------------------------
#
# The ROVER LINK card was removed from the panel, so the two tests that read
# its rows went with it. ConnectionStatusWidget still exists and is still
# covered by its own tests; what the panel must now guarantee is simply that
# it no longer carries that card, and that losing the link still blanks every
# remaining card.


def test_panel_no_longer_carries_a_rover_link_card(qapp):
    panel = TelemetryPanel()
    assert not hasattr(panel, "connection")
    assert len(panel.cards) == 4



# --- Failure and fallback states -----------------------------------------


def test_panel_shows_fallbacks_when_telemetry_is_unavailable(qapp):
    panel = TelemetryPanel()
    panel.update_from_state(make_state())  # populate first
    panel.update_from_state(
        make_state(connection=ConnectionState.DISCONNECTED, telemetry_valid=False)
    )

    assert panel.power.charge_row.value() == NO_DATA_TEXT
    assert panel.power.state_row.value() == "NO DATA"
    assert panel.motion.speed_row.value() == NO_DATA_TEXT
    assert panel.attitude.roll_row.value() == NO_DATA_TEXT
    assert panel.sensors.lidar_row.status_text() == "NO DATA"
    # Mission mode is unknown without a link, not simply the last value seen.
    assert panel.power.battery_bar.percentage() is None


def test_panel_reports_stale_telemetry_instead_of_frozen_values(qapp):
    panel = TelemetryPanel()
    panel.update_from_state(make_state(is_stale=True))

    # Stale readings are withheld rather than shown as if live.
    assert panel.motion.speed_row.value() == NO_DATA_TEXT
    assert panel.power.charge_row.value() == NO_DATA_TEXT


def test_panel_survives_missing_snapshot(qapp):
    panel = TelemetryPanel()
    panel.update_from_state(make_state())

    panel.update_from_state(None)
    panel.update_from_state("not-a-state")

    assert panel.attitude.yaw_row.value() == NO_DATA_TEXT


def test_panel_recovers_after_telemetry_returns(qapp):
    panel = TelemetryPanel()
    panel.update_from_state(None)
    panel.update_from_state(make_state(battery_pct=64.0))

    assert panel.power.charge_row.value() == "64%"


# --- Main window integration ---------------------------------------------


def test_main_window_renders_snapshots_from_the_event_bus(qapp):
    """The window must repaint from EventBus snapshots, not from the comms client."""
    bus = EventBus()
    client = MockCommsClient()
    client.start()
    window = MainWindow(AppConfig(), RoverConfig(), client, event_bus=bus)

    assert window.telemetry_panel.power.charge_row.value() == NO_DATA_TEXT

    bus.telemetry_updated.emit(make_state(battery_pct=77.0, speed=0.31))

    assert window.telemetry_panel.power.charge_row.value() == "77%"
    assert window.telemetry_panel.motion.speed_row.value() == "0.31 m/s"

    # A malformed publication must not take the window down.
    bus.telemetry_updated.emit(None)
    assert window.telemetry_panel.power.charge_row.value() == NO_DATA_TEXT

    client.stop()
    window.close()


def test_main_window_clears_telemetry_on_disconnect(qapp):
    bus = EventBus()
    client = MockCommsClient()
    client.start()
    window = MainWindow(AppConfig(), RoverConfig(), client, event_bus=bus)

    bus.telemetry_updated.emit(make_state(battery_pct=90.0))
    assert window.telemetry_panel.power.charge_row.value() == "90%"

    client.connect_rover()
    client.disconnect_rover()

    # The ROVER LINK badge was removed from the header; the behaviour it
    # advertised must survive it. Stale values left on screen after a dropped
    # link read as current, which is the failure that matters here.
    assert window.telemetry_panel.power.charge_row.value() == NO_DATA_TEXT

    client.stop()
    window.close()


def test_power_widget_shows_watts_and_watt_hours(qapp):
    """The COTS logger measures power and cumulative energy; both are shown."""
    panel = TelemetryPanel()
    state = make_state(battery_pct=82.0, voltage=27.4)
    state.battery.power_watts = 11.9
    state.battery.energy_wh = 1340.0
    panel.update_from_state(state)

    assert panel.power.power_row.value() == "11.9 W"
    assert panel.power.energy_row.value() == "1,340 Wh"


def test_power_widget_distinguishes_unreported_from_zero(qapp):
    """A logger that never reported watts must not read as an idle pack."""
    panel = TelemetryPanel()
    state = make_state()
    state.battery.power_watts = None
    state.battery.energy_wh = None
    panel.update_from_state(state)
    assert panel.power.power_row.value() == NO_DATA_TEXT
    assert panel.power.energy_row.value() == NO_DATA_TEXT

    state.battery.power_watts = 0.0
    panel.update_from_state(state)
    assert panel.power.power_row.value() == "0.0 W"
