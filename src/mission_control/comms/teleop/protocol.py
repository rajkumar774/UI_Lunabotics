"""Wire format for the safety/teleop plane (plane ①).

See ``docs/comms_architecture.md`` §4.1. This is the one link the rover's safety
depends on, so the format is deliberately dull: fixed-size little-endian frames,
no allocation, no optional fields, no parser state.

**This module imports nothing outside the standard library — no Qt, no ROS.**
That is a requirement, not a coincidence: the same file runs inside the Mission
Control UI and inside the Jetson safety daemon, and the rover must never need
PySide6 installed to be drivable.

Design rules worth not relitigating later:

* **UDP, and losses are ignored.** A drive command is *state*, not an event: the
  newest one supersedes every earlier one. Retransmitting a stale command is
  worse than dropping it, which is why this is not TCP.
* **Sequence numbers only ever move forward.** A receiver discards any frame
  older than the newest it has seen, so reordering can never apply an old
  command on top of a new one.
* **The sender's own clock is echoed back untouched.** Round-trip time is then
  exact without the two hosts agreeing on wall time — no NTP dependency.
* **Silence is meaningful.** Commands go out at a fixed rate whether or not the
  operator is touching anything, so "no frame" unambiguously means "no link"
  and the dead-man can rely on it.
"""

from __future__ import annotations

import random
import struct
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

#: Frame marker. Rejects stray traffic on a shared port before any parsing.
MAGIC = b"AGLV"

#: Bumped on any layout change. Mismatched versions are dropped, never guessed at.
PROTOCOL_VERSION = 1

#: Nominal command rate (Hz) and period (seconds).
COMMAND_RATE_HZ = 20
COMMAND_PERIOD_S = 1.0 / COMMAND_RATE_HZ

#: Default ports (see architecture doc §2).
COMMAND_PORT = 5000   # MCC -> Jetson
STATUS_PORT = 5001    # Jetson -> MCC

#: ``<`` keeps these byte-exact across architectures and suppresses padding,
#: which matters because an x86 laptop talks to an aarch64 Jetson.
_COMMAND_STRUCT = struct.Struct("<4sBHIQBffbHH")
_STATUS_STRUCT = struct.Struct("<4sBIQBBHffBH")

COMMAND_SIZE = _COMMAND_STRUCT.size   # 33 bytes
STATUS_SIZE = _STATUS_STRUCT.size     # 32 bytes


class ArmState(IntEnum):
    """Authoritative rover state. The Jetson owns it; the MCC only requests."""
    SAFE = 0            # motors inhibited, listening
    TELEOP_ARMED = 1    # obeying commands, dead-man live
    AUTONOMY = 2        # rover drives itself, teleop dead-man relaxed
    STOPPED = 3         # latched; only a deliberate re-arm leaves


class CommandFlag(IntEnum):
    """Bit flags in a command frame."""
    SOFTWARE_STOP = 1 << 0   # operator stop; latches STOPPED
    AUTONOMY_ACK = 1 << 1    # operator confirms the hands-free announcement
    LOW_BANDWIDTH = 1 << 2   # request reduced video bitrate


class HealthFlag(IntEnum):
    """Bit flags in a status frame."""
    MOTORS_OK = 1 << 0
    IMU_OK = 1 << 1
    ENCODERS_OK = 1 << 2
    EXCAVATION_OK = 1 << 3
    ROS_STACK_UP = 1 << 4
    VIDEO_UP = 1 << 5
    UNDERVOLTAGE = 1 << 6
    OVERCURRENT = 1 << 7
    THERMAL_THROTTLE = 1 << 8


