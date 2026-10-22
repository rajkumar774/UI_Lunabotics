"""End-to-end tests for plane ① over real loopback UDP sockets.

These run the actual `TeleopLink` against the actual `SafetyDaemon` — real
sockets, real threads, real 20 Hz timing. Nothing is mocked, because the whole
point of this plane is that it works when everything else is broken, and a
mocked socket would not demonstrate that.
"""

import os
import socket
import sys
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication

from mission_control.comms.teleop.link import TeleopLink
from mission_control.comms.teleop.protocol import ArmState, HealthFlag
from mission_control.comms.teleop.safety import SafetyConfig

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from rover_safety_daemon import MotorSink, SafetyDaemon  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RecordingMotors(MotorSink):
    """Captures what the daemon actually commanded."""

    def __init__(self):
        self.commands = []
        self.stops = 0
        self.autonomy_ticks = 0
        self.estop = False

    def drive(self, linear_x, angular_z, excavation):
        self.commands.append((linear_x, angular_z, excavation))

    def stop(self):
        self.stops += 1

    def release_to_autonomy(self):
        self.autonomy_ticks += 1

    def read_health(self):
        return (HealthFlag.MOTORS_OK | HealthFlag.IMU_OK, 25.6, 8.4)

    def read_estop(self):
        return self.estop

    @property
    def last_drive(self):
        return self.commands[-1] if self.commands else None

    @property
    def moving_commands(self):
        return [c for c in self.commands if c[0] or c[1]]


