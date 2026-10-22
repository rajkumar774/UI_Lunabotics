"""Jetson terminal panel: SSH shells on the rover, inside the operator UI.

This is the command path. Topics reach Mission Control over DDS because both
hosts share a ROS_DOMAIN_ID; there is no shell involved in that. Starting nodes,
inspecting the graph with the `ros2` CLI, checking `jtop` or rebooting the Jetson
all need a shell, which is what this panel provides.

**Multiple shells, not one.** A single shell can only run one foreground process
at a time, so `ros2 launch` would block the operator out of the terminal for the
rest of the run. This panel therefore runs tabbed shells over one SSH
connection, and a quick command fired while the current shell is busy opens a
new tab instead of being typed into a running process.

The widget owns no rover state and never writes to StateManager.
"""

from typing import List, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from mission_control.comms.jetson_ssh import (
    JetsonSSHConnection,
    JetsonShell,
    is_paramiko_available,
)
from mission_control.core.config import JetsonSSHConfig
from mission_control.core.logger import get_logger
from mission_control.telemetry.state import Severity
from mission_control.ui.components.telemetry_primitives import apply_severity
from mission_control.ui.components.terminal_view import TerminalView

logger = get_logger("ui.jetson_terminal")

#: Quick-command buttons per row.
QUICK_COMMAND_COLUMNS = 3

#: Shells the operator may have open at once. Each is a PTY on the Jetson.
MAX_SHELLS = 6


class ShellTab(QWidget):
    """One terminal tab: a TerminalView bound to one JetsonShell."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.view = TerminalView(self)
        layout.addWidget(self.view)
        self.shell: Optional[JetsonShell] = None

    def bind(self, shell: JetsonShell) -> None:
        """Attaches a live shell to this tab's view."""
        self.shell = shell
        shell.data_received.connect(self.view.feed)
        self.view.key_pressed.connect(self._on_key)
        self.view.resized.connect(self._on_resized)
        shell.resize(self.view.cols, self.view.rows)

    def unbind(self) -> None:
        self.shell = None

    @property
    def is_live(self) -> bool:
        return self.shell is not None and self.shell.is_alive

    @property
    def is_busy(self) -> bool:
        """Busy only means anything for a live shell."""
        return self.is_live and self.view.is_busy

    def run(self, command: str) -> bool:
        if not self.is_live:
            return False
        self.view.mark_command_submitted()
        return self.shell.send_line(command)

    def _on_key(self, data: bytes) -> None:
        if self.is_live:
            self.shell.send(data)

    def _on_resized(self, cols: int, rows: int) -> None:
        if self.is_live:
            self.shell.resize(cols, rows)


