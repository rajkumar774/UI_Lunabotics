"""Tests for the rover-side arm state machine and dead-man (plane ①).

Time is injected, so the dead-man's staging is verified exactly rather than by
sleeping. No Qt and no sockets here — this is the pure decision logic that
decides whether a rover is allowed to move.
"""

import pytest

from mission_control.comms.teleop.protocol import (
    ArmState,
    CommandFlag,
    CommandFrame,
)
from mission_control.comms.teleop.safety import (
    ControlSource,
    SafetyConfig,
    SafetyController,
)


def cmd(seq, arm=ArmState.TELEOP_ARMED, lin=0.0, ang=0.0, flags=0, excavation=0):
    return CommandFrame(
        seq=seq, t_sent_ms=seq * 50, arm_request=arm,
        linear_x=lin, angular_z=ang, excavation=excavation, flags=flags,
    )


@pytest.fixture
def armed():
    """A controller already armed via a legitimate rising edge at t=0."""
    c = SafetyController()
    c.on_command(cmd(1, arm=ArmState.SAFE), 0)
    c.on_command(cmd(2, arm=ArmState.TELEOP_ARMED), 0)
    assert c.arm_state is ArmState.TELEOP_ARMED
    return c


# --- Boot and arming (guidebook 4.3.3) ------------------------------------


def test_boots_safe_with_motors_inhibited():
    c = SafetyController()
    assert c.arm_state is ArmState.SAFE
    out = c.tick(0)
    assert out.motors_enabled is False
    assert out.source is ControlSource.NONE


def test_cannot_arm_without_first_seeing_a_disarmed_request():
    """A controller that boots already asking for TELEOP must not arm the rover.

    Guidebook 4.3.3: restoring power must not restore motion; a second
    deliberate action is required.
    """
    c = SafetyController()
    for seq in range(1, 20):
        c.on_command(cmd(seq, arm=ArmState.TELEOP_ARMED, lin=0.5), seq * 50)
    assert c.arm_state is ArmState.SAFE
    assert c.tick(1000).motors_enabled is False


def test_arms_on_a_rising_edge():
    c = SafetyController()
    c.on_command(cmd(1, arm=ArmState.SAFE), 0)
    assert c.arm_state is ArmState.SAFE
    c.on_command(cmd(2, arm=ArmState.TELEOP_ARMED), 50)
    assert c.arm_state is ArmState.TELEOP_ARMED


def test_armed_rover_passes_setpoints_through(armed):
    armed.on_command(cmd(3, lin=0.42, ang=-0.15, excavation=2), 50)
    out = armed.tick(60)

    assert out.source is ControlSource.TELEOP
    assert out.linear_x == pytest.approx(0.42)
    assert out.angular_z == pytest.approx(-0.15)
    assert out.excavation == 2
    assert out.link_stale is False


def test_operator_can_disarm(armed):
    armed.on_command(cmd(3, arm=ArmState.SAFE), 50)
    assert armed.arm_state is ArmState.SAFE
    assert armed.tick(60).motors_enabled is False


# --- Dead-man staging -----------------------------------------------------


def test_brief_jitter_does_not_stop_the_rover(armed):
    """Under 300 ms of silence the rover keeps driving: Wi-Fi jitter is normal."""
    armed.on_command(cmd(3, lin=0.4), 100)
    out = armed.tick(100 + 250)
    assert out.linear_x == pytest.approx(0.4)
    assert out.link_stale is False


def test_300ms_silence_zeroes_velocity_but_stays_armed(armed):
    armed.on_command(cmd(3, lin=0.4), 100)
    out = armed.tick(100 + 350)

    assert armed.arm_state is ArmState.TELEOP_ARMED
    assert out.link_stale is True
    assert out.linear_x == 0.0
    assert out.angular_z == 0.0


