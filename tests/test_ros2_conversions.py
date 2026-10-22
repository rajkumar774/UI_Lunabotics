"""Tests for ROS 2 message -> RoverState conversions.

These run with no ROS 2 installed: every conversion reads plain attributes, so
the messages here are lightweight stand-ins with the same field layout as the
generated types.
"""

import math
import types

import pytest

from mission_control.comms.ros2 import conversions as cv
from mission_control.telemetry.state import (
    DiagnosticsData,
    DriveData,
    MissionMode,
    MissionPhase,
    SensorStatus,
)


def ns(**kwargs):
    """Builds a simple attribute bag standing in for a ROS message."""
    return types.SimpleNamespace(**kwargs)


# --- Orientation ----------------------------------------------------------

def test_quaternion_identity_is_level():
    roll, pitch, yaw = cv.quaternion_to_euler_deg(0.0, 0.0, 0.0, 1.0)
    assert roll == pytest.approx(0.0, abs=1e-6)
    assert pitch == pytest.approx(0.0, abs=1e-6)
    assert yaw == pytest.approx(0.0, abs=1e-6)


def test_quaternion_yaw_wraps_into_compass_range():
    """A -90 degree yaw must read as 270, not as a negative heading."""
    half = math.radians(-90.0) / 2.0
    _, _, yaw = cv.quaternion_to_euler_deg(0.0, 0.0, math.sin(half), math.cos(half))
    assert yaw == pytest.approx(270.0, abs=1e-6)


def test_quaternion_roll_pitch_recovered():
    roll_in, pitch_in = math.radians(20.0), math.radians(-12.0)
    cr, sr = math.cos(roll_in / 2), math.sin(roll_in / 2)
    cp, sp = math.cos(pitch_in / 2), math.sin(pitch_in / 2)
    x, y, z, w = sr * cp, cr * sp, -sr * sp, cr * cp

    roll, pitch, _ = cv.quaternion_to_euler_deg(x, y, z, w)
    assert roll == pytest.approx(20.0, abs=1e-3)
    assert pitch == pytest.approx(-12.0, abs=1e-3)


def test_degenerate_quaternion_does_not_raise():
    """A zero quaternion off the wire must read level rather than crash."""
    assert cv.quaternion_to_euler_deg(0.0, 0.0, 0.0, 0.0) == (0.0, 0.0, 0.0)


def test_imu_from_msg_populates_timestamp():
    msg = ns(orientation=ns(x=0.0, y=0.0, z=0.0, w=1.0))
    imu = cv.imu_from_msg(msg, timestamp=1234.5)
    assert imu.timestamp == 1234.5
    assert imu.yaw_deg == pytest.approx(0.0, abs=1e-6)


# --- Odometry and joints --------------------------------------------------

def test_drive_from_odometry_preserves_wheel_speeds():
    """Odometry carries the body twist only; wheel RPM arrives separately."""
    previous = DriveData(left_wheel_rpm=41.0, right_wheel_rpm=45.0)
    msg = ns(twist=ns(twist=ns(linear=ns(x=0.37), angular=ns(z=-0.08))))

    drive = cv.drive_from_odometry(msg, previous, timestamp=10.0)
    assert drive.linear_velocity == pytest.approx(0.37)
    assert drive.angular_velocity == pytest.approx(-0.08)
    assert drive.left_wheel_rpm == 41.0
    assert drive.right_wheel_rpm == 45.0


def test_wheel_rpm_converts_rad_per_second():
    msg = ns(
        name=["left_wheel_joint", "right_wheel_joint"],
        velocity=[2 * math.pi, 4 * math.pi],   # 1 and 2 rev/s
        position=[],
    )
    left, right = cv.wheel_rpm_from_joint_states(msg)
    assert left == pytest.approx(60.0)
    assert right == pytest.approx(120.0)


def test_wheel_rpm_averages_multiple_joints_per_side():
    msg = ns(
        name=["front_left_wheel", "rear_left_wheel", "front_right_wheel"],
        velocity=[2 * math.pi, 4 * math.pi, 2 * math.pi],
        position=[],
    )
    left, right = cv.wheel_rpm_from_joint_states(msg)
    assert left == pytest.approx(90.0)   # mean of 60 and 120
    assert right == pytest.approx(60.0)


def test_wheel_rpm_returns_none_for_unreported_side():
    """An absent side must stay unknown, never be reported as a real zero."""
    msg = ns(name=["left_wheel_joint"], velocity=[2 * math.pi], position=[])
    left, right = cv.wheel_rpm_from_joint_states(msg)
    assert left == pytest.approx(60.0)
    assert right is None


def test_wheel_rpm_ignores_non_drive_joints():
    msg = ns(name=["left_arm_joint", "right_arm_joint"], velocity=[5.0, 5.0], position=[])
    assert cv.wheel_rpm_from_joint_states(msg) == (None, None)


def test_wheel_rpm_handles_empty_message():
    assert cv.wheel_rpm_from_joint_states(ns(name=[], velocity=[], position=[])) == (None, None)


