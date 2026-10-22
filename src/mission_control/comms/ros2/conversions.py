"""Pure ROS 2 message -> RoverState conversions.

Every function here is deliberately free of rclpy and of any generated message
class: each one reads plain attributes off whatever it is handed. That keeps the
mapping rules unit testable on a machine with no ROS 2 installed, and keeps the
decision of *what a message means* separate from the plumbing that receives it.

Nothing in this module mutates shared state; the worker owns the accumulating
RoverState and applies these results to it.
"""

import math
from typing import Any, Dict, Iterable, Optional

from mission_control.telemetry.state import (
    BatteryData,
    DiagnosticsData,
    DriveData,
    IMUData,
    MissionMode,
    MissionPhase,
    SensorStatus,
)

#: rad/s -> revolutions per minute
RAD_PER_S_TO_RPM = 60.0 / (2.0 * math.pi)

#: Substrings identifying a left-side or right-side drive joint in /joint_states.
LEFT_JOINT_HINTS = ("left", "_l_", "port")
RIGHT_JOINT_HINTS = ("right", "_r_", "starboard")
DRIVE_JOINT_HINTS = ("wheel", "drive", "track")
DRUM_JOINT_HINTS = ("drum", "bucket", "auger", "excav")
ARM_JOINT_HINTS = ("arm", "linkage", "boom", "lift")

#: Diagnostic KeyValue keys, matched case-insensitively by substring.
CPU_KEYS = ("cpu usage", "cpu_usage", "cpu load", "cpu")
GPU_KEYS = ("gpu usage", "gpu_usage", "gpu load", "gpu")
RAM_KEYS = ("ram usage", "ram_usage", "memory usage", "mem usage", "ram", "memory")
TEMP_KEYS = ("temp", "temperature", "thermal")

#: diagnostic_msgs/DiagnosticStatus level constants.
DIAG_OK = 0
DIAG_WARN = 1
DIAG_ERROR = 2
DIAG_STALE = 3

_DIAG_LEVEL_TO_SENSOR_STATUS = {
    DIAG_OK: SensorStatus.ONLINE,
    DIAG_WARN: SensorStatus.DEGRADED,
    DIAG_ERROR: SensorStatus.OFFLINE,
    DIAG_STALE: SensorStatus.OFFLINE,
}

#: KeyValue keys carrying the power logger's readings, and the status names
#: that may own them. sensor_msgs/BatteryState has no power or energy field,
#: so the PZEM reports both on /diagnostics.
POWER_W_KEYS = ("power_w", "power")
ENERGY_KEYS = ("energy_wh", "energy")
POWER_STATUS_HINTS = ("power", "pzem", "battery")

#: Which diagnostic status names feed which sensor-health field.
SENSOR_DIAG_HINTS = {
    "imu": ("imu", "inertial"),
    "encoders": ("encoder", "odom", "wheel"),
    "lidar": ("lidar", "laser", "scan"),
}