def monotonic_ms() -> int:
    """Monotonic milliseconds, used for timestamps and dead-man timing.

    Monotonic rather than wall clock so an NTP step mid-run cannot make the
    dead-man think it has been starved (or, worse, that it has not).
    """
    return int(time.monotonic() * 1000.0) & 0xFFFFFFFFFFFFFFFF


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE over ``data``.

    UDP already carries a checksum, but it is optional on IPv4 and weak; this
    costs microseconds and means a corrupt frame can never be read as a valid
    velocity command.
    """
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _clamp(value: float, limit: float) -> float:
    """Clamps to +/-limit, mapping NaN to 0.0.

    A NaN velocity reaching a motor controller is undefined behaviour at best,
    so it is filtered here rather than trusted to the far end.
    """
    if value != value:  # NaN
        return 0.0
    return max(-limit, min(limit, float(value)))


#: Envelope beyond which a command is treated as corrupt rather than clamped.
MAX_LINEAR_MS = 5.0
MAX_ANGULAR_RADS = 5.0


@dataclass
class CommandFrame:
    """MCC -> Jetson. Drive setpoints plus the requested arm state."""

    #: Randomised once per sender process. A change means the operator station
    #: restarted, so the receiver must forget the old sequence high-water mark.
    #: Without this, a mid-run UI restart resets seq to 0, every frame is
    #: rejected as stale forever, and the rover never recovers.
    session: int = 0
    seq: int = 0
    t_sent_ms: int = 0
    arm_request: ArmState = ArmState.SAFE
    linear_x: float = 0.0
    angular_z: float = 0.0
    excavation: int = 0
    flags: int = 0

    def has_flag(self, flag: CommandFlag) -> bool:
        return bool(self.flags & flag)

    @property
    def is_moving(self) -> bool:
        """True when this frame actually asks for motion."""
        return self.linear_x != 0.0 or self.angular_z != 0.0 or self.excavation != 0

    def pack(self) -> bytes:
        body = _COMMAND_STRUCT.pack(
            MAGIC,
            PROTOCOL_VERSION,
            self.session & 0xFFFF,
            self.seq & 0xFFFFFFFF,
            self.t_sent_ms & 0xFFFFFFFFFFFFFFFF,
            int(self.arm_request),
            _clamp(self.linear_x, MAX_LINEAR_MS),
            _clamp(self.angular_z, MAX_ANGULAR_RADS),
            max(-128, min(127, int(self.excavation))),
            self.flags & 0xFFFF,
            0,
        )[:-2]
        return body + struct.pack("<H", crc16_ccitt(body))

    @classmethod
    def unpack(cls, raw: bytes) -> Optional["CommandFrame"]:
        """Parses a frame, or returns None if it is not one we should act on.

        Returning None rather than raising keeps the receive loop branch-free;
        a bad frame is simply not a command, and the dead-man handles the rest.
        """
        if len(raw) != COMMAND_SIZE:
            return None
        (magic, version, session, seq, t_sent, arm,
         lin, ang, exc, flags, crc) = _COMMAND_STRUCT.unpack(raw)
        if magic != MAGIC or version != PROTOCOL_VERSION:
            return None
        if crc != crc16_ccitt(raw[:-2]):
            return None
        # Out-of-envelope values mean a corrupt-but-CRC-valid frame or a buggy
        # sender. Either way it is not something to hand to a motor controller.
        if abs(lin) > MAX_LINEAR_MS or abs(ang) > MAX_ANGULAR_RADS:
            return None
        if lin != lin or ang != ang:  # NaN
            return None
        try:
            arm_state = ArmState(arm)
        except ValueError:
            return None
        return cls(
            session=session,
            seq=seq,
            t_sent_ms=t_sent,
            arm_request=arm_state,
            linear_x=lin,
            angular_z=ang,
            excavation=exc,
            flags=flags,
        )


@dataclass
class StatusFrame:
    """Jetson -> MCC. Authoritative state, plus the echo that measures RTT."""

    seq_echo: int = 0
    t_echo_ms: int = 0
    arm_state: ArmState = ArmState.SAFE
    estop_engaged: bool = False
    health: int = 0
    battery_volts: float = 0.0
    current_amps: float = 0.0
    fault_code: int = 0

    def has_health(self, flag: HealthFlag) -> bool:
        return bool(self.health & flag)

    def pack(self) -> bytes:
        body = _STATUS_STRUCT.pack(
            MAGIC,
            PROTOCOL_VERSION,
            self.seq_echo & 0xFFFFFFFF,
            self.t_echo_ms & 0xFFFFFFFFFFFFFFFF,
            int(self.arm_state),
            1 if self.estop_engaged else 0,
            self.health & 0xFFFF,
            _clamp(self.battery_volts, 1e4),
            _clamp(self.current_amps, 1e4),
            max(0, min(255, int(self.fault_code))),
            0,
        )[:-2]
        return body + struct.pack("<H", crc16_ccitt(body))

    @classmethod
    def unpack(cls, raw: bytes) -> Optional["StatusFrame"]:
        if len(raw) != STATUS_SIZE:
            return None
        (magic, version, seq, t_echo, arm, estop, health,
         volts, amps, fault, crc) = _STATUS_STRUCT.unpack(raw)
        if magic != MAGIC or version != PROTOCOL_VERSION:
            return None
        if crc != crc16_ccitt(raw[:-2]):
            return None
        try:
            arm_state = ArmState(arm)
        except ValueError:
            return None
        return cls(
            seq_echo=seq,
            t_echo_ms=t_echo,
            arm_state=arm_state,
            estop_engaged=bool(estop),
            health=health,
            battery_volts=volts,
            current_amps=amps,
            fault_code=fault,
        )


def new_session_id() -> int:
    """A random 16-bit id for this sender process.

    Random rather than sequential so a quick restart cannot reuse the previous
    value and be mistaken for the same session.
    """
    return random.getrandbits(16)


def is_newer(seq: int, newest_seen: Optional[int]) -> bool:
    """True when ``seq`` should supersede ``newest_seen``.

    Comparison is modulo 2^32 so the counter can wrap during a long run without
    the link appearing to go permanently stale. A sequence more than half the
    space ahead is treated as old, which is the standard serial-number rule.
    """
    if newest_seen is None:
        return True
    return ((seq - newest_seen) & 0xFFFFFFFF) < 0x80000000 and seq != newest_seen
