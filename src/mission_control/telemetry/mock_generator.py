"""Deterministic mock telemetry generator.

MOCK DATA ONLY. This module does not model, simulate, or replace any rover
subsystem: it exists purely so the Mission Control UI can be developed and
validated against realistic, repeatable, time-varying values before real rover
telemetry is available. Every value is produced by bounded mathematical
functions of elapsed time, so two generators seeded identically and stepped
identically always produce identical output.

When real telemetry arrives, this generator is simply no longer instantiated —
it publishes the same RoverState structure the ROS 2 client will populate, so
nothing downstream (StateManager, EventBus, widgets) has to change.
"""

import math
import random
import time
from typing import Optional

from mission_control.telemetry.state import (
    BatteryData,
    DiagnosticsData,
    DriveData,
    ExcavationData,
    IMUData,
    RoverState,
    SensorHealthData,
    SensorStatus,
)

# --- Mock rover geometry (used only to keep speed and RPM mutually consistent) ---
WHEEL_RADIUS_M = 0.1016  # 8 inch wheel
TRACK_WIDTH_M = 0.55

# --- Deterministic sensor degradation windows (seconds) ---
LIDAR_DEGRADED_PERIOD_S = 47.0
LIDAR_DEGRADED_WINDOW_S = 6.0
ENCODER_DEGRADED_PERIOD_S = 83.0
ENCODER_DEGRADED_WINDOW_S = 5.0

# --- Battery model bounds (7S pack: 3.0 V .. 4.2 V per cell) ---
PACK_MIN_VOLTAGE = 21.0
PACK_MAX_VOLTAGE = 29.4


def _mps_to_rpm(velocity_mps: float) -> float:
    """Converts a linear wheel velocity to wheel RPM for the mock geometry."""
    return velocity_mps / (2.0 * math.pi * WHEEL_RADIUS_M) * 60.0