def _f(value: Any, default: float = 0.0) -> float:
    """Coerces a message field to float, tolerating None and NaN."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result


def diagnostic_level(status: Any) -> int:
    """Reads DiagnosticStatus.level as an int.

    rclpy maps the ROS ``byte`` type to a one-byte Python ``bytes`` object, so
    ``DiagnosticStatus.WARN`` is ``b'\\x01'`` and not ``1``. Coercing that with
    float() raises, which would quietly turn every warning and error into OK —
    exactly the failure an operator must never be shown.
    """
    raw = getattr(status, "level", DIAG_OK)
    if isinstance(raw, (bytes, bytearray)):
        return int.from_bytes(raw[:1], "little") if raw else DIAG_OK
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return DIAG_OK


def quaternion_to_euler_deg(x: float, y: float, z: float, w: float) -> tuple[float, float, float]:
    """Converts a ROS orientation quaternion to (roll, pitch, yaw) in degrees.

    Yaw is wrapped to [0, 360) to match the heading convention the attitude card
    already renders. Pitch is clamped before asin so a slightly denormalised
    quaternion off the wire cannot raise.
    """
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm < 1e-9:
        return 0.0, 0.0, 0.0
    x, y, z, w = x / norm, y / norm, z / norm, w / norm

    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2.0 * (w * y - z * x))))
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))

    return (
        math.degrees(roll),
        math.degrees(pitch),
        math.degrees(yaw) % 360.0,
    )


def imu_from_msg(msg: Any, timestamp: float) -> IMUData:
    """sensor_msgs/Imu -> IMUData."""
    q = msg.orientation
    roll, pitch, yaw = quaternion_to_euler_deg(_f(q.x), _f(q.y), _f(q.z), _f(q.w, 1.0))
    return IMUData(roll_deg=roll, pitch_deg=pitch, yaw_deg=yaw, timestamp=timestamp)


def drive_from_odometry(msg: Any, previous: DriveData, timestamp: float) -> DriveData:
    """nav_msgs/Odometry -> DriveData, preserving wheel speeds from /joint_states.

    Odometry carries the body twist; per-wheel RPM arrives separately, so the
    existing wheel values are carried forward rather than zeroed.
    """
    twist = msg.twist.twist
    return DriveData(
        linear_velocity=_f(twist.linear.x),
        angular_velocity=_f(twist.angular.z),
        left_wheel_rpm=previous.left_wheel_rpm,
        right_wheel_rpm=previous.right_wheel_rpm,
        motor_currents=list(previous.motor_currents),
        timestamp=timestamp,
    )


def _matches(name: str, hints: Iterable[str]) -> bool:
    return any(hint in name for hint in hints)


def wheel_rpm_from_joint_states(msg: Any) -> tuple[Optional[float], Optional[float]]:
    """sensor_msgs/JointState -> (left_rpm, right_rpm), averaged per side.

    Returns None for a side with no matching joint so the caller can leave the
    previous reading in place instead of displaying a fabricated zero.
    """
    names = list(getattr(msg, "name", []) or [])
    velocities = list(getattr(msg, "velocity", []) or [])
    if not names or not velocities:
        return None, None

    left: list[float] = []
    right: list[float] = []
    for name, velocity in zip(names, velocities):
        lowered = name.lower()
        if not _matches(lowered, DRIVE_JOINT_HINTS):
            continue
        rpm = _f(velocity) * RAD_PER_S_TO_RPM
        if _matches(lowered, LEFT_JOINT_HINTS):
            left.append(rpm)
        elif _matches(lowered, RIGHT_JOINT_HINTS):
            right.append(rpm)

    return (
        sum(left) / len(left) if left else None,
        sum(right) / len(right) if right else None,
    )


def excavation_from_joint_states(msg: Any) -> Dict[str, float]:
    """sensor_msgs/JointState -> excavation fields that are present.

    Only keys actually found are returned, so absent hardware leaves the
    corresponding ExcavationData field untouched rather than reading 0.0.
    """
    names = list(getattr(msg, "name", []) or [])
    velocities = list(getattr(msg, "velocity", []) or [])
    positions = list(getattr(msg, "position", []) or [])
    result: Dict[str, float] = {}

    for index, name in enumerate(names):
        lowered = name.lower()
        if _matches(lowered, DRUM_JOINT_HINTS) and index < len(velocities):
            result["drum_rpm"] = _f(velocities[index]) * RAD_PER_S_TO_RPM
        elif _matches(lowered, ARM_JOINT_HINTS) and index < len(positions):
            result["arm_extension_pct"] = max(0.0, min(100.0, _f(positions[index]) * 100.0))

    return result


def battery_from_msg(msg: Any, timestamp: float) -> BatteryData:
    """sensor_msgs/BatteryState -> BatteryData.

    ROS reports percentage as a 0..1 fraction and discharge current as negative;
    the operator cards want 0..100 and a positive draw.
    """
    percentage = _f(getattr(msg, "percentage", 0.0))
    if 0.0 <= percentage <= 1.0:
        percentage *= 100.0

    temperature = _f(getattr(msg, "temperature", 0.0))

    return BatteryData(
        voltage=_f(getattr(msg, "voltage", 0.0)),
        current_amps=abs(_f(getattr(msg, "current", 0.0))),
        percentage=max(0.0, min(100.0, percentage)),
        cell_temperature_c=temperature,
        timestamp=timestamp,
    )


def _diagnostic_values(status: Any) -> Dict[str, str]:
    """Flattens a DiagnosticStatus KeyValue array into a lowercased dict."""
    values = {}
    for kv in getattr(status, "values", []) or []:
        key = str(getattr(kv, "key", "")).strip().lower()
        if key:
            values[key] = str(getattr(kv, "value", ""))
    return values


def _first_numeric(values: Dict[str, str], keys: Iterable[str]) -> Optional[float]:
    """Finds the first value whose key contains one of ``keys`` and parses it."""
    for wanted in keys:
        for key, raw in values.items():
            if wanted not in key:
                continue
            cleaned = raw.strip().rstrip("%").replace("°C", "").replace("C", "").strip()
            try:
                return float(cleaned)
            except ValueError:
                continue
    return None


def diagnostics_from_msg(msg: Any, previous: DiagnosticsData) -> DiagnosticsData:
    """diagnostic_msgs/DiagnosticArray -> DiagnosticsData.

    Fields absent from this array keep their previous reading, because a
    DiagnosticArray commonly reports only the subsystems that changed.
    """
    result = DiagnosticsData(
        cpu_usage_pct=previous.cpu_usage_pct,
        gpu_usage_pct=previous.gpu_usage_pct,
        ram_usage_pct=previous.ram_usage_pct,
        jetson_temp_c=previous.jetson_temp_c,
        last_heartbeat_timestamp=previous.last_heartbeat_timestamp,
        active_alerts=[],
    )

    for status in getattr(msg, "status", []) or []:
        values = _diagnostic_values(status)

        for attr, keys in (
            ("cpu_usage_pct", CPU_KEYS),
            ("gpu_usage_pct", GPU_KEYS),
            ("ram_usage_pct", RAM_KEYS),
            ("jetson_temp_c", TEMP_KEYS),
        ):
            found = _first_numeric(values, keys)
            if found is not None:
                setattr(result, attr, found)

        level = diagnostic_level(status)
        if level >= DIAG_WARN:
            name = str(getattr(status, "name", "") or "diagnostic")
            message = str(getattr(status, "message", "") or "")
            label = f"{name}: {message}".strip().rstrip(":")
            if label not in result.active_alerts:
                result.active_alerts.append(label)

    return result


def power_from_diagnostics(msg: Any) -> Dict[str, float]:
    """diagnostic_msgs/DiagnosticArray -> {power_watts, energy_wh} when present.

    Only the power logger's own status is inspected, so a CPU status that
    happens to carry a key containing "power" cannot be mistaken for the pack's
    draw. Keys absent from the array are simply not returned, leaving the
    previous reading in place rather than resetting it to zero.
    """
    result: Dict[str, float] = {}
    for status in getattr(msg, "status", []) or []:
        name = str(getattr(status, "name", "")).lower()
        if not _matches(name, POWER_STATUS_HINTS):
            continue
        values = _diagnostic_values(status)
        watts = _first_numeric(values, POWER_W_KEYS)
        if watts is not None:
            result["power_watts"] = watts
        energy = _first_numeric(values, ENERGY_KEYS)
        if energy is not None:
            result["energy_wh"] = energy
    return result


def sensor_health_from_diagnostics(msg: Any) -> Dict[str, SensorStatus]:
    """diagnostic_msgs/DiagnosticArray -> sensor health fields that are reported.

    Only groups named in the array are returned; an unreported group keeps its
    previous status rather than being silently marked ONLINE.
    """
    result: Dict[str, SensorStatus] = {}
    for status in getattr(msg, "status", []) or []:
        name = str(getattr(status, "name", "")).lower()
        level = diagnostic_level(status)
        status_enum = _DIAG_LEVEL_TO_SENSOR_STATUS.get(level, SensorStatus.UNKNOWN)
        for field_name, hints in SENSOR_DIAG_HINTS.items():
            if _matches(name, hints):
                result[field_name] = status_enum
    return result


def mission_phase_from_string(raw: str) -> Optional[MissionPhase]:
    """Parses /mission/state text into a MissionPhase.

    Accepts the enum value ("PRE-CHECK"), the enum name ("PRE_CHECK"), and the
    loose spellings a rover node is likely to publish ("precheck", "teleop").
    """
    if not raw:
        return None

    normalized = _normalize_token(raw)
    for phase in MissionPhase:
        if normalized in (_normalize_token(phase.name), _normalize_token(phase.value)):
            return phase

    # Accept the run-together spelling too ("precheck", "postrun").
    collapsed = normalized.replace("_", "")
    for phase in MissionPhase:
        if collapsed == phase.name.replace("_", ""):
            return phase

    return None


def _normalize_token(raw: str) -> str:
    """Upper-cases and collapses separators to single underscores.

    Both the inbound string and the enum it is compared against go through this
    one function, so "IDLE / STANDBY", "IDLE_STANDBY" and "idle standby" all
    reduce to the same token.
    """
    normalized = raw.strip().upper()
    for separator in ("-", " ", "/"):
        normalized = normalized.replace(separator, "_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized.strip("_")


def mission_mode_from_string(raw: str) -> Optional[MissionMode]:
    """Parses a reported control mode into a MissionMode.

    Accepts the enum value ("MANUAL TELEOP"), the enum name ("MANUAL_TELEOP"),
    and the short spellings a rover node is likely to publish ("teleop",
    "auto", "excavation").
    """
    if not raw:
        return None

    normalized = _normalize_token(raw)

    for mode in MissionMode:
        if normalized in (_normalize_token(mode.name), _normalize_token(mode.value)):
            return mode

    aliases = {
        "TELEOP": MissionMode.MANUAL_TELEOP,
        "MANUAL": MissionMode.MANUAL_TELEOP,
        "AUTO": MissionMode.AUTONOMOUS_NAV,
        "AUTONOMOUS": MissionMode.AUTONOMOUS_NAV,
        "NAV": MissionMode.AUTONOMOUS_NAV,
        "EXCAVATE": MissionMode.EXCAVATION,
        "DIG": MissionMode.EXCAVATION,
        "DUMP": MissionMode.DUMP_CONSTRUCTION,
        "CONSTRUCTION": MissionMode.DUMP_CONSTRUCTION,
        "ESTOP": MissionMode.EMERGENCY_STOP,
        "E_STOP": MissionMode.EMERGENCY_STOP,
        "IDLE": MissionMode.IDLE,
        "STANDBY": MissionMode.IDLE,
    }
    return aliases.get(normalized)


def heartbeat_latency_ms(stamp_seconds: float, now_seconds: float) -> float:
    """One-way latency from a header stamp, clamped to a sane display range.

    This is only meaningful when both clocks are NTP/PTP synchronised. A negative
    result means the rover clock leads the laptop's, which reads as 0.0 rather
    than as a nonsensical negative latency.
    """
    if stamp_seconds <= 0.0:
        return 0.0
    delta_ms = (now_seconds - stamp_seconds) * 1000.0
    if delta_ms < 0.0:
        return 0.0
    return min(delta_ms, 60_000.0)
