"""Tests for the operator control column: mode/action buttons and status."""

import pytest
from PySide6.QtWidgets import QApplication

from mission_control.telemetry.state import (
    ConnectionState,
    MissionMode,
    RoverState,
    Severity,
)
from mission_control.ui.components.autonomy_control_widget import (
    AutonomyControlWidget,
    MissionAction,
)
from mission_control.ui.components.command_status_widget import CommandStatusWidget


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def live_state(**overrides) -> RoverState:
    """A snapshot that reads as connected with valid telemetry."""
    state = RoverState(
        connection_state=ConnectionState.CONNECTED,
        telemetry_valid=True,
        is_stale=False,
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


# --- Actions --------------------------------------------------------------

def test_action_buttons_emit_requests(qapp):
    widget = AutonomyControlWidget()
    requested = []
    widget.action_requested.connect(requested.append)

    for action in MissionAction:
        widget.action_buttons[action].click()

    assert requested == [MissionAction.EXCAVATE, MissionAction.DUMP, MissionAction.ONE_CYCLE_AUTO]


# --- Emergency stop -------------------------------------------------------
#
# The software E-Stop button was removed. The stop that counts is the physical
# one on the rover: a button here needed a live link to do anything, which is
# exactly the condition under which you most need to stop. The LATCH is still
# tracked, because the rover reports it on /safety/estop and it must still
# lock out the action buttons.


def test_estop_button_is_offered_but_marked_unwired(qapp):
    """The button is back; nothing is wired behind it yet, and it says so.

    Mission Control publishes no topics at all, so a press cannot reach the
    rover. The caption must carry that, because a safety control that looks
    live but does nothing is worse than no button.
    """
    widget = AutonomyControlWidget()
    assert widget.estop_btn.text() == "EMERGENCY STOP"
    assert "NOT WIRED" in widget.estop_caption.text()


def test_estop_press_requests_engage_then_release(qapp):
    widget = AutonomyControlWidget()
    requested = []
    widget.estop_requested.connect(requested.append)

    widget.estop_btn.click()
    assert requested == [True]

    widget.set_estop_engaged(True)
    assert widget.estop_btn.text() == "RELEASE E-STOP"
    widget.estop_btn.click()
    assert requested == [True, False]


def test_reported_estop_still_locks_out_commands(qapp):
    """A rover-reported E-Stop must still disable every action button."""
    widget = AutonomyControlWidget()
    widget.set_controls_enabled(True)
    assert any(b.isEnabled() for b in widget.action_buttons.values())

    widget.set_estop_engaged(True)
    assert widget.estop_engaged is True
    assert all(not b.isEnabled() for b in widget.action_buttons.values())


def test_estop_release_restores_commands(qapp):
    widget = AutonomyControlWidget()
    widget.set_controls_enabled(True)
    widget.set_estop_engaged(True)
    widget.set_estop_engaged(False)

    assert widget.estop_engaged is False
    assert all(b.isEnabled() for b in widget.action_buttons.values())


def test_link_loss_disables_commands(qapp):
    widget = AutonomyControlWidget()
    widget.set_controls_enabled(False)
    assert all(not b.isEnabled() for b in widget.action_buttons.values())


def test_enabling_controls_does_not_override_latched_estop(qapp):
    """A link coming back must not quietly re-arm commands under an E-Stop."""
    widget = AutonomyControlWidget()
    widget.set_estop_engaged(True)
    widget.set_controls_enabled(True)

    assert all(not b.isEnabled() for b in widget.action_buttons.values())


def test_widget_offers_no_mode_controls(qapp):
    """Modes were removed from the operator surface; only actions remain."""
    widget = AutonomyControlWidget()
    assert not hasattr(widget, "mode_buttons")
    assert not hasattr(widget, "mode_requested")


# --- Command status -------------------------------------------------------

def test_status_starts_in_no_data_state(qapp):
    widget = CommandStatusWidget()
    assert widget.mode_row.value() == "--"
    assert widget.estop_row.value() == "--"
    assert widget.command_row.value() == "NONE"
    assert widget.link_row.value() == ConnectionState.DISCONNECTED.value


def test_status_renders_live_snapshot(qapp):
    widget = CommandStatusWidget()
    widget.update_from_state(live_state(mission_mode=MissionMode.EXCAVATION))

    assert widget.mode_row.value() == MissionMode.EXCAVATION.value
    assert widget.estop_row.value() == "CLEAR"
    assert widget.link_row.value() == ConnectionState.CONNECTED.value


def test_status_shows_engaged_estop(qapp):
    widget = CommandStatusWidget()
    widget.update_from_state(live_state(is_estopped=True))
    assert widget.estop_row.value() == "ENGAGED"


def test_status_withholds_mode_when_link_is_down(qapp):
    """No link means no claim about the rover's control mode."""
    widget = CommandStatusWidget()
    widget.update_from_state(RoverState(connection_state=ConnectionState.LOST_LINK))
    assert widget.mode_row.value() == "--"
    assert widget.estop_row.value() == "--"


def test_status_marks_estop_unconfirmed_when_telemetry_is_stale(qapp):
    """Linked but stale must not assert CLEAR — an operator would act on it."""
    widget = CommandStatusWidget()
    widget.update_from_state(
        RoverState(connection_state=ConnectionState.CONNECTED, telemetry_valid=True, is_stale=True)
    )
    assert widget.estop_row.value() == "UNCONFIRMED"


def test_status_shows_engaged_estop_even_when_stale(qapp):
    """Hiding an engaged stop is unsafe; showing a released one is merely wrong."""
    widget = CommandStatusWidget()
    widget.update_from_state(
        RoverState(
            connection_state=ConnectionState.CONNECTED,
            is_estopped=True,
            telemetry_valid=True,
            is_stale=True,
        )
    )
    assert widget.estop_row.value() == "ENGAGED"


def test_status_records_command_acknowledgements(qapp):
    widget = CommandStatusWidget()
    widget.record_command("EXCAVATE", True)
    assert "EXCAVATE" in widget.command_row.value()
    assert "SENT" in widget.command_row.value()

    widget.record_command("DUMP", False)
    assert "FAILED" in widget.command_row.value()
    assert widget.last_command == "DUMP"


def test_status_malformed_snapshot_degrades_safely(qapp):
    widget = CommandStatusWidget()
    widget.update_from_state(live_state())
    widget.update_from_state("not a rover state")
    assert widget.mode_row.value() == "--"
