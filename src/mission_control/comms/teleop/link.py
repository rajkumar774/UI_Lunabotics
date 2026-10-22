"""Mission Control side of the safety/teleop plane (plane ①).

See ``docs/comms_architecture.md`` §4.1.

Sends a command frame at a fixed 20 Hz whether or not the operator is touching
anything, and receives the rover's authoritative status. Both run on their own
threads so neither can be delayed by the Qt event loop — a repaint stall must
never look like a link loss to the rover's dead-man.

Three properties this class exists to guarantee:

* **The transmit cadence is unconditional.** Silence on the wire means "no
  link", never "no input". That is the entire basis of the dead-man.
* **The rover's reported state wins.** ``arm_state`` here is whatever the rover
  last said, not what the operator asked for. The UI renders the rover.
* **It shares nothing with ROS 2, video or SSH.** A raw socket, a struct, and a
  thread.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, Signal

from mission_control.comms.teleop.protocol import (
    COMMAND_PERIOD_S,
    COMMAND_PORT,
    STATUS_PORT,
    STATUS_SIZE,
    ArmState,
    CommandFlag,
    CommandFrame,
    StatusFrame,
    monotonic_ms,
    new_session_id,
)
from mission_control.core.logger import get_logger

logger = get_logger("teleop_link")

#: DSCP EF (46) << 2 — Voice class, so teleop outranks a video burst on the AP.
IPTOS_DSCP_EF = 0xB8

#: No status for this long and we declare the link down in the UI. Generous
#: next to the rover's own 300 ms dead-man: the rover protects itself, while
#: this threshold only drives what the operator is shown.
LINK_TIMEOUT_MS = 1_000

#: Round-trip samples kept for the displayed average.
RTT_WINDOW = 20


class TeleopLink(QObject):
    """UDP control link to the rover's safety daemon."""

    #: (StatusFrame) authoritative rover state, ~20 Hz
    status_received = Signal(object)
    #: (bool) link up / down, edge-triggered
    link_changed = Signal(bool)
    #: (float) smoothed round-trip time in milliseconds
    rtt_updated = Signal(float)
    #: (str) transport-level problem worth showing the operator
    error_occurred = Signal(str)

    def __init__(
        self,
        rover_host: str,
        command_port: int = COMMAND_PORT,
        status_port: int = STATUS_PORT,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.rover_host = rover_host
        self.command_port = command_port
        self.status_port = status_port

        self._session = new_session_id()
        self._seq = 0
        self._lock = threading.Lock()

        # Requested setpoints, overwritten by the input source at any rate; the
        # sender thread samples whatever is current at each 20 Hz tick.
        self._linear_x = 0.0
        self._angular_z = 0.0
        self._excavation = 0
        self._arm_request = ArmState.SAFE
        self._flags = 0

        self._last_status: Optional[StatusFrame] = None
        self._last_status_ms: Optional[int] = None
        self._link_up = False
        self._rtt_samples: list[float] = []

        self._tx_sock: Optional[socket.socket] = None
        self._rx_sock: Optional[socket.socket] = None
        self._tx_thread: Optional[threading.Thread] = None
        self._rx_thread: Optional[threading.Thread] = None
        self._running = False

        self._frames_sent = 0
        self._frames_received = 0

    # -- Properties ----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_link_up(self) -> bool:
        return self._link_up

    @property
    def session(self) -> int:
        return self._session

    @property
    def frames_sent(self) -> int:
        return self._frames_sent

    @property
    def frames_received(self) -> int:
        return self._frames_received

    @property
    def arm_state(self) -> ArmState:
        """What the rover says it is — never what we asked for."""
        status = self._last_status
        return status.arm_state if status else ArmState.SAFE

    @property
    def rtt_ms(self) -> Optional[float]:
        if not self._rtt_samples:
            return None
        return sum(self._rtt_samples) / len(self._rtt_samples)

    @property
    def loss_ratio(self) -> float:
        """Fraction of sent frames with no corresponding status reply."""
        if self._frames_sent == 0:
            return 0.0
        return max(0.0, 1.0 - self._frames_received / self._frames_sent)

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> bool:
        """Opens the sockets and starts the 20 Hz transmit and receive threads."""
        if self._running:
            return True
        try:
            self._tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._rx_sock.bind(("0.0.0.0", self.status_port))
            self._rx_sock.settimeout(0.2)
            for sock in (self._tx_sock, self._rx_sock):
                try:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, IPTOS_DSCP_EF)
                except OSError:
                    pass  # not fatal; we simply lose WMM prioritisation
        except OSError as exc:
            logger.error(f"Teleop link could not open sockets: {exc}")
            self.error_occurred.emit(str(exc))
            self._close_sockets()
            return False

        self._running = True
        self._tx_thread = threading.Thread(target=self._tx_loop, name="teleop-tx", daemon=True)
        self._rx_thread = threading.Thread(target=self._rx_loop, name="teleop-rx", daemon=True)
        self._tx_thread.start()
        self._rx_thread.start()
        logger.info(
            f"Teleop link up: -> {self.rover_host}:{self.command_port}, "
            f"<- :{self.status_port}, session {self._session:#06x}"
        )
        return True

    def stop(self) -> None:
        """Sends a final disarm, then shuts the threads and sockets down."""
        if not self._running:
            return
        self.request_safe()
        try:
            self._send_once()
        except Exception:
            pass

        self._running = False
        for thread in (self._tx_thread, self._rx_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.0)
        self._tx_thread = self._rx_thread = None
        self._close_sockets()

        if self._link_up:
            self._link_up = False
            self.link_changed.emit(False)
        logger.info("Teleop link stopped.")

    def _close_sockets(self) -> None:
        for sock in (self._tx_sock, self._rx_sock):
            try:
                if sock is not None:
                    sock.close()
            except OSError:
                pass
        self._tx_sock = self._rx_sock = None

    # -- Commanding ----------------------------------------------------------

    def set_velocity(self, linear_x: float, angular_z: float) -> None:
        """Updates the setpoint. Sampled by the sender at the next tick."""
        with self._lock:
            self._linear_x = float(linear_x)
            self._angular_z = float(angular_z)

    def set_excavation(self, value: int) -> None:
        with self._lock:
            self._excavation = int(value)

    def request_arm(self) -> None:
        """Asks the rover to arm. Only takes effect as a rising edge."""
        with self._lock:
            self._arm_request = ArmState.TELEOP_ARMED
            self._flags &= ~CommandFlag.SOFTWARE_STOP

    def request_safe(self) -> None:
        """Disarms and zeroes the setpoint."""
        with self._lock:
            self._arm_request = ArmState.SAFE
            self._linear_x = 0.0
            self._angular_z = 0.0
            self._excavation = 0

    def request_autonomy(self) -> None:
        """Requests hands-free operation, with the acknowledgement flag set."""
        with self._lock:
            self._arm_request = ArmState.AUTONOMY
            self._flags |= CommandFlag.AUTONOMY_ACK
            self._linear_x = 0.0
            self._angular_z = 0.0

    def software_stop(self) -> None:
        """Latches the rover's stop. Cleared only by an explicit disarm."""
        with self._lock:
            self._arm_request = ArmState.STOPPED
            self._flags |= CommandFlag.SOFTWARE_STOP
            self._linear_x = 0.0
            self._angular_z = 0.0
            self._excavation = 0
        logger.warning("Teleop link: SOFTWARE STOP latched.")

    def clear_software_stop(self) -> None:
        with self._lock:
            self._flags &= ~CommandFlag.SOFTWARE_STOP
            self._arm_request = ArmState.SAFE

    def set_low_bandwidth(self, enabled: bool) -> None:
        with self._lock:
            if enabled:
                self._flags |= CommandFlag.LOW_BANDWIDTH
            else:
                self._flags &= ~CommandFlag.LOW_BANDWIDTH

    # -- Threads -------------------------------------------------------------

    def _build_frame(self) -> CommandFrame:
        with self._lock:
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            return CommandFrame(
                session=self._session,
                seq=self._seq,
                t_sent_ms=monotonic_ms(),
                arm_request=self._arm_request,
                linear_x=self._linear_x,
                angular_z=self._angular_z,
                excavation=self._excavation,
                flags=self._flags,
            )

    def _send_once(self) -> None:
        if self._tx_sock is None:
            return
        self._tx_sock.sendto(self._build_frame().pack(), (self.rover_host, self.command_port))
        self._frames_sent += 1

    def _tx_loop(self) -> None:
        """Transmits unconditionally at 20 Hz on an absolute schedule.

        Absolute rather than ``sleep(period)`` so transmission does not drift
        with the time spent packing each frame.
        """
        next_tick = time.monotonic()
        while self._running:
            try:
                self._send_once()
            except OSError as exc:
                logger.debug(f"teleop tx failed: {exc}")

            self._evaluate_link()

            next_tick += COMMAND_PERIOD_S
            sleep = next_tick - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_tick = time.monotonic()

    def _rx_loop(self) -> None:
        while self._running:
            try:
                data, _addr = self._rx_sock.recvfrom(STATUS_SIZE * 4)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                if self._running:
                    continue
                return

            frame = StatusFrame.unpack(data)
            if frame is None:
                continue

            now = monotonic_ms()
            self._frames_received += 1
            self._last_status = frame
            self._last_status_ms = now

            if frame.t_echo_ms:
                rtt = float(now - frame.t_echo_ms)
                # Discard absurd values rather than letting a stale echo or a
                # monotonic reset poison the displayed average.
                if 0.0 <= rtt < 10_000.0:
                    self._rtt_samples.append(rtt)
                    if len(self._rtt_samples) > RTT_WINDOW:
                        self._rtt_samples.pop(0)
                    self.rtt_updated.emit(self.rtt_ms)

            self.status_received.emit(frame)

    def _evaluate_link(self) -> None:
        now = monotonic_ms()
        fresh = (
            self._last_status_ms is not None
            and (now - self._last_status_ms) < LINK_TIMEOUT_MS
        )
        if fresh != self._link_up:
            self._link_up = fresh
            logger.info(f"Teleop link {'UP' if fresh else 'DOWN'}.")
            if not fresh:
                self._rtt_samples.clear()
            self.link_changed.emit(fresh)
