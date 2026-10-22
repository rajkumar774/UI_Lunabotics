"""Tests for running commands while another command is still running.

A single shell runs one foreground process at a time, so `ros2 launch` would
lock the operator out of the terminal for the rest of the run. The panel
therefore keeps several shells over one SSH connection and routes a command to
a free one. These tests pin that routing down without needing an SSH server.
"""

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from mission_control.comms.jetson_ssh import (
    IDLE_SENTINEL,
    PROMPT_COMMAND_SETUP,
    shell_init_commands,
)
from mission_control.core.config import JetsonSSHConfig, QuickCommand
from mission_control.ui.components.jetson_terminal_widget import (
    MAX_SHELLS,
    JetsonTerminalWidget,
)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class FakeShell(QObject):
    """A shell that records what was sent instead of talking to a Jetson."""

    data_received = Signal(bytes)
    closed = Signal(str)

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self._name = name
        self.sent_lines = []
        self.interrupts = 0
        self._alive = True

    @property
    def name(self):
        return self._name

    @property
    def is_alive(self):
        return self._alive

    def send_line(self, text):
        self.sent_lines.append(text)
        return True

    def send(self, data):
        return True

    def send_interrupt(self):
        self.interrupts += 1
        return True

    def resize(self, cols, rows):
        pass

    def close(self, reason="closed"):
        if self._alive:
            self._alive = False
            self.closed.emit(reason)

    # -- Test helpers -------------------------------------------------------

    def go_idle(self):
        """Emits the sentinel the real shell's PROMPT_COMMAND produces."""
        self.data_received.emit(f"\x1b]0;{IDLE_SENTINEL}\x07".encode())


class FakeConnection(QObject):
    """Stands in for JetsonSSHConnection, handing out FakeShells."""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.is_connected = True
        self.shells = []
        self._counter = 0

    def open_shell(self, name=""):
        self._counter += 1
        shell = FakeShell(name or f"sh{self._counter}", parent=self)
        # Mirror the real connection: a shell arrives already initialised.
        for command in shell_init_commands(self.config):
            shell.send_line(command)
        self.shells.append(shell)
        return shell

    def stop_watching(self):
        pass

    def close(self, reason=""):
        self.is_connected = False

    def shutdown(self):
        self.close()


@pytest.fixture
def panel(qapp):
    """A connected terminal panel backed by fake shells."""
    config = JetsonSSHConfig(
        enabled=True,
        auto_connect=False,
        quick_commands=[QuickCommand("TOPIC LIST", "ros2 topic list")],
    )
    widget = JetsonTerminalWidget(config=config, host="10.0.0.5")
    if widget.connection is None:
        pytest.skip("paramiko unavailable, panel disabled")
    widget.connection = FakeConnection(config, widget)
    yield widget
    widget.deleteLater()


def idle(tab):
    """Marks a tab's shell as back at a prompt."""
    tab.shell.go_idle()


# --- Shell creation -------------------------------------------------------


def test_opening_a_shell_creates_a_tab(panel):
    tab = panel.open_shell_tab()
    assert tab is not None
    assert panel.shell_count == 1
    assert panel.active_tab() is tab


def test_multiple_shells_are_independent(panel):
    first = panel.open_shell_tab()
    second = panel.open_shell_tab()

    assert panel.shell_count == 2
    assert first.shell is not second.shell
    # Output into one shell must not appear in the other.
    first.shell.data_received.emit(b"only in the first shell")
    assert "only in the first shell" in first.view.visible_text()
    assert "only in the first shell" not in second.view.visible_text()


def test_shell_limit_is_enforced(panel):
    for _ in range(MAX_SHELLS + 3):
        panel.open_shell_tab()
    assert panel.shell_count == MAX_SHELLS


# --- The actual question: running a command while one is running ----------


def test_command_runs_in_the_current_shell_when_it_is_free(panel):
    tab = panel.open_shell_tab()
    idle(tab)
    assert tab.is_busy is False

    assert panel.run_command("ros2 topic list") is True
    assert "ros2 topic list" in tab.shell.sent_lines
    assert panel.shell_count == 1


