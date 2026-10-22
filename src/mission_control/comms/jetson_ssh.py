"""SSH connection to the Jetson, used by the in-UI terminal.

This is the command path, and it is deliberately separate from the data path:
telemetry and control topics travel over DDS between the two ROS 2 hosts, while
this carries the keystrokes and output of interactive shells. Nothing read from
here is ever written into StateManager.

Structure mirrors how SSH actually works: one *connection* (one TCP socket, one
authentication) carrying any number of *shells*, each its own PTY channel. That
is what lets an operator start `ros2 launch` in one shell and keep working in
another, instead of being blocked behind a running foreground process.

A real PTY is requested rather than running one command at a time, so a long
launch streams its output live, Ctrl-C reaches the foreground process, and
curses programs such as `jtop` render correctly.

paramiko is imported lazily: the terminal is an optional panel and the rest of
Mission Control must start on a host without it.
"""

import os
import socket
import threading
import time
from typing import List, Optional

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from mission_control.core.config import JetsonSSHConfig
from mission_control.core.logger import get_logger

logger = get_logger("jetson_ssh")

#: Bytes pulled from a channel per read.
READ_CHUNK = 8192

#: Idle poll interval while a channel has nothing to deliver.
IDLE_POLL_S = 0.02

#: Written to the terminal title by the remote shell every time it returns to a
#: prompt, via PROMPT_COMMAND. The terminal uses it to tell busy from idle.
IDLE_SENTINEL = "MC_IDLE"

#: Installed in every shell so the sentinel above is emitted before each prompt.
#: OSC 0 is consumed by the terminal emulator, so nothing is printed on screen.
PROMPT_COMMAND_SETUP = (
    "export PROMPT_COMMAND='printf \"\\033]0;" + IDLE_SENTINEL + "\\007\"'"
)

#: How long a reachability probe waits before calling the host down.
PROBE_TIMEOUT_S = 1.5


def is_paramiko_available() -> bool:
    """Checks whether the SSH backend can be imported on this host."""
    try:
        import paramiko  # noqa: F401
        return True
    except (ImportError, ModuleNotFoundError):
        return False


def shell_init_commands(config: JetsonSSHConfig) -> List[str]:
    """Commands run in every new shell, in order.

    The PROMPT_COMMAND sentinel comes first and is not optional: the terminal's
    busy/idle detection depends on it, and without it a shell would look busy
    forever and every command would open a new tab.
    """
    commands = [PROMPT_COMMAND_SETUP]
    commands.extend(config.startup_commands)
    if config.clear_on_open:
        commands.append("clear")
    return commands