def test_recovery_from_stale_resumes_driving(armed):
    armed.on_command(cmd(3, lin=0.4), 100)
    assert armed.tick(500).link_stale is True

    armed.on_command(cmd(4, lin=0.3), 600)
    out = armed.tick(610)
    assert out.link_stale is False
    assert out.linear_x == pytest.approx(0.3)


def test_two_seconds_silence_disarms(armed):
    armed.on_command(cmd(3, lin=0.4), 100)
    out = armed.tick(100 + 2_100)

    assert armed.arm_state is ArmState.SAFE
    assert out.motors_enabled is False


def test_ten_seconds_silence_latches_stopped(armed):
    """Guidebook 6.1.4 Loss of Comm."""
    armed.on_command(cmd(3, lin=0.4), 100)
    armed.tick(100 + 10_100)

    assert armed.arm_state is ArmState.STOPPED
    reasons = [t[3] for t in armed.transitions]
    assert any("Loss of Comm" in r for r in reasons)


def test_the_full_ladder_fires_in_order(armed):
    """Stages must fire in sequence, not jump straight to the last one.

    The ladder continues past SAFE into STOPPED, because a 10 s outage is a
    reportable Loss of Comm (6.1.4.2) and not merely a quiet disarm -- the
    operator needs those two annunciated differently.
    """
    armed.on_command(cmd(3, lin=0.4), 100)

    armed.tick(100 + 350)      # stage 1: zero velocity, still armed
    assert armed.arm_state is ArmState.TELEOP_ARMED

    armed.tick(100 + 2_100)    # stage 2: disarm
    assert armed.arm_state is ArmState.SAFE

    armed.tick(100 + 10_100)   # stage 3: latched loss of comm
    assert armed.arm_state is ArmState.STOPPED

    states = [t[2] for t in armed.transitions]
    assert states == [ArmState.TELEOP_ARMED, ArmState.SAFE, ArmState.STOPPED]
    assert armed.tick(100 + 20_000).motors_enabled is False


def test_loss_of_comm_latches_only_once(armed):
    armed.on_command(cmd(3, lin=0.4), 100)
    for t in range(10_100, 30_000, 1_000):
        armed.tick(100 + t)
    stops = [t for t in armed.transitions if t[2] is ArmState.STOPPED]
    assert len(stops) == 1


# --- Software and hardware stop -------------------------------------------


def test_software_stop_latches(armed):
    armed.on_command(cmd(3, lin=0.4, flags=CommandFlag.SOFTWARE_STOP), 100)
    assert armed.arm_state is ArmState.STOPPED
    assert armed.tick(110).motors_enabled is False


def test_stopped_ignores_a_bare_arm_request(armed):
    """Latched means latched: an ARM alone must not resume motion."""
    armed.on_command(cmd(3, flags=CommandFlag.SOFTWARE_STOP), 100)
    for seq in range(4, 15):
        armed.on_command(cmd(seq, arm=ArmState.TELEOP_ARMED, lin=0.5), 100 + seq * 50)

    assert armed.arm_state is ArmState.STOPPED
    assert armed.tick(2_000).motors_enabled is False


def test_clearing_a_stop_returns_to_safe_not_to_driving(armed):
    armed.on_command(cmd(3, flags=CommandFlag.SOFTWARE_STOP), 100)
    armed.on_command(cmd(4, arm=ArmState.SAFE), 200)
    assert armed.arm_state is ArmState.SAFE

    armed.on_command(cmd(5, arm=ArmState.TELEOP_ARMED), 250)
    assert armed.arm_state is ArmState.TELEOP_ARMED


def test_hardware_estop_stops_and_blocks_commands(armed):
    armed.set_estop(True, 100)
    assert armed.arm_state is ArmState.STOPPED

    armed.on_command(cmd(3, arm=ArmState.TELEOP_ARMED, lin=0.9), 150)
    assert armed.tick(160).motors_enabled is False