def test_busy_shell_gets_a_new_shell_instead_of_keystrokes(panel):
    """The whole point: a running `ros2 launch` must not be typed into."""
    launching = panel.open_shell_tab()
    idle(launching)
    panel.run_command("ros2 launch rover_bringup rover.launch.py")
    assert launching.is_busy is True

    sent_before = list(launching.shell.sent_lines)
    assert panel.run_command("ros2 topic list") is True

    # A second shell was opened, and the launching one was left alone.
    assert panel.shell_count == 2
    assert launching.shell.sent_lines == sent_before
    assert "ros2 topic list" in panel.active_tab().shell.sent_lines


def test_a_shell_that_finishes_is_reused(panel):
    tab = panel.open_shell_tab()
    idle(tab)
    panel.run_command("ros2 topic list")
    assert tab.is_busy is True

    idle(tab)   # command finished, prompt returned
    panel.run_command("ros2 node list")

    assert panel.shell_count == 1
    assert tab.shell.sent_lines[-1] == "ros2 node list"


def test_command_with_no_shell_open_creates_one(panel):
    assert panel.shell_count == 0
    assert panel.run_command("ros2 topic list") is True
    assert panel.shell_count == 1


def test_busy_shell_at_the_limit_reports_instead_of_hijacking(panel):
    """At the shell limit a busy shell is still not typed into."""
    tabs = [panel.open_shell_tab() for _ in range(MAX_SHELLS)]
    for index, tab in enumerate(tabs):
        panel.tabs.setCurrentIndex(index)
        idle(tab)
        panel.run_command("sleep 100")
    assert all(t.is_busy for t in tabs)

    sent_before = [list(t.shell.sent_lines) for t in tabs]
    assert panel.run_command("ros2 topic list") is False
    assert [list(t.shell.sent_lines) for t in tabs] == sent_before


# --- Busy indication ------------------------------------------------------


def test_busy_shells_are_marked_in_the_tab_label(panel):
    tab = panel.open_shell_tab()
    idle(tab)
    assert not panel.tabs.tabText(0).startswith("●")

    panel.run_command("ros2 launch rover_bringup rover.launch.py")
    assert panel.tabs.tabText(0).startswith("●")

    idle(tab)
    assert not panel.tabs.tabText(0).startswith("●")


# --- Interrupt and teardown ----------------------------------------------


def test_interrupt_only_hits_the_active_shell(panel):
    first = panel.open_shell_tab()
    second = panel.open_shell_tab()
    panel.tabs.setCurrentIndex(1)

    panel.send_interrupt()

    assert second.shell.interrupts == 1
    assert first.shell.interrupts == 0


def test_closing_a_tab_closes_its_shell(panel):
    tab = panel.open_shell_tab()
    shell = tab.shell
    panel._close_tab(0)

    assert shell.is_alive is False
    assert panel.shell_count == 0


def test_new_shell_receives_the_idle_sentinel_setup(panel):
    """Without PROMPT_COMMAND installed, busy detection would never clear."""
    tab = panel.open_shell_tab()
    assert PROMPT_COMMAND_SETUP in tab.shell.sent_lines


def test_shell_init_installs_sentinel_before_user_commands():
    """The sentinel must be first: a startup command could block on input."""
    from mission_control.core.config import JetsonSSHConfig as Cfg

    commands = shell_init_commands(
        Cfg(startup_commands=["source /opt/ros/humble/setup.bash"], clear_on_open=True)
    )
    assert commands[0] == PROMPT_COMMAND_SETUP
    assert IDLE_SENTINEL in commands[0]
    assert "source /opt/ros/humble/setup.bash" in commands
    assert commands[-1] == "clear"


def test_shell_init_respects_clear_on_open_false():
    from mission_control.core.config import JetsonSSHConfig as Cfg

    commands = shell_init_commands(Cfg(startup_commands=[], clear_on_open=False))
    assert commands == [PROMPT_COMMAND_SETUP]
