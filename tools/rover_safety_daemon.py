#!/usr/bin/env python3
"""Rover-side safety and teleop daemon — plane ① (runs on the Jetson Orin Nano).

See ``docs/comms_architecture.md`` §4.1 and §5.

This is the process that decides whether the rover is allowed to move. It is
deliberately the smallest, dullest thing on the vehicle:

* **Standard library only.** No ROS, no Qt, no numpy. It must stay runnable when
  the ROS graph is broken, half-started, or has never come up at all.
* **Starts before everything else** (``rover-safety.service`` before
  ``rover-ros.service``). A rover that can drive but not report telemetry still
  scores; the reverse does not.
* **Real-time scheduled.** On a six-core Orin Nano also running two NVENC
  sessions, RealSense depth and Nav2, a nice-level daemon can miss its 20 Hz
  deadline under exactly the load that matters — and the dead-man would then
  trip on a link that was perfectly healthy.

Wire it to real hardware by passing a ``MotorSink`` implementation; the default
only logs, so the daemon is safe to run on a bench.

Deployment::

    sudo install -m755 rover_safety_daemon.py /opt/rover/
    sudo install -m644 rover-safety.service /etc/systemd/system/
    sudo systemctl enable --now rover-safety

Bench test from the laptop, with no rover attached::

    python3 tools/rover_safety_daemon.py --host 127.0.0.1 --log-level DEBUG
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import signal
import socket
import sys
import time
from typing import Optional, Tuple

# Allow running straight from a checkout without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from mission_control.comms.teleop.protocol import (  # noqa: E402
    COMMAND_PORT,
    COMMAND_RATE_HZ,
    COMMAND_SIZE,
    STATUS_PORT,
    ArmState,
    CommandFrame,
    HealthFlag,
    StatusFrame,
    monotonic_ms,
)
from mission_control.comms.teleop.safety import (  # noqa: E402
    ControlSource,
    SafetyConfig,
    SafetyController,
    SafetyOutput,
)

logger = logging.getLogger("rover.daemon")

#: DSCP EF (46) << 2. Marks control traffic as Voice so the AP's WMM queues
#: put it ahead of a video burst. One of the cheapest reliability wins we get.
IPTOS_DSCP_EF = 0xB8

_MCL_CURRENT = 1
_MCL_FUTURE = 2


class MotorSink:
    """Where motion commands actually go. Replace with the real driver.

    Kept as a seam so the daemon can be bench-tested, and so the safety logic
    is never entangled with a specific motor controller's API.
    """

    def drive(self, linear_x: float, angular_z: float, excavation: int) -> None:
        logger.debug(f"drive lin={linear_x:+.3f} ang={angular_z:+.3f} exc={excavation}")

    def stop(self) -> None:
        logger.debug("stop")

    def release_to_autonomy(self) -> None:
        """Hands control to the onboard autonomy stack without inhibiting motors."""
        logger.debug("autonomy has the wheel")

    def read_health(self) -> Tuple[int, float, float]:
        """Returns (health bitfield, battery volts, current amps)."""
        return (
            HealthFlag.MOTORS_OK | HealthFlag.IMU_OK | HealthFlag.ENCODERS_OK,
            0.0,
            0.0,
        )

    def read_estop(self) -> bool:
        """Reads the hardware E-Stop line, where one is wired to a GPIO."""
        return False


def apply_realtime_scheduling(priority: int = 80, cpu: Optional[int] = None) -> None:
    """Pins this process to a real-time priority, and optionally a core.

    Failures are logged and tolerated: a daemon running at normal priority is
    far better than one that refused to start because it was not run as root.
    """
    try:
        param = os.sched_param(priority)
        os.sched_setscheduler(0, os.SCHED_FIFO, param)
        logger.info(f"SCHED_FIFO priority {priority} acquired.")
    except (PermissionError, OSError) as exc:
        logger.warning(f"Could not set SCHED_FIFO ({exc}); running at normal priority.")

    if cpu is not None:
        try:
            os.sched_setaffinity(0, {cpu})
            logger.info(f"Pinned to CPU {cpu}.")
        except OSError as exc:
            logger.warning(f"Could not pin to CPU {cpu}: {exc}")

    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.mlockall(_MCL_CURRENT | _MCL_FUTURE) == 0:
            logger.info("Memory locked; daemon will not be swapped out.")
        else:
            logger.warning(f"mlockall failed: {os.strerror(ctypes.get_errno())}")
    except Exception as exc:
        logger.warning(f"mlockall unavailable: {exc}")


class SafetyDaemon:
    """Sockets and timing around a SafetyController."""

    def __init__(
        self,
        bind_host: str = "0.0.0.0",
        command_port: int = COMMAND_PORT,
        status_port: int = STATUS_PORT,
        peer_host: Optional[str] = None,
        motors: Optional[MotorSink] = None,
        config: Optional[SafetyConfig] = None,
    ):
        self.controller = SafetyController(config=config or SafetyConfig())
        self.motors = motors or MotorSink()
        self.status_port = status_port
        self._peer_host = peer_host
        self._running = False

        self._rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._rx.bind((bind_host, command_port))
        # Non-blocking: the loop is driven by its own 20 Hz clock, never by
        # the arrival of a packet. Silence has to be observable.
        self._rx.setblocking(False)

        self._tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._tx.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, IPTOS_DSCP_EF)
            self._rx.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, IPTOS_DSCP_EF)
        except OSError as exc:
            logger.warning(f"Could not set DSCP EF: {exc}")

        logger.info(f"Listening for commands on {bind_host}:{command_port}")

    # -- Main loop -----------------------------------------------------------

    def run(self) -> None:
        self._running = True
        period = 1.0 / COMMAND_RATE_HZ
        next_tick = time.monotonic()

        while self._running:
            self._drain_commands()

            now = monotonic_ms()
            self.controller.set_estop(self.motors.read_estop(), now)
            output = self.controller.tick(now)
            self._apply(output)
            self._send_status(output, now)

            next_tick += period
            sleep = next_tick - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                # Overran the budget: resync rather than spiral into catch-up.
                next_tick = time.monotonic()

        self.motors.stop()
        logger.info("Safety daemon stopped; motors inhibited.")

    def stop(self) -> None:
        self._running = False

    # -- Internals -----------------------------------------------------------

    def _drain_commands(self) -> None:
        """Reads every queued datagram, so we always act on the newest.

        Draining rather than reading one per tick matters: if the loop is ever
        late, the socket buffer holds several frames and acting on the oldest
        would apply a stale velocity.
        """
        while True:
            try:
                data, addr = self._rx.recvfrom(COMMAND_SIZE * 4)
            except BlockingIOError:
                return
            except OSError as exc:
                logger.warning(f"recv error: {exc}")
                return

            frame = CommandFrame.unpack(data)
            if frame is None:
                continue
            if self._peer_host is None:
                self._peer_host = addr[0]
                logger.info(f"Operator station at {self._peer_host}")
            self.controller.on_command(frame, monotonic_ms())

    def _apply(self, output: SafetyOutput) -> None:
        if output.source is ControlSource.TELEOP:
            self.motors.drive(output.linear_x, output.angular_z, output.excavation)
        elif output.source is ControlSource.AUTONOMY:
            self.motors.release_to_autonomy()
        else:
            self.motors.stop()

    def _send_status(self, output: SafetyOutput, now_ms: int) -> None:
        if self._peer_host is None:
            return
        last = self.controller._last_command
        health, volts, amps = self.motors.read_health()
        frame = StatusFrame(
            seq_echo=last.seq if last else 0,
            # Echo the operator's own timestamp untouched: that is what makes
            # the round-trip measurement independent of clock sync.
            t_echo_ms=last.t_sent_ms if last else 0,
            arm_state=output.arm_state,
            estop_engaged=self.controller.estop_engaged,
            health=health,
            battery_volts=volts,
            current_amps=amps,
        )
        try:
            self._tx.sendto(frame.pack(), (self._peer_host, self.status_port))
        except OSError as exc:
            logger.debug(f"status send failed: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rover safety / teleop daemon (plane 1)")
    parser.add_argument("--host", default="0.0.0.0", help="bind address")
    parser.add_argument("--command-port", type=int, default=COMMAND_PORT)
    parser.add_argument("--status-port", type=int, default=STATUS_PORT)
    parser.add_argument("--peer", default=None, help="operator station IP (else learned)")
    parser.add_argument("--rt-priority", type=int, default=80)
    parser.add_argument("--cpu", type=int, default=None, help="pin to this core")
    parser.add_argument("--no-realtime", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.no_realtime:
        apply_realtime_scheduling(args.rt_priority, args.cpu)

    daemon = SafetyDaemon(
        bind_host=args.host,
        command_port=args.command_port,
        status_port=args.status_port,
        peer_host=args.peer,
    )

    def shutdown(_signum, _frame):
        logger.info("Signal received; inhibiting motors and exiting.")
        daemon.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    daemon.run()

    for ts, old, new, reason in daemon.controller.transitions:
        logger.info(f"  t={ts}ms  {old.name} -> {new.name}  ({reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
