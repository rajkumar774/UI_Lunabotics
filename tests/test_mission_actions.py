"""Tests for operator action dispatch and its config bindings.

The important property here is a safety one: an action must never *appear* to
have commanded the rover when it did not. Mocks are visible, unknown names
fall back to mock, and unimplemented modes report failure rather than success.
"""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from mission_control.comms.mission_actions import MissionActionDispatcher
from mission_control.comms.mock_client import MockCommsClient
from mission_control.core.config import (
    ActionMode,
    MissionActionBinding,
    load_rover_config,
)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def client(qapp):
    c = MockCommsClient()
    c.start()
    c.connect_rover()
    yield c
    c.stop()


# --- Config bindings ------------------------------------------------------


def test_shipped_config_binds_every_button():
    bindings = load_rover_config(CONFIG_DIR).mission_actions
    assert {"EXCAVATE", "DUMP", "ONE CYCLE AUTO"} <= set(bindings)


def test_shipped_mission_actions_run_their_launch_files():
    """Each mission task maps to a launch file that exists in lunar_ws.

    These were mocks until the autonomy launch files were written. SSH is the
    bridge, not the destination: it reports that the command was typed, not
    that the nodes came up. Switch to mode: service when LaunchSystem.srv
    exists -- the service/action endpoints are already in the YAML.
    """
    bindings = load_rover_config(CONFIG_DIR).mission_actions
    tasks = {n: b for n, b in bindings.items() if not b.is_system}
    assert tasks, "expected EXCAVATE/DUMP/ONE CYCLE AUTO"
    assert not any(b.is_mock for b in tasks.values())
    assert tasks["EXCAVATE"].command == "ros2 launch excavate_dump excavate_phase.launch.py"
    assert tasks["DUMP"].command == "ros2 launch excavate_dump dump_phase.launch.py"
    assert tasks["ONE CYCLE AUTO"].command == (
        "ros2 launch rover_navigation full_autonomy.launch.py")


def test_localization_is_not_a_separate_button():
    """It is started by full_autonomy, so a button would double-start it.

    full_autonomy.launch.py -> traverse.launch.py -> rover_localization, which
    owns the IMU, wheel odometry and the OV9281 driver. A second LOCALIZATION
    press would collide with that on /dev/video0.
    """
    bindings = load_rover_config(CONFIG_DIR).mission_actions
    assert "LOCALIZATION" not in bindings
    assert not any(b.is_system for b in bindings.values())


def test_bindings_carry_the_future_endpoints():
    """The real topic/service/action names are present and ready to switch on."""
    binding = load_rover_config(CONFIG_DIR).mission_actions["EXCAVATE"]
    assert binding.payload == "EXCAVATE"
    assert binding.action.startswith("/")
    assert binding.service.startswith("/")


def test_unknown_mode_in_yaml_falls_back_to_mock(tmp_path):
    """A typo must never become a live rover command."""
    (tmp_path / "rover_config.yaml").write_text(
        "mission_actions:\n  EXCAVATE:\n    mode: lauch\n    payload: EXCAVATE\n"
    )
    assert load_rover_config(tmp_path).mission_actions["EXCAVATE"].is_mock


def test_malformed_binding_falls_back_to_mock(tmp_path):
    (tmp_path / "rover_config.yaml").write_text(
        "mission_actions:\n  DUMP: 'just a string'\n"
    )
    assert load_rover_config(tmp_path).mission_actions["DUMP"].is_mock


# --- Dispatch -------------------------------------------------------------


def test_mock_is_accepted_but_commands_nothing(client):
    d = MissionActionDispatcher(client, {"EXCAVATE": MissionActionBinding(name="EXCAVATE")})
    events = []
    d.dispatched.connect(lambda n, ok, detail: events.append((n, ok, detail)))

    assert d.dispatch("EXCAVATE") is True
    name, ok, detail = events[0]
    assert (name, ok) == ("EXCAVATE", True)
    assert "NOTHING was commanded" in detail
    assert client.last_mission_command is None, "a mock must not reach the client"


def test_mock_is_labelled_in_the_status_feedback(client):
    """The operator has to be able to tell a rehearsal from a real dispatch."""
    d = MissionActionDispatcher(client, {"DUMP": MissionActionBinding(name="DUMP")})
    feedback = []
    client.command_feedback_received.connect(lambda c, ok: feedback.append(c))

    d.dispatch("DUMP")
    assert any("MOCK" in c for c in feedback)


