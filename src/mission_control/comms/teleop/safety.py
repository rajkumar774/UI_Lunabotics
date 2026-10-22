"""Rover-side arm state machine and dead-man (plane ①).

Runs on the Jetson. See ``docs/comms_architecture.md`` §5.

**Standard library only — no Qt, no ROS, no PySide6.** This logic decides
whether the rover is allowed to move, so it must be able to run when every
other subsystem on the rover is broken or not yet started.

The socket layer lives in ``tools/rover_safety_daemon.py``; everything here is
pure and time-injected, so the dead-man's staging can be tested exactly rather
than by sleeping and hoping.

Two rules are load-bearing and come straight from the rulebook:

* **Arming requires a rising edge** (guidebook §4.3.3 — "resetting an E-stop
  alone shall not resume machine operation. A second deliberate action … is
  required"). A controller that boots already requesting TELEOP cannot arm the
  rover; the daemon must first observe a disarmed request.
* **Any operator input during autonomy drops straight back to teleop** and is
  logged with a timestamp (§6.2). The log is the team's own evidence of when
  hands went back on the controls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from mission_control.comms.teleop.protocol import (
    ArmState,
    CommandFlag,
    CommandFrame,
    is_newer,
)

logger = logging.getLogger("rover.safety")

#: Dead-man staging, milliseconds. See architecture doc §4.1.
#: Three stages rather than one because a single threshold forces a choice
#: between nuisance stops on a jittery link and unsafe coasting on a dead one.
ZERO_VELOCITY_MS = 300     # ~6 missed frames: ride out normal Wi-Fi jitter
DISARM_MS = 2_000          # beyond a transient: require a deliberate re-arm
LATCH_STOPPED_MS = 10_000  # guidebook §6.1.4 Loss of Comm


class ControlSource(str, Enum):
    """Who, if anyone, is allowed to drive right now."""
    NONE = "NONE"            # motors inhibited
    TELEOP = "TELEOP"        # obeying plane ① setpoints
    AUTONOMY = "AUTONOMY"    # onboard stack drives; daemon only gates


@dataclass
class SafetyOutput:
    """What the motor layer should do this tick."""
    arm_state: ArmState = ArmState.SAFE
    source: ControlSource = ControlSource.NONE
    linear_x: float = 0.0
    angular_z: float = 0.0
    excavation: int = 0
    #: True when the link watchdog has fired at its first stage.
    link_stale: bool = False

    @property
    def motors_enabled(self) -> bool:
        return self.source is not ControlSource.NONE


@dataclass
class SafetyConfig:
    zero_velocity_ms: int = ZERO_VELOCITY_MS
    disarm_ms: int = DISARM_MS
    latch_stopped_ms: int = LATCH_STOPPED_MS
    #: Require CommandFlag.AUTONOMY_ACK before honouring an AUTONOMY request,
    #: so a stray frame cannot put the rover into hands-free operation.
    require_autonomy_ack: bool = True


@dataclass
class SafetyController:
    """The rover's authority on whether it may move.

    All timing is passed in as monotonic milliseconds, never read from a clock
    inside this class, so tests drive it deterministically and the daemon can
    use whichever clock it trusts.
    """

    config: SafetyConfig = field(default_factory=SafetyConfig)

    _arm_state: ArmState = ArmState.SAFE
    _last_command: Optional[CommandFrame] = None
    _last_command_ms: Optional[int] = None
    _newest_seq: Optional[int] = None
    _session: Optional[int] = None
    _estop_engaged: bool = False
    #: Guards the §4.3.3 rising edge: no arming until a disarmed request is seen.
    _seen_disarmed_request: bool = False
    _transitions: List[Tuple[int, ArmState, ArmState, str]] = field(default_factory=list)
    _accepted: int = 0
    _rejected_stale: int = 0

    # -- Introspection -------------------------------------------------------

    @property
    def arm_state(self) -> ArmState:
        return self._arm_state

    @property
    def estop_engaged(self) -> bool:
        return self._estop_engaged

    @property
    def transitions(self) -> List[Tuple[int, ArmState, ArmState, str]]:
        """(timestamp_ms, from, to, reason) — the run's safety audit trail."""
        return list(self._transitions)

    @property
    def accepted_commands(self) -> int:
        return self._accepted

    @property
    def rejected_stale_commands(self) -> int:
        return self._rejected_stale

    def silence_ms(self, now_ms: int) -> Optional[int]:
        """Milliseconds since the last accepted command, or None if never."""
        if self._last_command_ms is None:
            return None
        return max(0, now_ms - self._last_command_ms)

    # -- Inputs --------------------------------------------------------------

    def set_estop(self, engaged: bool, now_ms: int) -> None:
        """Reports the hardware E-Stop line.

        In practice the physical stop cuts battery power and the Jetson dies
        with it (§4.4.3 puts the logger before the kill switch), so this mostly
        matters for rigs where the compute is separately powered — and for
        making the behaviour explicit either way.
        """
        was = self._estop_engaged
        self._estop_engaged = bool(engaged)
        if engaged and not was:
            self._transition(ArmState.STOPPED, now_ms, "hardware E-Stop engaged")
        elif was and not engaged:
            # Power returning must not return motion. Back to SAFE, disarmed,
            # and a fresh rising edge is required before anything moves.
            self._seen_disarmed_request = False
            self._transition(ArmState.SAFE, now_ms, "E-Stop released — rearm required")

    def on_command(self, frame: CommandFrame, now_ms: int) -> bool:
        """Applies one received command frame. Returns False if discarded.

        Reordered and duplicate frames are dropped here, so an old velocity can
        never land on top of a newer one.
        """
        if frame.session != self._session:
            # The operator station restarted: its sequence counter went back to
            # zero. Without resetting the high-water mark here, every frame from
            # the new process would look stale forever and the rover would latch
            # STOPPED with no way back. Arming is NOT inherited across the
            # restart -- a fresh rising edge is still required.
            if self._session is not None:
                logger.warning(
                    f"Operator station session changed "
                    f"{self._session:#06x} -> {frame.session:#06x}; resyncing sequence."
                )
                self._seen_disarmed_request = False
                self._transition(ArmState.SAFE, now_ms, "operator station restarted")
            self._session = frame.session
            self._newest_seq = None

        if not is_newer(frame.seq, self._newest_seq):
            self._rejected_stale += 1
            return False

        self._newest_seq = frame.seq
        self._last_command = frame
        self._last_command_ms = now_ms
        self._accepted += 1

        # A disarmed request at any point re-arms the §4.3.3 edge detector.
        if frame.arm_request in (ArmState.SAFE, ArmState.STOPPED):
            self._seen_disarmed_request = True

        if frame.has_flag(CommandFlag.SOFTWARE_STOP):
            self._transition(ArmState.STOPPED, now_ms, "operator software stop")
            return True

        if self._estop_engaged:
            return True  # nothing moves while the hardware stop is in

        self._apply_request(frame, now_ms)
        return True

    def _apply_request(self, frame: CommandFrame, now_ms: int) -> None:
        requested = frame.arm_request
        current = self._arm_state

        if current is ArmState.STOPPED:
            # Latched. Only an explicit disarmed request clears it, and that
            # only reaches SAFE -- never straight back to driving.
            if requested in (ArmState.SAFE, ArmState.STOPPED):
                self._transition(ArmState.SAFE, now_ms, "operator cleared stop")
            return

        if requested is ArmState.SAFE:
            if current is not ArmState.SAFE:
                self._transition(ArmState.SAFE, now_ms, "operator disarmed")
            return

        if requested is ArmState.TELEOP_ARMED:
            if current is ArmState.AUTONOMY:
                self._transition(ArmState.TELEOP_ARMED, now_ms, "operator resumed manual control")
            elif current is ArmState.SAFE and self._seen_disarmed_request:
                self._transition(ArmState.TELEOP_ARMED, now_ms, "operator armed")
            return

        if requested is ArmState.AUTONOMY:
            if self.config.require_autonomy_ack and not frame.has_flag(CommandFlag.AUTONOMY_ACK):
                return
            if current is ArmState.TELEOP_ARMED:
                self._transition(ArmState.AUTONOMY, now_ms, "autonomy engaged (hands-free declared)")
            return

    # -- Per-tick evaluation -------------------------------------------------

    def tick(self, now_ms: int) -> SafetyOutput:
        """Evaluates the dead-man and returns what the motors may do.

        Must be called at the command rate even when no frame arrived — that is
        the entire mechanism by which silence becomes a stop.
        """
        silence = self.silence_ms(now_ms)

        # Loss of Comm is evaluated whatever the arm state. A link that has
        # been gone this long is a mission anomaly the operator must see and
        # report (guidebook 6.1.4.2), which is a different thing from a quiet
        # disarm -- so the ladder continues past SAFE into a latched STOPPED
        # rather than stopping there.
        if (
            silence is not None
            and silence >= self.config.latch_stopped_ms
            and self._arm_state is not ArmState.STOPPED
        ):
            self._transition(
                ArmState.STOPPED, now_ms,
                f"link lost {silence} ms — guidebook 6.1.4 Loss of Comm",
            )

        if self._arm_state in (ArmState.SAFE, ArmState.STOPPED) or self._estop_engaged:
            return SafetyOutput(arm_state=self._arm_state, source=ControlSource.NONE)

        if silence is None:
            # Armed but nothing ever received: should not happen, since arming
            # requires a frame. Fail closed regardless.
            return SafetyOutput(arm_state=self._arm_state, source=ControlSource.NONE)

        if self._arm_state is ArmState.AUTONOMY:
            # The rover is driving itself; operator silence is expected and
            # must not stop it. Only the Loss-of-Comm stage above applies.
            return SafetyOutput(
                arm_state=self._arm_state,
                source=ControlSource.AUTONOMY,
                link_stale=silence >= self.config.zero_velocity_ms,
            )

        if silence >= self.config.disarm_ms:
            self._transition(ArmState.SAFE, now_ms, f"no command for {silence} ms — disarmed")
            return SafetyOutput(arm_state=self._arm_state, source=ControlSource.NONE)

        if silence >= self.config.zero_velocity_ms:
            # Still armed, but we will not keep applying a stale velocity.
            return SafetyOutput(
                arm_state=self._arm_state,
                source=ControlSource.TELEOP,
                link_stale=True,
            )

        command = self._last_command
        return SafetyOutput(
            arm_state=self._arm_state,
            source=ControlSource.TELEOP,
            linear_x=command.linear_x,
            angular_z=command.angular_z,
            excavation=command.excavation,
        )

    # -- Internals -----------------------------------------------------------

    def _transition(self, new_state: ArmState, now_ms: int, reason: str) -> None:
        if new_state is self._arm_state:
            return
        old = self._arm_state
        self._arm_state = new_state
        self._transitions.append((now_ms, old, new_state, reason))
        log = logger.warning if new_state in (ArmState.STOPPED, ArmState.SAFE) else logger.info
        log(f"ARM {old.name} -> {new_state.name}: {reason}")