class JetsonTerminalWidget(QFrame):
    """Tabbed SSH terminal with auto-connect and one-click commands."""

    connection_changed = Signal(bool)
    #: Emitted whenever the panel's status text changes, so the header's
    #: JETSON badge mirrors it without duplicating the state machine.
    status_changed = Signal(str)

    def __init__(
        self,
        config: Optional[JetsonSSHConfig] = None,
        host: str = "",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setObjectName("TerminalPanel")
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.config = config or JetsonSSHConfig()
        self.host = host or self.config.host or "jetson"
        self.connection: Optional[JetsonSSHConnection] = None
        self.quick_buttons: List[QPushButton] = []
        self._placeholder_view: Optional[TerminalView] = None

        self._build_ui()
        self._apply_availability()

    # -- Construction --------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(7)

        layout.addWidget(self._build_header())

        quick_bar = self._build_quick_commands()
        if quick_bar is not None:
            layout.addWidget(quick_bar)

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("TerminalTabs")
        self.tabs.setTabsClosable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(lambda _i: self._refresh_tab_labels())
        layout.addWidget(self.tabs, stretch=1)

        # Shown before the first shell exists, so the panel is never an empty box.
        self._placeholder_view = TerminalView(self)
        layout.addWidget(self._placeholder_view, stretch=1)
        self.tabs.hide()

    def _build_header(self) -> QWidget:
        header = QWidget(self)
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        title = QLabel("JETSON TERMINAL")
        title.setObjectName("TelemetryCardTitle")
        row.addWidget(title)

        self.target_label = QLabel(f"{self.config.username}@{self.host}")
        self.target_label.setObjectName("TerminalTarget")
        row.addWidget(self.target_label)

        row.addStretch()

        self.status_label = QLabel("OFFLINE")
        self.status_label.setObjectName("StatusValue")
        apply_severity(self.status_label, Severity.OFFLINE)
        row.addWidget(self.status_label)

        self.new_tab_btn = QPushButton("+ SHELL")
        self.new_tab_btn.setObjectName("TerminalButton")
        self.new_tab_btn.setToolTip("Open another shell, so a running command does not block you")
        self.new_tab_btn.clicked.connect(lambda: self.open_shell_tab())
        row.addWidget(self.new_tab_btn)

        self.connect_btn = QPushButton("CONNECT")
        self.connect_btn.setObjectName("TerminalButton")
        self.connect_btn.clicked.connect(self.toggle_connection)
        row.addWidget(self.connect_btn)

        self.interrupt_btn = QPushButton("^C")
        self.interrupt_btn.setObjectName("TerminalButton")
        self.interrupt_btn.setToolTip("Send Ctrl-C to the foreground process in this shell")
        self.interrupt_btn.clicked.connect(self.send_interrupt)
        row.addWidget(self.interrupt_btn)

        self.clear_btn = QPushButton("CLEAR")
        self.clear_btn.setObjectName("TerminalButton")
        self.clear_btn.clicked.connect(self.clear)
        row.addWidget(self.clear_btn)

        return header

    def _build_quick_commands(self) -> Optional[QWidget]:
        commands = self.config.quick_commands
        if not commands:
            return None

        container = QWidget(self)
        grid = QGridLayout(container)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)

        for index, entry in enumerate(commands):
            button = QPushButton(entry.label)
            button.setObjectName("QuickCommandButton")
            button.setToolTip(f"{entry.command}\n(runs in a new shell if this one is busy)")
            button.clicked.connect(
                lambda _checked=False, cmd=entry.command: self.run_command(cmd)
            )
            grid.addWidget(button, index // QUICK_COMMAND_COLUMNS, index % QUICK_COMMAND_COLUMNS)
            self.quick_buttons.append(button)

        return container

    def _apply_availability(self) -> None:
        """Disables the panel with an explanation when it cannot work."""
        if not self.config.enabled:
            self._set_status("DISABLED", Severity.OFFLINE)
            self._set_controls_enabled(False)
            self._write_placeholder(
                "\r\n  Jetson terminal disabled in config/rover_config.yaml "
                "(jetson_ssh.enabled: false).\r\n"
            )
            return

        if not is_paramiko_available():
            self._set_status("UNAVAILABLE", Severity.ERROR)
            self._set_controls_enabled(False)
            self._write_placeholder(
                "\r\n  paramiko is not installed, so the SSH terminal cannot start.\r\n"
                "  Install it with:  pip install paramiko\r\n"
            )
            return

        self.connection = JetsonSSHConnection(self.config, self.host, parent=self)
        self.connection.connected.connect(self._on_connected)
        self.connection.disconnected.connect(self._on_disconnected)
        self.connection.error_occurred.connect(self._on_error)
        self.connection.reachability_changed.connect(self._on_reachability_changed)

        mode = (
            "Waiting for the Jetson to come online — will connect automatically."
            if self.config.auto_connect
            else "Press CONNECT to open an interactive shell on the rover."
        )
        self._write_placeholder(
            f"\r\n  Jetson terminal ready — target {self.config.username}@{self.host}:{self.config.port}\r\n"
            f"  {mode}\r\n"
            "  Shell commands only; telemetry arrives over DDS, not through this session.\r\n\r\n"
        )

        if self.config.auto_connect:
            self._set_status("WAITING", Severity.WARNING)
            self.connection.start_watching()

    # -- Session control -----------------------------------------------------

    def toggle_connection(self) -> None:
        if self.connection is None:
            return
        if self.connection.is_connected:
            self.disconnect_session()
        else:
            self.connect_session()

    def connect_session(self) -> None:
        """Opens the SSH connection (non-blocking)."""
        if self.connection is None or self.connection.is_connected:
            return
        self._set_status("CONNECTING", Severity.WARNING)
        self.connect_btn.setText("CANCEL")
        self._write_placeholder(
            f"\r\n  Connecting to {self.config.username}@{self.host}:{self.config.port} ...\r\n"
        )
        self.connection.connect()

    def disconnect_session(self) -> None:
        """Closes the connection and stops auto-reconnect until asked again."""
        if self.connection is None:
            return
        self.connection.stop_watching()
        self.connection.close(reason="disconnected by operator")

    def open_shell_tab(self, name: str = "") -> Optional[ShellTab]:
        """Opens an additional shell and gives it its own tab."""
        if self.connection is None or not self.connection.is_connected:
            return None
        if self.tabs.count() >= MAX_SHELLS:
            self._write_active(f"\r\n  Shell limit reached ({MAX_SHELLS}).\r\n")
            return None

        shell = self.connection.open_shell(name)
        if shell is None:
            return None

        tab = ShellTab(self)
        tab.bind(shell)
        tab.view.became_idle.connect(self._refresh_tab_labels)
        index = self.tabs.addTab(tab, shell.name)
        self.tabs.setCurrentIndex(index)
        self._show_tabs()
        self._refresh_tab_labels()
        return tab

    def run_command(self, command: str) -> bool:
        """Runs a command, opening a new shell if the current one is busy.

        This is the whole point of the tabbed design: dispatching into a shell
        that is already running `ros2 launch` would just feed keystrokes to that
        process, so a busy shell gets a fresh one instead.
        """
        if self.connection is None or not self.connection.is_connected:
            self._write_placeholder(f"\r\n  Not connected — cannot run: {command}\r\n")
            return False

        tab = self.active_tab()
        if tab is None or not tab.is_live:
            tab = self.open_shell_tab()
        elif tab.is_busy:
            busy_in = self.tabs.tabText(self.tabs.currentIndex())
            fresh = self.open_shell_tab()
            if fresh is None:
                self._write_active(
                    f"\r\n  {busy_in} is busy and no further shell is available.\r\n"
                )
                return False
            logger.info(f"'{busy_in}' busy — running '{command}' in a new shell.")
            tab = fresh

        if tab is None:
            return False

        ok = tab.run(command)
        self._refresh_tab_labels()
        return ok

    def send_interrupt(self) -> bool:
        """Sends Ctrl-C to the active shell only."""
        tab = self.active_tab()
        if tab is None or not tab.is_live:
            return False
        return tab.shell.send_interrupt()

    def clear(self) -> None:
        tab = self.active_tab()
        (tab.view if tab is not None else self._placeholder_view).clear()

    def shutdown(self) -> None:
        """Closes every shell and the connection during application teardown."""
        if self.connection is not None:
            self.connection.shutdown()
            self.connection = None

    # -- Tab helpers ---------------------------------------------------------

    def active_tab(self) -> Optional[ShellTab]:
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, ShellTab) else None

    @property
    def shell_count(self) -> int:
        return self.tabs.count()

    @property
    def terminal(self) -> TerminalView:
        """The view the operator is currently looking at."""
        tab = self.active_tab()
        return tab.view if tab is not None else self._placeholder_view

    def _close_tab(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if isinstance(widget, ShellTab) and widget.shell is not None:
            widget.shell.close(reason="tab closed by operator")
        self.tabs.removeTab(index)
        widget.deleteLater()
        if self.tabs.count() == 0:
            self._show_placeholder()

    def _refresh_tab_labels(self) -> None:
        """Marks busy shells so the operator can see what is still running."""
        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if not isinstance(widget, ShellTab) or widget.shell is None:
                continue
            name = widget.shell.name
            self.tabs.setTabText(index, f"● {name}" if widget.is_busy else name)

    def _show_tabs(self) -> None:
        if self._placeholder_view is not None:
            self._placeholder_view.hide()
        self.tabs.show()

    def _show_placeholder(self) -> None:
        self.tabs.hide()
        if self._placeholder_view is not None:
            self._placeholder_view.show()

    def _write_placeholder(self, text: str) -> None:
        if self._placeholder_view is not None:
            self._placeholder_view.write_local(text)

    def _write_active(self, text: str) -> None:
        self.terminal.write_local(text)

    # -- Signal handlers -----------------------------------------------------

    def _on_connected(self, target: str) -> None:
        self._set_status("CONNECTED", Severity.NORMAL)
        self.connect_btn.setText("DISCONNECT")
        self.target_label.setText(target)
        self.connection_changed.emit(True)
        logger.info(f"Jetson terminal connected: {target}")
        if self.tabs.count() == 0:
            self.open_shell_tab()

    def _on_disconnected(self, reason: str) -> None:
        self._set_status("OFFLINE", Severity.OFFLINE)
        self.connect_btn.setText("CONNECT")
        for index in reversed(range(self.tabs.count())):
            widget = self.tabs.widget(index)
            self.tabs.removeTab(index)
            widget.deleteLater()
        self._show_placeholder()
        self._write_placeholder(f"\r\n  Session closed — {reason}\r\n")
        self.connection_changed.emit(False)

    def _on_error(self, message: str) -> None:
        # Auto-connect retries on its own schedule, so a failed attempt returns
        # to WAITING rather than looking like a dead end.
        if self.config.auto_connect and self.connection is not None:
            self._set_status("WAITING", Severity.WARNING)
        else:
            self._set_status("ERROR", Severity.ERROR)
        self.connect_btn.setText("CONNECT")
        self._write_placeholder(f"\r\n  SSH error: {message}\r\n")

    def _on_reachability_changed(self, reachable: bool) -> None:
        if reachable:
            self._write_placeholder("\r\n  Jetson is online — connecting...\r\n")
        elif not (self.connection and self.connection.is_connected):
            self._set_status("WAITING", Severity.WARNING)

    def _set_status(self, text: str, severity: Severity) -> None:
        if self.status_label.text() == text:
            return
        self.status_label.setText(text)
        apply_severity(self.status_label, severity)
        self.status_changed.emit(text)

    def _set_controls_enabled(self, enabled: bool) -> None:
        for button in (self.connect_btn, self.new_tab_btn, self.interrupt_btn):
            button.setEnabled(enabled)

    def status_text(self) -> str:
        return self.status_label.text()