class Rig:
    """A daemon on a background thread plus a link pointed at it."""

    def __init__(self, config=None):
        self.motors = RecordingMotors()
        self.cmd_port = free_port()
        self.sts_port = free_port()
        self.daemon = SafetyDaemon(
            bind_host="127.0.0.1",
            command_port=self.cmd_port,
            status_port=self.sts_port,
            peer_host="127.0.0.1",
            motors=self.motors,
            config=config or SafetyConfig(),
        )
        self.thread = threading.Thread(target=self.daemon.run, daemon=True)
        self.thread.start()
        self.link = TeleopLink("127.0.0.1", self.cmd_port, self.sts_port)
        assert self.link.start()

    def wait_for_link(self, timeout=3.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.link.is_link_up:
                return True
            time.sleep(0.02)
        return False

    def wait_for_state(self, state, timeout=3.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.daemon.controller.arm_state is state:
                return True
            time.sleep(0.02)
        return False

    def close(self):
        self.link.stop()
        self.daemon.stop()
        self.thread.join(timeout=2.0)


@pytest.fixture
def rig(qapp):
    r = Rig()
    yield r
    r.close()


# --- The link itself ------------------------------------------------------


def test_link_comes_up_over_real_sockets(rig):
    assert rig.wait_for_link(), "no status frames received"
    assert rig.link.frames_sent > 0
    assert rig.link.frames_received > 0


def test_link_measures_round_trip_time(rig):
    assert rig.wait_for_link()
    time.sleep(0.3)
    rtt = rig.link.rtt_ms
    assert rtt is not None
    # Loopback, so this is scheduling overhead only.
    assert 0.0 <= rtt < 500.0


def test_transmit_is_unconditional(rig):
    """Frames must flow with no operator input: silence has to mean link loss."""
    assert rig.wait_for_link()
    before = rig.link.frames_sent
    time.sleep(0.5)
    sent = rig.link.frames_sent - before
    # 20 Hz for 0.5 s, with generous slack for CI timing.
    assert sent >= 5, f"only {sent} frames in 500 ms"


def test_rover_state_is_reported_back(rig):
    assert rig.wait_for_link()
    assert rig.link.arm_state is ArmState.SAFE


# --- Arming and driving ---------------------------------------------------


def test_arming_round_trip(rig):
    assert rig.wait_for_link()
    rig.link.request_arm()

    assert rig.wait_for_state(ArmState.TELEOP_ARMED), "rover did not arm"
    deadline = time.time() + 2.0
    while time.time() < deadline and rig.link.arm_state is not ArmState.TELEOP_ARMED:
        time.sleep(0.02)
    assert rig.link.arm_state is ArmState.TELEOP_ARMED


def test_velocity_reaches_the_motors(rig):
    assert rig.wait_for_link()
    rig.link.request_arm()
    assert rig.wait_for_state(ArmState.TELEOP_ARMED)

    rig.link.set_velocity(0.42, -0.15)
    time.sleep(0.4)

    moving = rig.motors.moving_commands
    assert moving, "no motion commands reached the motor sink"
    lin, ang, _ = moving[-1]
    assert lin == pytest.approx(0.42, abs=1e-5)
    assert ang == pytest.approx(-0.15, abs=1e-5)


def test_motors_inhibited_before_arming(rig):
    """Setting a velocity without arming must move nothing."""
    assert rig.wait_for_link()
    rig.link.set_velocity(0.9, 0.9)
    time.sleep(0.4)

    assert rig.motors.moving_commands == []
    assert rig.motors.stops > 0


def test_disarm_stops_the_rover(rig):
    assert rig.wait_for_link()
    rig.link.request_arm()
    assert rig.wait_for_state(ArmState.TELEOP_ARMED)
    rig.link.set_velocity(0.4, 0.0)
    time.sleep(0.2)

    rig.link.request_safe()
    assert rig.wait_for_state(ArmState.SAFE)
    count = len(rig.motors.moving_commands)
    time.sleep(0.3)
    assert len(rig.motors.moving_commands) == count, "still driving after disarm"


# --- The behaviour this plane exists for ----------------------------------


def test_dead_man_fires_when_the_operator_station_goes_silent(qapp):
    """The whole point: kill the sender and the rover stops by itself."""
    rig = Rig(config=SafetyConfig(zero_velocity_ms=200, disarm_ms=600,
                                  latch_stopped_ms=1_500))
    try:
        assert rig.wait_for_link()
        rig.link.request_arm()
        assert rig.wait_for_state(ArmState.TELEOP_ARMED)
        rig.link.set_velocity(0.5, 0.0)
        time.sleep(0.2)
        assert rig.motors.moving_commands

        # Hard-stop the transmitter without a graceful disarm, exactly as a
        # laptop crash or a radio drop would.
        cut_at = time.monotonic()
        rig.link._running = False

        # Motion is allowed to continue up to the first dead-man stage (200 ms
        # here) -- that tolerance is deliberate, so Wi-Fi jitter does not cause
        # a nuisance stop. Past it, nothing more may reach the motors.
        time.sleep(0.4)
        settled = len(rig.motors.moving_commands)
        elapsed_to_settle = time.monotonic() - cut_at

        time.sleep(0.4)
        assert len(rig.motors.moving_commands) == settled, "kept driving while silent"
        assert elapsed_to_settle < 1.0, "took too long to stop applying velocity"

        assert rig.wait_for_state(ArmState.SAFE, timeout=2.0), "did not disarm"
        assert rig.wait_for_state(ArmState.STOPPED, timeout=3.0), "did not latch STOPPED"
    finally:
        rig.close()


def test_motion_persists_through_brief_jitter_then_stops(qapp):
    """The tolerance window is a feature, not a gap: it must exist and end."""
    rig = Rig(config=SafetyConfig(zero_velocity_ms=300, disarm_ms=5_000,
                                  latch_stopped_ms=30_000))
    try:
        assert rig.wait_for_link()
        rig.link.request_arm()
        assert rig.wait_for_state(ArmState.TELEOP_ARMED)
        rig.link.set_velocity(0.5, 0.0)
        time.sleep(0.2)

        rig.link._running = False
        time.sleep(0.15)   # inside the 300 ms window
        during = len(rig.motors.moving_commands)
        assert during > 0

        time.sleep(0.6)    # well past it
        after = len(rig.motors.moving_commands)
        time.sleep(0.3)
        assert len(rig.motors.moving_commands) == after

        # Still armed: a short dropout must not force a re-arm.
        assert rig.daemon.controller.arm_state is ArmState.TELEOP_ARMED
    finally:
        rig.close()


def test_software_stop_latches_over_the_wire(rig):
    assert rig.wait_for_link()
    rig.link.request_arm()
    assert rig.wait_for_state(ArmState.TELEOP_ARMED)

    rig.link.software_stop()
    assert rig.wait_for_state(ArmState.STOPPED)

    # A bare re-arm must not resume motion (guidebook 4.3.3).
    rig.link.request_arm()
    time.sleep(0.3)
    assert rig.daemon.controller.arm_state is ArmState.STOPPED


def test_stop_is_cleared_only_through_safe(rig):
    assert rig.wait_for_link()
    rig.link.request_arm()
    assert rig.wait_for_state(ArmState.TELEOP_ARMED)
    rig.link.software_stop()
    assert rig.wait_for_state(ArmState.STOPPED)

    rig.link.clear_software_stop()
    assert rig.wait_for_state(ArmState.SAFE)
    rig.link.request_arm()
    assert rig.wait_for_state(ArmState.TELEOP_ARMED)


def test_autonomy_engages_and_releases_the_wheel(rig):
    assert rig.wait_for_link()
    rig.link.request_arm()
    assert rig.wait_for_state(ArmState.TELEOP_ARMED)

    rig.link.request_autonomy()
    assert rig.wait_for_state(ArmState.AUTONOMY)
    time.sleep(0.3)
    assert rig.motors.autonomy_ticks > 0


def test_operator_input_during_autonomy_takes_back_control(rig):
    assert rig.wait_for_link()
    rig.link.request_arm()
    assert rig.wait_for_state(ArmState.TELEOP_ARMED)
    rig.link.request_autonomy()
    assert rig.wait_for_state(ArmState.AUTONOMY)

    rig.link.request_arm()
    rig.link.set_velocity(0.3, 0.0)
    assert rig.wait_for_state(ArmState.TELEOP_ARMED)


def test_stop_disarms_on_shutdown(rig):
    """Closing the UI must leave the rover safe, not coasting."""
    assert rig.wait_for_link()
    rig.link.request_arm()
    assert rig.wait_for_state(ArmState.TELEOP_ARMED)

    rig.link.stop()
    assert rig.wait_for_state(ArmState.SAFE, timeout=2.0)


def test_corrupt_traffic_on_the_port_is_ignored(rig):
    """Stray datagrams must not disturb the link or the rover."""
    assert rig.wait_for_link()
    rig.link.request_arm()
    assert rig.wait_for_state(ArmState.TELEOP_ARMED)

    noise = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for payload in (b"", b"hello", b"\x00" * 33, os.urandom(33), os.urandom(200)):
        noise.sendto(payload, ("127.0.0.1", rig.cmd_port))
    noise.close()

    time.sleep(0.3)
    assert rig.daemon.controller.arm_state is ArmState.TELEOP_ARMED
    assert rig.link.is_link_up
