"""Tests for the Jetson terminal: VT100 rendering and SSH degradation."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from mission_control.core.config import JetsonSSHConfig, QuickCommand
from mission_control.ui.components.jetson_terminal_widget import JetsonTerminalWidget
from mission_control.comms.jetson_ssh import IDLE_SENTINEL
from mission_control.ui.components.terminal_view import TerminalView, resolve_color


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def press(view: TerminalView, key, modifiers=Qt.NoModifier, text=""):
    """Sends a key press and returns the bytes the view would write to the PTY."""
    captured = []
    view.key_pressed.connect(captured.append)
    view.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, key, modifiers, text))
    view.key_pressed.disconnect()
    return captured[0] if captured else None


# --- Screen model ---------------------------------------------------------

def test_plain_output_renders(qapp):
    view = TerminalView()
    view.feed(b"ros2 topic list\r\n/imu/data\r\n")
    text = view.visible_text()
    assert "ros2 topic list" in text
    assert "/imu/data" in text


def test_ansi_colour_codes_are_consumed_not_printed(qapp):
    """Escape sequences must style the text, never appear as literal characters."""
    view = TerminalView()
    view.feed(b"\x1b[1;31mERROR\x1b[0m done")
    text = view.visible_text()
    assert "ERROR done" in text
    assert "\x1b" not in text
    assert "[1;31m" not in text


def test_carriage_return_overwrite(qapp):
    """A progress line that rewrites itself must not stack up."""
    view = TerminalView()
    view.feed(b"progress 10%\rprogress 90%")
    display = view.visible_text()
    assert "progress 90%" in display
    assert display.count("progress") == 1


def test_clear_screen_sequence(qapp):
    view = TerminalView()
    view.feed(b"old output\r\n")
    view.feed(b"\x1b[2J\x1b[H")
    assert "old output" not in view.visible_text()


def test_utf8_split_across_reads(qapp):
    """A multi-byte character split by the network must still decode."""
    view = TerminalView()
    encoded = "✓".encode("utf-8")
    view.feed(encoded[:1])
    view.feed(encoded[1:])
    assert "✓" in view.visible_text()


def test_clear_resets_screen(qapp):
    view = TerminalView()
    view.feed(b"something")
    view.clear()
    assert view.visible_text() == ""


def test_resize_emits_new_grid(qapp):
    view = TerminalView()
    sizes = []
    view.resized.connect(lambda c, r: sizes.append((c, r)))
    view.resize(900, 500)
    view.show()
    qapp.processEvents()

    assert sizes, "resizing the widget must report a new PTY geometry"
    cols, rows = sizes[-1]
    assert cols == view.cols and rows == view.rows
    assert cols >= 20 and rows >= 5


# --- Colour mapping -------------------------------------------------------

def test_resolve_named_and_hex_colours(qapp):
    from PySide6.QtGui import QColor

    default = QColor("#000000")
    assert resolve_color("red", default).isValid()
    assert resolve_color("brightcyan", default).isValid()
    assert resolve_color("ff8800", default) == QColor("#ff8800")
    assert resolve_color("default", default) == default
    assert resolve_color("", default) == default


# --- Key translation ------------------------------------------------------

def test_enter_and_backspace(qapp):
    view = TerminalView()
    assert press(view, Qt.Key_Return) == b"\r"
    assert press(view, Qt.Key_Backspace) == b"\x7f"
    assert press(view, Qt.Key_Tab) == b"\t"


def test_ctrl_c_sends_interrupt(qapp):
    """Ctrl-C must reach the foreground process, not be swallowed by Qt."""
    view = TerminalView()
    assert press(view, Qt.Key_C, Qt.ControlModifier) == b"\x03"


def test_ctrl_d_and_ctrl_z(qapp):
    view = TerminalView()
    assert press(view, Qt.Key_D, Qt.ControlModifier) == b"\x04"
    assert press(view, Qt.Key_Z, Qt.ControlModifier) == b"\x1a"


def test_arrow_keys_for_shell_history(qapp):
    view = TerminalView()
    assert press(view, Qt.Key_Up) == b"\x1b[A"
    assert press(view, Qt.Key_Down) == b"\x1b[B"
    assert press(view, Qt.Key_Right) == b"\x1b[C"
    assert press(view, Qt.Key_Left) == b"\x1b[D"


def test_printable_text_passes_through(qapp):
    view = TerminalView()
    assert press(view, Qt.Key_A, Qt.NoModifier, "a") == b"a"


def test_shift_pageup_scrolls_locally_without_writing(qapp):
    """Scrollback is a view concern; it must not inject bytes into the shell."""
    view = TerminalView()
    # b"" means "handled here, write nothing", and so emits no key_pressed.
    assert view._key_to_bytes(
        QKeyEvent(QKeyEvent.KeyPress, Qt.Key_PageUp, Qt.ShiftModifier, "")
    ) == b""
    assert press(view, Qt.Key_PageUp, Qt.ShiftModifier) is None


def test_plain_pageup_reaches_the_shell(qapp):
    """Without Shift, PageUp belongs to the remote program, not the scrollback."""
    view = TerminalView()
    assert press(view, Qt.Key_PageUp) == b"\x1b[5~"


# --- Panel behaviour ------------------------------------------------------

def test_panel_reports_offline_before_connecting(qapp):
    widget = JetsonTerminalWidget(
        config=JetsonSSHConfig(enabled=True, auto_connect=False), host="10.0.0.5"
    )
    assert widget.status_text() in ("OFFLINE", "UNAVAILABLE")
    assert widget.shell_count == 0


def test_auto_connect_reports_waiting(qapp):
    """Auto-connect must read as WAITING, not OFFLINE: it is still watching."""
    widget = JetsonTerminalWidget(
        config=JetsonSSHConfig(enabled=True, auto_connect=True), host="10.0.0.5"
    )
    if widget.connection is not None:
        assert widget.status_text() == "WAITING"
        assert "automatically" in widget.terminal.visible_text()
    widget.shutdown()


def test_disabled_panel_explains_itself(qapp):
    widget = JetsonTerminalWidget(config=JetsonSSHConfig(enabled=False), host="10.0.0.5")
    assert widget.status_text() == "DISABLED"
    assert not widget.connect_btn.isEnabled()
    assert "disabled" in widget.terminal.visible_text().lower()


def test_quick_command_buttons_built_from_config(qapp):
    config = JetsonSSHConfig(
        enabled=True,
        auto_connect=False,
        quick_commands=[
            QuickCommand("TOPIC LIST", "ros2 topic list"),
            QuickCommand("NODE LIST", "ros2 node list"),
        ],
    )
    widget = JetsonTerminalWidget(config=config, host="10.0.0.5")
    assert [b.text() for b in widget.quick_buttons] == ["TOPIC LIST", "NODE LIST"]
    assert "ros2 topic list" in widget.quick_buttons[0].toolTip()


def test_command_without_session_is_refused(qapp):
    """Clicking a quick command offline must report, not raise."""
    widget = JetsonTerminalWidget(
        config=JetsonSSHConfig(enabled=True, auto_connect=False), host="10.0.0.5"
    )
    assert widget.run_command("ros2 topic list") is False
    assert "Not connected" in widget.terminal.visible_text()


def test_interrupt_without_session_is_safe(qapp):
    widget = JetsonTerminalWidget(
        config=JetsonSSHConfig(enabled=True, auto_connect=False), host="10.0.0.5"
    )
    assert widget.send_interrupt() is False


def test_shutdown_without_session_is_safe(qapp):
    widget = JetsonTerminalWidget(
        config=JetsonSSHConfig(enabled=True, auto_connect=False), host="10.0.0.5"
    )
    widget.shutdown()
    assert widget.connection is None


def test_open_shell_tab_requires_a_connection(qapp):
    widget = JetsonTerminalWidget(
        config=JetsonSSHConfig(enabled=True, auto_connect=False), host="10.0.0.5"
    )
    assert widget.open_shell_tab() is None
    assert widget.shell_count == 0


# --- Busy / idle tracking -------------------------------------------------


def test_view_starts_busy_until_a_prompt_arrives(qapp):
    """Nothing has reported a prompt yet, so the shell is not known to be free."""
    view = TerminalView()
    assert view.is_busy is True


def test_idle_sentinel_clears_busy(qapp):
    """The shell's PROMPT_COMMAND emits an OSC title before every prompt."""
    view = TerminalView()
    idle_events = []
    view.became_idle.connect(lambda: idle_events.append(1))

    view.feed(f"\x1b]0;{IDLE_SENTINEL}\x07".encode())

    assert view.is_busy is False
    assert idle_events == [1]