def test_excavation_only_reports_present_joints():
    msg = ns(
        name=["excavation_drum_joint", "arm_lift_joint"],
        velocity=[2 * math.pi, 0.0],
        position=[0.0, 0.42],
    )
    result = cv.excavation_from_joint_states(msg)
    assert result["drum_rpm"] == pytest.approx(60.0)
    assert result["arm_extension_pct"] == pytest.approx(42.0)

    assert cv.excavation_from_joint_states(ns(name=["left_wheel"], velocity=[1.0], position=[0.0])) == {}


# --- Battery --------------------------------------------------------------

def test_battery_fraction_scaled_to_percent():
    """ROS reports 0..1; the operator cards want 0..100."""
    battery = cv.battery_from_msg(
        ns(voltage=25.6, current=-8.4, percentage=0.83, temperature=31.5), timestamp=1.0
    )
    assert battery.percentage == pytest.approx(83.0)


def test_battery_discharge_current_shown_as_positive_draw():
    battery = cv.battery_from_msg(ns(voltage=24.0, current=-9.2, percentage=0.5, temperature=0.0), 1.0)
    assert battery.current_amps == pytest.approx(9.2)


def test_battery_already_in_percent_is_not_rescaled():
    battery = cv.battery_from_msg(ns(voltage=24.0, current=0.0, percentage=76.0, temperature=0.0), 1.0)
    assert battery.percentage == pytest.approx(76.0)


def test_battery_nan_fields_fall_back_to_zero():
    """BatteryState publishes NaN for unmeasured fields; NaN must not reach the UI."""
    battery = cv.battery_from_msg(
        ns(voltage=float("nan"), current=float("nan"), percentage=float("nan"), temperature=float("nan")),
        timestamp=1.0,
    )
    assert battery.voltage == 0.0
    assert battery.current_amps == 0.0
    assert battery.percentage == 0.0


# --- Diagnostics ----------------------------------------------------------

def test_diagnostic_level_reads_rclpy_bytes():
    """rclpy maps the ROS `byte` type to bytes, so WARN arrives as b'\\x01'.

    Parsing that as a float raises, which previously turned every warning and
    error into OK -- the exact failure an operator must never be shown.
    """
    assert cv.diagnostic_level(ns(level=b"\x01")) == cv.DIAG_WARN
    assert cv.diagnostic_level(ns(level=b"\x02")) == cv.DIAG_ERROR
    assert cv.diagnostic_level(ns(level=b"\x00")) == cv.DIAG_OK


def test_diagnostic_level_accepts_plain_int_and_garbage():
    assert cv.diagnostic_level(ns(level=2)) == cv.DIAG_ERROR
    assert cv.diagnostic_level(ns(level="3")) == cv.DIAG_STALE
    assert cv.diagnostic_level(ns(level=None)) == cv.DIAG_OK
    assert cv.diagnostic_level(ns()) == cv.DIAG_OK


def test_diagnostics_parses_jetson_key_values():
    msg = ns(status=[ns(
        level=b"\x00", name="jetson_stats", message="nominal",
        values=[ns(key="CPU Usage", value="47.5"),
                ns(key="GPU Usage", value="22.0"),
                ns(key="RAM Usage", value="61.3"),
                ns(key="Temperature", value="52.4")],
    )])
    diag = cv.diagnostics_from_msg(msg, DiagnosticsData())
    assert diag.cpu_usage_pct == pytest.approx(47.5)
    assert diag.gpu_usage_pct == pytest.approx(22.0)
    assert diag.ram_usage_pct == pytest.approx(61.3)
    assert diag.jetson_temp_c == pytest.approx(52.4)


def test_diagnostics_strips_units_from_values():
    msg = ns(status=[ns(level=b"\x00", name="stats", message="", values=[
        ns(key="CPU Usage", value="48%"), ns(key="Temperature", value="51.2°C"),
    ])])
    diag = cv.diagnostics_from_msg(msg, DiagnosticsData())
    assert diag.cpu_usage_pct == pytest.approx(48.0)
    assert diag.jetson_temp_c == pytest.approx(51.2)


def test_diagnostics_keeps_previous_values_for_absent_fields():
    """A DiagnosticArray commonly reports only what changed."""
    previous = DiagnosticsData(cpu_usage_pct=40.0, gpu_usage_pct=15.0, jetson_temp_c=49.0)
    msg = ns(status=[ns(level=b"\x00", name="stats", message="", values=[ns(key="CPU Usage", value="55.0")])])

    diag = cv.diagnostics_from_msg(msg, previous)
    assert diag.cpu_usage_pct == pytest.approx(55.0)
    assert diag.gpu_usage_pct == pytest.approx(15.0)
    assert diag.jetson_temp_c == pytest.approx(49.0)