def test_topic_mode_actually_sends(client):
    d = MissionActionDispatcher(client, {
        "EXCAVATE": MissionActionBinding(
            name="EXCAVATE", mode=ActionMode.TOPIC, payload="EXCAVATE"
        )
    })
    assert d.dispatch("EXCAVATE") is True
    assert client.last_mission_command == "EXCAVATE"


def test_unimplemented_modes_report_failure(client):
    """Better a visible 'not implemented' than a silent success."""
    for mode, field in ((ActionMode.SERVICE, "service"), (ActionMode.ACTION, "action")):
        d = MissionActionDispatcher(client, {
            "X": MissionActionBinding(name="X", mode=mode, **{field: "/somewhere"})
        })
        details = []
        d.dispatched.connect(lambda n, ok, detail: details.append((ok, detail)))
        assert d.dispatch("X") is False
        assert "not implemented" in details[0][1]


def test_unknown_action_defaults_to_mock(client):
    d = MissionActionDispatcher(client, {})
    assert d.is_mock("SOMETHING NEW") is True
    assert d.dispatch("SOMETHING NEW") is True
    assert client.last_mission_command is None


def test_any_real_bindings_flag(client):
    assert MissionActionDispatcher(client, {}).any_real_bindings is False
    assert MissionActionDispatcher(client, {
        "A": MissionActionBinding(name="A"),
    }).any_real_bindings is False
    assert MissionActionDispatcher(client, {
        "A": MissionActionBinding(name="A"),
        "B": MissionActionBinding(name="B", mode=ActionMode.TOPIC),
    }).any_real_bindings is True


# --- SSH mode -------------------------------------------------------------


def test_ssh_mode_runs_the_command(client):
    ran = []
    d = MissionActionDispatcher(
        client,
        {"LOCALIZATION": MissionActionBinding(
            name="LOCALIZATION", mode=ActionMode.SSH,
            command="ros2 launch rover_localization localization.launch.py",
        )},
        ssh_runner=lambda cmd: ran.append(cmd) or True,
    )
    assert d.dispatch("LOCALIZATION") is True
    assert ran == ["ros2 launch rover_localization localization.launch.py"]


def test_ssh_mode_reports_a_disconnected_terminal(client):
    """A refused command must not look dispatched."""
    d = MissionActionDispatcher(
        client,
        {"LOCALIZATION": MissionActionBinding(
            name="LOCALIZATION", mode=ActionMode.SSH, command="ros2 launch x y.py")},
        ssh_runner=lambda cmd: False,
    )
    details = []
    d.dispatched.connect(lambda n, ok, detail: details.append((ok, detail)))
    assert d.dispatch("LOCALIZATION") is False
    assert "not connected" in details[0][1]


def test_ssh_mode_without_a_command_is_refused(client):
    d = MissionActionDispatcher(
        client,
        {"X": MissionActionBinding(name="X", mode=ActionMode.SSH)},
        ssh_runner=lambda cmd: True,
    )
    assert d.dispatch("X") is False


def test_ssh_mode_without_a_terminal_is_refused(client):
    d = MissionActionDispatcher(
        client,
        {"X": MissionActionBinding(name="X", mode=ActionMode.SSH, command="ls")},
        ssh_runner=None,
    )
    assert d.dispatch("X") is False


def test_every_binding_names_a_command_it_can_actually_run():
    """A non-mock binding with no command would fail silently at press time."""
    bindings = load_rover_config(CONFIG_DIR).mission_actions
    for name, binding in bindings.items():
        if binding.mode is ActionMode.SSH:
            assert binding.command, f"{name} is mode:ssh but carries no command"
            assert binding.command.startswith("ros2 launch"), name


# --- Placeholder systems --------------------------------------------------


def test_cameras_are_not_operator_buttons():
    """Cameras autostart on the Jetson (systemd), so they are not buttons.

    Video is worth 240 points across two runs and must return unaided after
    the E-Stop reboots the Jetson -- a button cannot provide either.
    """
    bindings = load_rover_config(CONFIG_DIR).mission_actions
    assert not any("CAMERA" in name.upper() for name in bindings)


def test_placeholder_system_dispatch_commands_nothing(client):
    from mission_control.core.config import MissionActionBinding as B

    ran = []
    d = MissionActionDispatcher(
        client,
        {"CAMERA FRONT": B(name="CAMERA FRONT", is_system=True)},
        ssh_runner=lambda c: ran.append(c) or True,
    )
    assert d.dispatch("CAMERA FRONT") is True   # acknowledged
    assert ran == []                             # but nothing was run