def test_submitting_a_command_marks_busy_again(qapp):
    view = TerminalView()
    view.feed(f"\x1b]0;{IDLE_SENTINEL}\x07".encode())
    assert view.is_busy is False

    press(view, Qt.Key_Return)
    assert view.is_busy is True

    view.feed(f"\x1b]0;{IDLE_SENTINEL}\x07".encode())
    assert view.is_busy is False


def test_sentinel_is_not_printed_to_the_screen(qapp):
    view = TerminalView()
    view.feed(f"\x1b]0;{IDLE_SENTINEL}\x07ready".encode())
    text = view.visible_text()
    assert "ready" in text
    assert IDLE_SENTINEL not in text


def test_unrelated_titles_do_not_clear_busy(qapp):
    """Only our sentinel counts; a program setting its own title must not."""
    view = TerminalView()
    press(view, Qt.Key_Return)
    view.feed(b"\x1b]0;vim README.md\x07")
    assert view.is_busy is True


# --- Reachability and auto-connect ----------------------------------------


def test_reachable_host_is_detected(qapp):
    """A listening socket must read as reachable."""
    import socket

    from mission_control.comms.jetson_ssh import is_host_reachable

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        assert is_host_reachable("127.0.0.1", port, timeout=2.0) is True
    finally:
        server.close()


