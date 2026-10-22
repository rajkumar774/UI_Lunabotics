"""Tests for the deterministic mock telemetry generator (MOCK DATA ONLY)."""

import pytest

from mission_control.telemetry.mock_generator import (
    ENCODER_DEGRADED_PERIOD_S,
    LIDAR_DEGRADED_PERIOD_S,
    LIDAR_DEGRADED_WINDOW_S,
    MockTelemetryGenerator,
    _mps_to_rpm,
)
from mission_control.telemetry.state import RoverState, SensorStatus


def _sample(generator: MockTelemetryGenerator, count: int, dt: float = 0.2) -> list[RoverState]:
    return [generator.advance(dt) for _ in range(count)]


def test_generator_populates_every_required_channel():
    """Every telemetry channel the UI renders must carry a plausible value."""
    packet = MockTelemetryGenerator().advance(1.0)

    assert 0.0 < packet.battery.percentage <= 100.0
    assert 21.0 <= packet.battery.voltage <= 29.4
    assert packet.battery.current_amps > 0.0
    assert 0.0 < packet.drive.linear_velocity < 1.0
    assert packet.drive.left_wheel_rpm != 0.0
    assert packet.drive.right_wheel_rpm != 0.0
    assert abs(packet.imu.roll_deg) < 10.0
    assert abs(packet.imu.pitch_deg) < 10.0
    assert 0.0 <= packet.imu.yaw_deg < 360.0
    assert packet.heartbeat_latency_ms > 0.0
    assert packet.sensors.imu == SensorStatus.ONLINE
    assert 0.0 < packet.diagnostics.jetson_temp_c < 100.0
    assert 0.0 <= packet.diagnostics.cpu_usage_pct <= 100.0


def test_values_change_gradually_rather_than_jumping():
    """Consecutive samples must differ, but only by small smooth increments."""
    samples = _sample(MockTelemetryGenerator(), 25, dt=0.2)

    speeds = [s.drive.linear_velocity for s in samples]
    rolls = [s.imu.roll_deg for s in samples]

    # Values genuinely vary over the window...
    assert max(speeds) - min(speeds) > 0.01
    assert max(rolls) - min(rolls) > 0.1

    # ...but never jump the way uncontrolled random values would.
    assert all(abs(b - a) < 0.05 for a, b in zip(speeds, speeds[1:]))
    assert all(abs(b - a) < 1.5 for a, b in zip(rolls, rolls[1:]))


def test_generator_is_deterministic_for_a_given_seed():
    """Two identically seeded generators stepped identically must agree exactly."""
    left = _sample(MockTelemetryGenerator(seed=7), 12)
    right = _sample(MockTelemetryGenerator(seed=7), 12)

    for a, b in zip(left, right):
        assert a.battery.percentage == b.battery.percentage
        assert a.drive.linear_velocity == b.drive.linear_velocity
        assert a.imu.yaw_deg == b.imu.yaw_deg
        assert a.heartbeat_latency_ms == b.heartbeat_latency_ms


def test_reset_restarts_the_profile():
    generator = MockTelemetryGenerator(seed=99)
    first = generator.advance(1.0)
    _sample(generator, 10, dt=1.0)

    generator.reset()
    assert generator.elapsed_seconds == 0.0
    replayed = generator.advance(1.0)

    assert replayed.drive.linear_velocity == first.drive.linear_velocity
    assert replayed.heartbeat_latency_ms == first.heartbeat_latency_ms


def test_battery_drains_slowly_over_mission_time():
    generator = MockTelemetryGenerator(battery_start_pct=100.0, battery_drain_pct_per_minute=1.5)
    start = generator.advance(0.0).battery.percentage
    after_ten_minutes = generator.advance(600.0).battery.percentage

    assert after_ten_minutes < start
    # 1.5 %/min for 10 minutes, allowing for the small load ripple.
    assert 13.0 < (start - after_ten_minutes) < 17.0


def test_voltage_follows_charge_level():
    """Pack voltage must fall as the pack depletes, not float independently."""
    generator = MockTelemetryGenerator(battery_start_pct=100.0, battery_drain_pct_per_minute=10.0)
    full = generator.advance(0.0)
    depleted = generator.advance(300.0)

    assert depleted.battery.percentage < full.battery.percentage
    assert depleted.battery.voltage < full.battery.voltage


def test_wheel_rpm_stays_consistent_with_speed():
    """Mean wheel RPM must correspond to the reported linear velocity."""
    for packet in _sample(MockTelemetryGenerator(), 10, dt=0.5):
        expected_rpm = _mps_to_rpm(packet.drive.linear_velocity)
        assert packet.drive.average_wheel_rpm == pytest.approx(expected_rpm, abs=0.01)


def test_sensor_degradation_windows_are_deterministic():
    """Sensor health must follow the documented profile, not random flapping."""
    generator = MockTelemetryGenerator()

    early = generator.advance(1.0)
    assert early.sensors.lidar == SensorStatus.ONLINE
    assert early.sensors.encoders == SensorStatus.ONLINE

    # Step into the LiDAR degradation window near the end of its period.
    inside = generator.advance(LIDAR_DEGRADED_PERIOD_S - LIDAR_DEGRADED_WINDOW_S / 2.0 - 1.0)
    assert inside.sensors.lidar == SensorStatus.DEGRADED
    assert inside.sensors.imu == SensorStatus.ONLINE

    # And back out of it on the next cycle.
    recovered = generator.advance(LIDAR_DEGRADED_WINDOW_S)
    assert recovered.sensors.lidar == SensorStatus.ONLINE
    assert generator.elapsed_seconds < ENCODER_DEGRADED_PERIOD_S