def test_estop_release_requires_a_fresh_rising_edge():
    """Guidebook 4.3.3 again, on the hardware path."""
    c = SafetyController()
    c.on_command(cmd(1, arm=ArmState.SAFE), 0)
    c.on_command(cmd(2, arm=ArmState.TELEOP_ARMED), 50)
    assert c.arm_state is ArmState.TELEOP_ARMED

    c.set_estop(True, 100)
    c.set_estop(False, 200)
    assert c.arm_state is ArmState.SAFE

    # A controller still holding TELEOP must not re-arm by itself.
    for seq in range(3, 12):
        c.on_command(cmd(seq, arm=ArmState.TELEOP_ARMED, lin=0.5), 200 + seq * 50)
    assert c.arm_state is ArmState.SAFE

    c.on_command(cmd(20, arm=ArmState.SAFE), 900)
    c.on_command(cmd(21, arm=ArmState.TELEOP_ARMED), 950)
    assert c.arm_state is ArmState.TELEOP_ARMED


# --- Autonomy -------------------------------------------------------------


def test_autonomy_requires_the_acknowledgement_flag(armed):
    armed.on_command(cmd(3, arm=ArmState.AUTONOMY), 100)
    assert armed.arm_state is ArmState.TELEOP_ARMED

    armed.on_command(cmd(4, arm=ArmState.AUTONOMY, flags=CommandFlag.AUTONOMY_ACK), 150)
    assert armed.arm_state is ArmState.AUTONOMY


def test_autonomy_cannot_be_entered_from_safe():
    """Hands-free must be a step up from a live armed state, not from cold."""
    c = SafetyController()
    c.on_command(cmd(1, arm=ArmState.SAFE), 0)
    c.on_command(cmd(2, arm=ArmState.AUTONOMY, flags=CommandFlag.AUTONOMY_ACK), 50)
    assert c.arm_state is ArmState.SAFE


def test_autonomy_ignores_the_teleop_deadman(armed):
    """Operator silence is expected during hands-free and must not stop it."""
    armed.on_command(cmd(3, arm=ArmState.AUTONOMY, flags=CommandFlag.AUTONOMY_ACK), 100)

    out = armed.tick(100 + 5_000)
    assert armed.arm_state is ArmState.AUTONOMY
    assert out.source is ControlSource.AUTONOMY
    assert out.motors_enabled is True


def test_autonomy_still_honours_loss_of_comm(armed):
    armed.on_command(cmd(3, arm=ArmState.AUTONOMY, flags=CommandFlag.AUTONOMY_ACK), 100)
    armed.tick(100 + 10_500)

    assert armed.arm_state is ArmState.STOPPED


def test_operator_input_during_autonomy_reverts_to_teleop(armed):
    """Guidebook 6.2: touching the controls ends the autonomy attempt."""
    armed.on_command(cmd(3, arm=ArmState.AUTONOMY, flags=CommandFlag.AUTONOMY_ACK), 100)
    assert armed.arm_state is ArmState.AUTONOMY

    armed.on_command(cmd(4, arm=ArmState.TELEOP_ARMED, lin=0.3), 200)
    assert armed.arm_state is ArmState.TELEOP_ARMED

    reasons = [t[3] for t in armed.transitions]
    assert any("resumed manual control" in r for r in reasons)


def test_transitions_are_timestamped_for_the_audit_trail(armed):
    armed.on_command(cmd(3, arm=ArmState.AUTONOMY, flags=CommandFlag.AUTONOMY_ACK), 1_234)
    stamps = [t[0] for t in armed.transitions]
    assert 1_234 in stamps
    assert all(isinstance(s, int) for s in stamps)


# --- Frame ordering -------------------------------------------------------


def test_reordered_frames_are_discarded(armed):
    armed.on_command(cmd(10, lin=0.5), 100)
    assert armed.on_command(cmd(7, lin=-0.9), 110) is False

    out = armed.tick(120)
    assert out.linear_x == pytest.approx(0.5)
    assert armed.rejected_stale_commands == 1