class MockTelemetryGenerator:
    """Produces deterministic, smoothly varying mock rover telemetry.

    The generator is driven by an explicit time step rather than by wall clock, so
    tests can advance it to any point in the profile without sleeping, and the UI
    gets the same profile on every run.
    """

    def __init__(
        self,
        seed: int = 1337,
        # Slightly below full so the load ripple is visible immediately instead of
        # being clamped flat at 100% for the first seconds of the profile.
        battery_start_pct: float = 98.5,
        battery_drain_pct_per_minute: float = 1.5,
        default_dt: float = 0.05,
        latency_baseline_ms: float = 4.2,
    ):
        self.seed = seed
        self.battery_start_pct = max(0.0, min(100.0, battery_start_pct))
        self.battery_drain_pct_per_minute = battery_drain_pct_per_minute
        self.default_dt = default_dt
        self.latency_baseline_ms = latency_baseline_ms

        self._rng = random.Random(seed)
        self._elapsed = 0.0
        self._yaw_deg = 45.0

    @property
    def elapsed_seconds(self) -> float:
        """Total mock mission time advanced so far."""
        return self._elapsed

    def reset(self) -> None:
        """Restarts the profile from t=0 with the original seed."""
        self._rng = random.Random(self.seed)
        self._elapsed = 0.0
        self._yaw_deg = 45.0

    # -- Individual channels -------------------------------------------------

    def _battery_percentage(self) -> float:
        """Slow linear drain with a small load ripple, clamped to 0..100."""
        drained = self.battery_start_pct - (self.battery_drain_pct_per_minute * self._elapsed / 60.0)
        ripple = 0.35 * math.sin(2.0 * math.pi * self._elapsed / 19.0)
        return max(0.0, min(100.0, drained + ripple))

    def _linear_velocity(self) -> float:
        """Smooth low-speed profile in a plausible 0.09 .. 0.55 m/s band."""
        return (
            0.32
            + 0.18 * math.sin(2.0 * math.pi * self._elapsed / 23.0)
            + 0.05 * math.sin(2.0 * math.pi * self._elapsed / 6.5)
        )

    def _yaw_rate_deg_s(self) -> float:
        """Slow alternating turn rate, integrated into heading."""
        return 6.0 * math.sin(2.0 * math.pi * self._elapsed / 37.0)

    def _roll_deg(self) -> float:
        return (
            2.4 * math.sin(2.0 * math.pi * self._elapsed / 7.3)
            + 0.7 * math.sin(2.0 * math.pi * self._elapsed / 2.1)
        )

    def _pitch_deg(self) -> float:
        return (
            3.2 * math.sin(2.0 * math.pi * self._elapsed / 11.7 + 0.6)
            + 0.5 * math.sin(2.0 * math.pi * self._elapsed / 1.7)
        )

    def _latency_ms(self) -> float:
        """Baseline link latency with a slow swell and bounded seeded jitter."""
        swell = 1.6 * math.sin(2.0 * math.pi * self._elapsed / 13.0)
        jitter = self._rng.uniform(-0.5, 0.5)
        return max(0.8, self.latency_baseline_ms + swell + jitter)

    def _sensor_health(self) -> SensorHealthData:
        """Deterministic health profile: brief periodic degradation windows."""
        lidar = SensorStatus.ONLINE
        if (self._elapsed % LIDAR_DEGRADED_PERIOD_S) >= (
            LIDAR_DEGRADED_PERIOD_S - LIDAR_DEGRADED_WINDOW_S
        ):
            lidar = SensorStatus.DEGRADED

        encoders = SensorStatus.ONLINE
        if (self._elapsed % ENCODER_DEGRADED_PERIOD_S) >= (
            ENCODER_DEGRADED_PERIOD_S - ENCODER_DEGRADED_WINDOW_S
        ):
            encoders = SensorStatus.DEGRADED

        return SensorHealthData(
            imu=SensorStatus.ONLINE,
            encoders=encoders,
            lidar=lidar,
            timestamp=time.time(),
        )

    # -- Public API ----------------------------------------------------------

    def advance(self, dt: Optional[float] = None) -> RoverState:
        """Advances mock mission time by dt seconds and returns a telemetry packet.

        Only the telemetry payload blocks are populated. Connection state, mission
        mode and E-Stop remain the responsibility of the comms client, exactly as
        they will be for a real ROS 2 link.
        """
        step = self.default_dt if dt is None else dt
        self._elapsed += max(0.0, step)

        now = time.time()

        # Motion
        velocity = self._linear_velocity()
        yaw_rate = self._yaw_rate_deg_s()
        self._yaw_deg = (self._yaw_deg + yaw_rate * max(0.0, step)) % 360.0

        yaw_rate_rad = math.radians(yaw_rate)
        left_mps = velocity - yaw_rate_rad * TRACK_WIDTH_M / 2.0
        right_mps = velocity + yaw_rate_rad * TRACK_WIDTH_M / 2.0

        # Power (draw scales with commanded speed, voltage sags under load)
        percentage = self._battery_percentage()
        current = 5.0 + 9.0 * abs(velocity) + 1.5 * math.sin(2.0 * math.pi * self._elapsed / 5.0)
        voltage = PACK_MIN_VOLTAGE + (PACK_MAX_VOLTAGE - PACK_MIN_VOLTAGE) * (percentage / 100.0)
        voltage -= 0.045 * current

        # Jetson diagnostics
        cpu = 34.0 + 11.0 * math.sin(2.0 * math.pi * self._elapsed / 17.0) + 4.0 * math.sin(
            2.0 * math.pi * self._elapsed / 3.1
        )
        gpu = 26.0 + 15.0 * math.sin(2.0 * math.pi * self._elapsed / 13.0)
        ram = 47.0 + 5.0 * math.sin(2.0 * math.pi * self._elapsed / 53.0)
        jetson_temp = 44.0 + 5.0 * math.sin(2.0 * math.pi * self._elapsed / 71.0) + 0.08 * cpu

        latency = self._latency_ms()

        return RoverState(
            heartbeat_latency_ms=latency,
            last_heartbeat_time=now,
            imu=IMUData(
                roll_deg=self._roll_deg(),
                pitch_deg=self._pitch_deg(),
                yaw_deg=self._yaw_deg,
                timestamp=now,
            ),
            battery=BatteryData(
                voltage=voltage,
                current_amps=current,
                percentage=percentage,
                cell_temperature_c=28.0
                + 6.0 * math.sin(2.0 * math.pi * self._elapsed / 61.0)
                + 0.2 * current,
                timestamp=now,
            ),
            drive=DriveData(
                linear_velocity=velocity,
                angular_velocity=yaw_rate_rad,
                left_wheel_rpm=_mps_to_rpm(left_mps),
                right_wheel_rpm=_mps_to_rpm(right_mps),
                motor_currents=[
                    current / 4.0 + 0.3 * math.sin(2.0 * math.pi * self._elapsed / (4.0 + i))
                    for i in range(4)
                ],
                timestamp=now,
            ),
            excavation=ExcavationData(timestamp=now),
            diagnostics=DiagnosticsData(
                cpu_usage_pct=max(0.0, min(100.0, cpu)),
                gpu_usage_pct=max(0.0, min(100.0, gpu)),
                ram_usage_pct=max(0.0, min(100.0, ram)),
                jetson_temp_c=jetson_temp,
                last_heartbeat_timestamp=now,
            ),
            sensors=self._sensor_health(),
        )