def is_host_reachable(host: str, port: int, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """True when a TCP connection to host:port completes.

    Used to notice the Jetson booting without hammering it with full SSH
    authentication attempts.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class JetsonShell(QObject):
    """One interactive shell (one SSH channel with its own PTY)."""

    data_received = Signal(bytes)
    closed = Signal(str)

    def __init__(self, channel, name: str, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._channel = channel
        self._name = name
        self._alive = True
        self._lock = threading.Lock()
        self._reader = _ChannelReader(channel, self)
        self._reader.start()

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_alive(self) -> bool:
        return self._alive

    def send(self, data: bytes) -> bool:
        """Writes raw bytes (keystrokes) to the remote PTY."""
        with self._lock:
            channel = self._channel
            if channel is None or not self._alive:
                return False
        try:
            channel.sendall(data)
            return True
        except Exception as exc:
            self.close(reason=f"write failed: {exc}")
            return False

    def send_line(self, text: str) -> bool:
        """Writes a command followed by a newline."""
        return self.send((text + "\n").encode("utf-8", errors="replace"))

    def send_interrupt(self) -> bool:
        """Sends Ctrl-C (ETX) to the foreground process."""
        return self.send(b"\x03")

    def resize(self, cols: int, rows: int) -> None:
        """Tells the remote PTY the terminal geometry changed."""
        with self._lock:
            channel = self._channel
            if channel is None or not self._alive:
                return
        try:
            channel.resize_pty(width=max(20, cols), height=max(5, rows))
        except Exception as exc:
            logger.debug(f"resize_pty failed on {self._name}: {exc}")

    def close(self, reason: str = "closed") -> None:
        """Closes this shell's channel and stops its reader."""
        with self._lock:
            if not self._alive:
                return
            self._alive = False
            channel = self._channel
            self._channel = None

        reader, self._reader = self._reader, None
        if reader is not None:
            reader.stop()
        try:
            if channel is not None:
                channel.close()
        except Exception:
            pass

        logger.info(f"Jetson shell '{self._name}' closed ({reason}).")
        self.closed.emit(reason)

    # -- Reader callbacks ---------------------------------------------------

    def _on_reader_data(self, data: bytes) -> None:
        self.data_received.emit(data)

    def _on_reader_finished(self, reason: str) -> None:
        self.close(reason=reason)


class JetsonSSHConnection(QObject):
    """One authenticated SSH connection, carrying any number of shells.

    Connecting runs on a background thread because a TCP connect to an
    unreachable Jetson blocks for the full timeout, which must never happen on
    the Qt thread. When auto-connect is enabled the connection also watches for
    the Jetson coming online and attaches by itself.
    """

    connected = Signal(str)          # "user@host:port"
    disconnected = Signal(str)       # reason
    error_occurred = Signal(str)
    reachability_changed = Signal(bool)
    shell_opened = Signal(object)    # JetsonShell

    def __init__(self, config: JetsonSSHConfig, host: str, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.config = config
        self.host = host

        self._client = None
        self._shells: List[JetsonShell] = []
        self._is_connected = False
        self._connecting = False
        self._closing = False
        self._host_reachable = False
        self._shell_counter = 0
        self._lock = threading.Lock()
        self._probe: Optional[_ReachabilityProbe] = None

        # Drives auto-connect and auto-reconnect. Lives on the Qt thread; the
        # blocking work it triggers does not.
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(max(2, config.reconnect_interval_s) * 1000)
        self._watch_timer.timeout.connect(self._tick_watch)

    # -- Properties ----------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def is_connecting(self) -> bool:
        return self._connecting

    @property
    def host_reachable(self) -> bool:
        """Whether the last probe found the Jetson's SSH port open."""
        return self._host_reachable

    @property
    def shells(self) -> List[JetsonShell]:
        return list(self._shells)

    @property
    def target(self) -> str:
        return f"{self.config.username}@{self.host}:{self.config.port}"

    # -- Lifecycle -----------------------------------------------------------

    def start_watching(self) -> None:
        """Begins probing for the Jetson and connecting when it appears."""
        if not self.config.auto_connect:
            return
        if not self._watch_timer.isActive():
            self._watch_timer.start()
        self._tick_watch()

    def stop_watching(self) -> None:
        self._watch_timer.stop()

    def connect(self) -> None:
        """Opens the connection on a background thread. Returns immediately."""
        if self._is_connected or self._connecting:
            return
        if not is_paramiko_available():
            self.error_occurred.emit(
                "paramiko is not installed — run `pip install paramiko` to enable the Jetson terminal."
            )
            return

        self._closing = False
        self._connecting = True
        thread = threading.Thread(
            target=self._connect_blocking, name="jetson-ssh-connect", daemon=True
        )
        thread.start()

    def _connect_blocking(self) -> None:
        import paramiko

        try:
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            # The rover is a known device on a private competition LAN whose host
            # key changes whenever the Jetson is reflashed. Refusing to connect
            # mid-run would be worse than accepting a new key here.
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            key_path = os.path.expanduser(self.config.key_path.strip()) if self.config.key_path.strip() else None
            if key_path and not os.path.exists(key_path):
                logger.info(f"SSH key {key_path} not found; falling back to agent/default keys.")
                key_path = None

            client.connect(
                hostname=self.host,
                port=self.config.port,
                username=self.config.username,
                key_filename=key_path,
                timeout=self.config.connect_timeout_s,
                auth_timeout=self.config.connect_timeout_s,
                banner_timeout=self.config.connect_timeout_s,
                allow_agent=True,
                look_for_keys=True,
            )

            transport = client.get_transport()
            if transport is not None and self.config.keepalive_s > 0:
                transport.set_keepalive(self.config.keepalive_s)

            with self._lock:
                self._client = client
                self._is_connected = True
                self._connecting = False

            logger.info(f"Jetson SSH connection established: {self.target}")
            self.connected.emit(self.target)

        except Exception as exc:
            self._connecting = False
            self._is_connected = False
            message = f"{type(exc).__name__}: {exc}"
            logger.warning(f"Jetson SSH connect to {self.target} failed — {message}")
            self.error_occurred.emit(message)

    def open_shell(self, name: str = "") -> Optional[JetsonShell]:
        """Opens an additional shell on the existing connection.

        Each shell is an independent PTY channel, so a long-running command in
        one does not block another.
        """
        with self._lock:
            client = self._client
            if client is None or not self._is_connected:
                return None

        try:
            channel = client.invoke_shell(term="xterm-256color", width=100, height=28)
            channel.settimeout(0.0)
        except Exception as exc:
            logger.error(f"Failed to open Jetson shell: {exc}")
            self.error_occurred.emit(f"could not open shell: {exc}")
            return None

        self._shell_counter += 1
        shell = JetsonShell(channel, name or f"sh{self._shell_counter}", parent=self)
        shell.closed.connect(lambda _reason, s=shell: self._on_shell_closed(s))
        self._shells.append(shell)

        for command in shell_init_commands(self.config):
            shell.send_line(command)

        logger.info(f"Jetson shell '{shell.name}' opened on {self.target}.")
        self.shell_opened.emit(shell)
        return shell

    def close(self, reason: str = "closed by operator") -> None:
        """Closes every shell and the underlying connection."""
        self._closing = True
        shells, self._shells = self._shells, []
        for shell in shells:
            shell.close(reason=reason)

        with self._lock:
            client = self._client
            self._client = None
            was_connected = self._is_connected
            self._is_connected = False
            self._connecting = False

        try:
            if client is not None:
                client.close()
        except Exception:
            pass

        if was_connected:
            logger.info(f"Jetson SSH connection closed ({reason}).")
            self.disconnected.emit(reason)

    def shutdown(self) -> None:
        """Stops watching and tears the connection down."""
        self.stop_watching()
        if self._probe is not None:
            self._probe.stop()
            self._probe = None
        self.close(reason="application shutting down")

    # -- Auto-connect --------------------------------------------------------

    def _tick_watch(self) -> None:
        """Probes reachability, then connects if the Jetson is up and we are not."""
        if self._closing or self._is_connected or self._connecting:
            return
        if self._probe is not None and self._probe.isRunning():
            return

        self._probe = _ReachabilityProbe(self.host, self.config.port, self)
        self._probe.result.connect(self._on_probe_result)
        self._probe.start()

    def _on_probe_result(self, reachable: bool) -> None:
        if reachable != self._host_reachable:
            self._host_reachable = reachable
            logger.info(
                f"Jetson {self.host}:{self.config.port} "
                f"{'came online' if reachable else 'went offline'}."
            )
            self.reachability_changed.emit(reachable)

        if reachable and not self._is_connected and not self._connecting and not self._closing:
            self.connect()

    def _on_shell_closed(self, shell: JetsonShell) -> None:
        if shell in self._shells:
            self._shells.remove(shell)

        # Every shell gone while still nominally connected means the transport
        # died under us; drop the connection so the watcher can re-establish it.
        with self._lock:
            client = self._client
            connected = self._is_connected
        if connected and client is not None:
            transport = client.get_transport()
            if transport is None or not transport.is_active():
                self.close(reason="transport lost")


class _ReachabilityProbe(QThread):
    """One-shot background TCP probe, so the Qt thread never waits on a socket."""

    result = Signal(bool)

    def __init__(self, host: str, port: int, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._host = host
        self._port = port

    def stop(self) -> None:
        self.requestInterruption()
        if self.isRunning():
            self.wait(PROBE_TIMEOUT_S * 1000 + 500)

    def run(self) -> None:
        reachable = is_host_reachable(self._host, self._port)
        if not self.isInterruptionRequested():
            self.result.emit(reachable)


class _ChannelReader(QThread):
    """Drains one SSH channel and hands bytes to its shell."""

    def __init__(self, channel, shell: JetsonShell):
        super().__init__(shell)
        self._channel = channel
        self._shell = shell
        self._running = True

    def stop(self) -> None:
        self._running = False
        self.requestInterruption()
        if self.isRunning():
            self.wait(1500)

    def run(self) -> None:
        reason = "remote shell exited"
        while self._running and not self.isInterruptionRequested():
            try:
                if self._channel.closed or self._channel.exit_status_ready():
                    break
                if self._channel.recv_ready():
                    data = self._channel.recv(READ_CHUNK)
                    if not data:
                        break
                    self._shell._on_reader_data(data)
                    continue
                if self._channel.recv_stderr_ready():
                    data = self._channel.recv_stderr(READ_CHUNK)
                    if data:
                        self._shell._on_reader_data(data)
                    continue
                time.sleep(IDLE_POLL_S)
            except Exception as exc:
                reason = f"read error: {exc}"
                break

        if self._running:
            self._shell._on_reader_finished(reason)