def test_diagnostics_collects_warning_and_error_alerts():
    msg = ns(status=[
        ns(level=b"\x00", name="imu", message="ok", values=[]),
        ns(level=b"\x01", name="wheel_encoders", message="intermittent", values=[]),
        ns(level=b"\x02", name="lidar_rear", message="no returns", values=[]),
    ])
    diag = cv.diagnostics_from_msg(msg, DiagnosticsData())
    assert "wheel_encoders: intermittent" in diag.active_alerts
    assert "lidar_rear: no returns" in diag.active_alerts
    assert not any("imu" in alert for alert in diag.active_alerts)


def test_sensor_health_maps_diagnostic_levels():
    msg = ns(status=[
        ns(level=b"\x00", name="imu_driver", message="", values=[]),
        ns(level=b"\x01", name="wheel_encoders", message="", values=[]),
        ns(level=b"\x02", name="lidar_front", message="", values=[]),
    ])
    health = cv.sensor_health_from_diagnostics(msg)
    assert health["imu"] == SensorStatus.ONLINE
    assert health["encoders"] == SensorStatus.DEGRADED
    assert health["lidar"] == SensorStatus.OFFLINE


def test_sensor_health_omits_unreported_groups():
    """An unreported group must keep its previous status, not default to ONLINE."""
    msg = ns(status=[ns(level=b"\x00", name="imu_driver", message="", values=[])])
    health = cv.sensor_health_from_diagnostics(msg)
    assert set(health) == {"imu"}


# --- Mission phase and mode -----------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("PRE-CHECK", MissionPhase.PRE_CHECK),
    ("PRE_CHECK", MissionPhase.PRE_CHECK),
    ("precheck", MissionPhase.PRE_CHECK),
    ("TELEOP", MissionPhase.TELEOP),
    (" teleop ", MissionPhase.TELEOP),
    ("POST-RUN", MissionPhase.POST_RUN),
    ("DUMP", MissionPhase.DUMP),
])
def test_mission_phase_parsing(raw, expected):
    assert cv.mission_phase_from_string(raw) == expected


def test_mission_phase_rejects_unknown():
    assert cv.mission_phase_from_string("LAUNCHING") is None
    assert cv.mission_phase_from_string("") is None


@pytest.mark.parametrize("raw,expected", [
    ("MANUAL TELEOP", MissionMode.MANUAL_TELEOP),
    ("MANUAL_TELEOP", MissionMode.MANUAL_TELEOP),
    ("teleop", MissionMode.MANUAL_TELEOP),
    ("EXCAVATION", MissionMode.EXCAVATION),
    ("dig", MissionMode.EXCAVATION),
    ("DUMP / CONSTRUCTION", MissionMode.DUMP_CONSTRUCTION),
    ("dump", MissionMode.DUMP_CONSTRUCTION),
    ("AUTONOMOUS NAV", MissionMode.AUTONOMOUS_NAV),
    ("auto", MissionMode.AUTONOMOUS_NAV),
    ("IDLE / STANDBY", MissionMode.IDLE),
    ("standby", MissionMode.IDLE),
])
def test_mission_mode_parsing(raw, expected):
    assert cv.mission_mode_from_string(raw) == expected


def test_mission_mode_rejects_unknown():
    assert cv.mission_mode_from_string("HOVER") is None
    assert cv.mission_mode_from_string("") is None


# --- Heartbeat ------------------------------------------------------------

def test_heartbeat_latency_from_stamp():
    assert cv.heartbeat_latency_ms(100.0, 100.025) == pytest.approx(25.0)


def test_heartbeat_latency_clamps_clock_skew():
    """A rover clock ahead of the laptop's must read 0, not a negative latency."""
    assert cv.heartbeat_latency_ms(100.0, 99.9) == 0.0


def test_heartbeat_latency_ignores_unset_stamp():
    assert cv.heartbeat_latency_ms(0.0, 12345.0) == 0.0


# --- Power logger readings (watts and watt-hours) -------------------------


def test_power_and_energy_come_from_the_logger_status():
    """BatteryState has no power or energy field, so the PZEM reports both
    on /diagnostics and they are read back from there."""
    msg = ns(status=[ns(
        level=b"\x00", name="power: PZEM-003 logger", message="24.44 V",
        values=[ns(key="voltage", value="24.44"),
                ns(key="current", value="0.49"),
                ns(key="power", value="11.9"),
                ns(key="energy_wh", value="134")],
    )])
    power = cv.power_from_diagnostics(msg)
    assert power["power_watts"] == pytest.approx(11.9)
    assert power["energy_wh"] == pytest.approx(134.0)


def test_power_is_not_taken_from_an_unrelated_status():
    """A CPU status carrying a 'power' key must not be read as pack draw."""
    msg = ns(status=[ns(
        level=b"\x00", name="jetson_stats", message="",
        values=[ns(key="power mode", value="25"), ns(key="CPU Usage", value="47.5")],
    )])
    assert cv.power_from_diagnostics(msg) == {}


def test_absent_power_keys_are_simply_not_reported():
    """Missing keys leave the previous reading alone rather than zeroing it."""
    msg = ns(status=[ns(
        level=b"\x00", name="power: PZEM-003 logger", message="",
        values=[ns(key="voltage", value="24.44")],
    )])
    assert cv.power_from_diagnostics(msg) == {}