def test_duplicate_frames_are_discarded(armed):
    armed.on_command(cmd(10, lin=0.5), 100)
    assert armed.on_command(cmd(10, lin=0.5), 110) is False


def test_sequence_wrap_does_not_wedge_the_link():
    """A counter rolling past 2^32 during a long run must keep flowing."""
    c = SafetyController()
    c.on_command(CommandFrame(seq=0xFFFFFFFD, arm_request=ArmState.SAFE), 0)
    c.on_command(CommandFrame(seq=0xFFFFFFFE, arm_request=ArmState.TELEOP_ARMED), 50)
    assert c.arm_state is ArmState.TELEOP_ARMED

    # ...FFFE -> 1 wraps forward and must be accepted.
    assert c.on_command(
        CommandFrame(seq=1, arm_request=ArmState.TELEOP_ARMED, linear_x=0.4), 100
    ) is True
    assert c.tick(110).linear_x == pytest.approx(0.4)


def test_huge_forward_sequence_jump_is_rejected_as_garbage():
    """More than half the sequence space ahead is treated as old, not new."""
    c = SafetyController()
    c.on_command(cmd(1, arm=ArmState.SAFE), 0)
    c.on_command(cmd(2, arm=ArmState.TELEOP_ARMED), 50)
    assert c.on_command(
        CommandFrame(seq=0xF0000000, arm_request=ArmState.TELEOP_ARMED, linear_x=0.9), 100
    ) is False


# --- Operator station restart ---------------------------------------------


def test_restarted_operator_station_resyncs():
    """A UI restart resets seq to 0. Without session tracking every subsequent
    frame would look stale forever and the rover could never be driven again."""
    c = SafetyController()
    c.on_command(CommandFrame(session=0xAAAA, seq=1, arm_request=ArmState.SAFE), 0)
    c.on_command(CommandFrame(session=0xAAAA, seq=12_000, arm_request=ArmState.TELEOP_ARMED), 50)
    assert c.arm_state is ArmState.TELEOP_ARMED

    # New process, sequence back to 0.
    assert c.on_command(
        CommandFrame(session=0xBBBB, seq=0, arm_request=ArmState.SAFE), 1_000
    ) is True
    assert c.on_command(
        CommandFrame(session=0xBBBB, seq=1, arm_request=ArmState.TELEOP_ARMED, linear_x=0.4), 1_050
    ) is True
    assert c.arm_state is ArmState.TELEOP_ARMED
    assert c.tick(1_060).linear_x == pytest.approx(0.4)


def test_restart_does_not_inherit_the_armed_state():
    """A restarting station must not find the rover already armed."""
    c = SafetyController()
    c.on_command(CommandFrame(session=0xAAAA, seq=1, arm_request=ArmState.SAFE), 0)
    c.on_command(CommandFrame(session=0xAAAA, seq=2, arm_request=ArmState.TELEOP_ARMED), 50)
    assert c.arm_state is ArmState.TELEOP_ARMED

    # New session arrives already asking to drive: must be refused.
    for seq in range(5):
        c.on_command(
            CommandFrame(session=0xBBBB, seq=seq, arm_request=ArmState.TELEOP_ARMED, linear_x=0.8),
            1_000 + seq * 50,
        )
    assert c.arm_state is ArmState.SAFE
    assert c.tick(1_300).motors_enabled is False


def test_silence_ms_reports_none_before_any_command():
    assert SafetyController().silence_ms(5_000) is None


def test_configurable_timings_are_honoured():
    c = SafetyController(config=SafetyConfig(zero_velocity_ms=100, disarm_ms=500,
                                             latch_stopped_ms=1_000))
    c.on_command(cmd(1, arm=ArmState.SAFE), 0)
    c.on_command(cmd(2, arm=ArmState.TELEOP_ARMED, lin=0.4), 0)

    assert c.tick(150).link_stale is True
    c.tick(600)
    assert c.arm_state is ArmState.SAFE