def test_closed_port_is_not_reachable(qapp):
    """A bound-then-closed port must read as down, without raising."""
    import socket

    from mission_control.comms.jetson_ssh import is_host_reachable

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.close()

    assert is_host_reachable("127.0.0.1", port, timeout=1.0) is False


def test_unroutable_host_times_out_cleanly(qapp):
    """TEST-NET-1 is unroutable; the probe must return False, not hang forever."""
    import time

    from mission_control.comms.jetson_ssh import is_host_reachable

    started = time.monotonic()
    assert is_host_reachable("192.0.2.99", 22, timeout=1.0) is False
    assert time.monotonic() - started < 5.0


def test_auto_connect_does_not_fire_without_the_flag(qapp):
    from mission_control.comms.jetson_ssh import JetsonSSHConnection

    config = JetsonSSHConfig(enabled=True, auto_connect=False)
    connection = JetsonSSHConnection(config, "192.0.2.99")
    connection.start_watching()

    assert connection._watch_timer.isActive() is False
    connection.shutdown()


def test_auto_connect_starts_watching(qapp):
    from mission_control.comms.jetson_ssh import JetsonSSHConnection

    config = JetsonSSHConfig(enabled=True, auto_connect=True, reconnect_interval_s=5)
    connection = JetsonSSHConnection(config, "192.0.2.99")
    connection.start_watching()

    assert connection._watch_timer.isActive() is True
    assert connection._watch_timer.interval() == 5000
    connection.shutdown()
    assert connection._watch_timer.isActive() is False


def test_disconnect_stops_auto_reconnect(qapp):
    """An operator pressing DISCONNECT must not be reconnected a second later."""
    widget = JetsonTerminalWidget(
        config=JetsonSSHConfig(enabled=True, auto_connect=True), host="192.0.2.99"
    )
    if widget.connection is None:
        pytest.skip("paramiko unavailable")

    widget.disconnect_session()
    assert widget.connection._watch_timer.isActive() is False
    widget.shutdown()
