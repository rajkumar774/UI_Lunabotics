"""Main Window of the Rover Mission Control application."""

from pathlib import Path
from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
)

from mission_control.core.config import AppConfig, RoverConfig
from mission_control.core.event_bus import EventBus, get_event_bus
from mission_control.core.logger import get_logger
from mission_control.core.mission_clock import MissionClock
from mission_control.comms.base_client import BaseCommsClient
from mission_control.comms.mission_actions import MissionActionDispatcher
from mission_control.core.run_logger import RunLogger
from mission_control.telemetry.state import (
    ConnectionState,
    MissionPhase,
    RoverState,
    StateManager,
)
from mission_control.ui.components.autonomy_control_widget import MissionAction
from mission_control.ui.components.control_sidebar import ControlSidebar
from mission_control.ui.components.mission_status_widget import MissionStatusWidget
from mission_control.ui.components.telemetry_panel import TelemetryPanel
from mission_control.video.player_widget import LiveVideoPlayerWidget

logger = get_logger("ui.main_window")


class MainWindow(QMainWindow):
    """Primary Mission Control Operator Interface Window."""

    def __init__(
        self,
        app_config: AppConfig,
        rover_config: RoverConfig,
        comms_client: BaseCommsClient,
        parent: Optional[QWidget] = None,
        event_bus: Optional[EventBus] = None,
        state_manager: Optional[StateManager] = None,
    ):
        super().__init__(parent)
        self.app_config = app_config
        self.rover_config = rover_config
        self.comms_client = comms_client
        # Telemetry reaches the UI only through the EventBus, never straight from
        # the comms client, so the state layer stays the single source of truth.
        self.event_bus = event_bus if event_bus is not None else get_event_bus()
        # Mission phase is read from and written to application state, never held
        # in a widget. A standalone window without a pipeline gets its own manager.
        self.state_manager = state_manager if state_manager is not None else StateManager()

        # Operator actions resolve through config bindings, so the real
        # commands can be filled in without touching UI code.
        self.action_dispatcher = MissionActionDispatcher(
            comms_client=comms_client,
            bindings=rover_config.mission_actions,
            parent=self,
        )

        # The run countdown is operator-station time, not rover telemetry.
        self.mission_clock = MissionClock(
            duration_seconds=app_config.mission.run_duration_seconds,
            parent=self,
        )

        self._init_window()
        self._setup_ui()
        self._connect_signals()

    def _init_window(self) -> None:
        """Sets window properties from application configuration."""
        self.setWindowTitle(self.app_config.ui.window_title)
        self.resize(
            self.app_config.ui.default_width,
            self.app_config.ui.default_height,
        )
        self.setMinimumSize(
            self.app_config.ui.min_width,
            self.app_config.ui.min_height,
        )

    def _setup_ui(self) -> None:
        """Assembles the layout hierarchy."""
        central_widget = QWidget(self)
        central_widget.setObjectName("CentralWidget")
        self.setCentralWidget(central_widget)

        root_layout = QVBoxLayout(central_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 1. Header Bar (carries the clock controls inline; there is no
        #    separate mission phase row).
        header = self._create_header_bar()
        root_layout.addWidget(header)

        # Content Layout
        content_container = QWidget()
        content_layout = QVBoxLayout(content_container)
        content_layout.setContentsMargins(16, 14, 16, 14)
        content_layout.setSpacing(14)

        # 2. Main Visualization Area (Camera + Map Placeholders)
        main_splitter = self._create_main_visualization_area()
        content_layout.addWidget(main_splitter, stretch=4)

        # 3. Bottom Area (Telemetry strip — deliberately height-capped so the
        #    live camera keeps the majority of the viewport)
        bottom_panel = self._create_bottom_panel()
        content_layout.addWidget(bottom_panel, stretch=0)

        root_layout.addWidget(content_container, stretch=1)

    def _create_header_bar(self) -> QWidget:
        """Constructs the top mission control header bar."""
        header = QWidget()
        header.setObjectName("HeaderBar")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(20)

        # Title Block
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        title_label = QLabel("ROVER MISSION CONTROL")
        title_label.setObjectName("AppTitle")
        subtitle_label = QLabel(
            f"{self.rover_config.name.upper()} • {self.app_config.mission.competition_name.upper()}"
        )
        subtitle_label.setObjectName("AppSubtitle")
        title_block.addWidget(title_label)
        title_block.addWidget(subtitle_label)
        layout.addLayout(title_block)

        layout.addStretch()

        # ROVER LINK badge removed: the JETSON badge below covers the link
        # the operator can act on, and the telemetry cards already show
        # NO DATA when the topic link is down.
        # Jetson shell link. Separate from ROVER LINK on purpose: the topic
        # link and the command shell fail independently, and an operator needs
        # to know which one is down.
        jetson_block = QVBoxLayout()
        jetson_block.setSpacing(2)
        jetson_title = QLabel("JETSON")
        jetson_title.setObjectName("CardHeader")
        self.jetson_badge = QLabel("OFFLINE")
        self.jetson_badge.setProperty("class", "Badge BadgeDisconnected")
        self.jetson_badge.setAlignment(Qt.AlignCenter)
        jetson_block.addWidget(jetson_title)
        jetson_block.addWidget(self.jetson_badge)
        layout.addLayout(jetson_block)

        # Mission Countdown (competition run duration from configuration)
        self.mission_status = MissionStatusWidget(
            duration_seconds=self.app_config.mission.run_duration_seconds
        )
        self.mission_controls = self.mission_status.controls
        layout.addWidget(self.mission_status)

        # SAFETY STATUS, CONNECT MOCK and E-STOP removed from the header.
        # The E-Stop that matters is the physical one on the rover.

        return header

    def _create_main_visualization_area(self) -> QSplitter:
        """Constructs the central split-view: main camera left, operator controls right.

        The left pane is the primary camera with the secondary camera drawn as a
        picture-in-picture inset inside its 16:9 viewport. The right pane carries
        everything the operator acts through — mode, actions, status, and the
        Jetson shell.
        """
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        cameras = self.rover_config.cameras

        # 1. Primary camera (16:9 locked) with the secondary stream inset.
        self.video_player = LiveVideoPlayerWidget(self, camera_title=cameras.main.name)
        self.video_player.configure_pip(
            label=cameras.pip.name,
            corner=cameras.pip.corner,
            width_fraction=cameras.pip.width_fraction,
        )
        splitter.addWidget(self.video_player)

        # 2. Operator control column.
        self.control_sidebar = ControlSidebar(
            ssh_config=self.rover_config.jetson_ssh,
            ssh_host=self.rover_config.ssh_host,
            parent=self,
        )
        splitter.addWidget(self.control_sidebar)

        # The camera keeps the majority of the width; the sidebar needs enough
        # room for the terminal to hold a readable ~70 column line.
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([860, 560])
        return splitter

    @property
    def controls(self):
        """The mode/action control panel in the right-hand column."""
        return self.control_sidebar.controls

    @property
    def command_status(self):
        """The control status block in the right-hand column."""
        return self.control_sidebar.status

    @property
    def jetson_terminal(self):
        """The Jetson SSH terminal in the right-hand column."""
        return self.control_sidebar.terminal

    def _create_bottom_panel(self) -> QWidget:
        """Constructs the operator telemetry strip."""
        self.telemetry_panel = TelemetryPanel()
        self.telemetry_panel.setMaximumHeight(180)
        return self.telemetry_panel

    def _connect_signals(self) -> None:
        """Binds comms lifecycle signals and the EventBus telemetry feed to the UI."""
        self.comms_client.connection_state_changed.connect(self._on_connection_changed)
        self.comms_client.command_feedback_received.connect(self._on_command_feedback)
        self.event_bus.telemetry_updated.connect(self._on_telemetry_snapshot)

        # Mission clock <-> header display
        self.mission_clock.tick.connect(self.mission_status.set_remaining)
        # One folder per run, written when the clock reaches zero. Recording
        # follows the CLOCK, not the link: a run that loses telemetry halfway
        # is exactly the run whose log is worth having.
        self.run_logger = RunLogger(
            output_dir=Path(self.app_config.mission.run_log_dir))
        self.mission_clock.running_changed.connect(self._on_clock_running_changed)
        self.mission_clock.finished.connect(self._on_mission_finished)
        self.mission_clock.running_changed.connect(self.mission_status.set_running)
        self.mission_controls.start_pause_requested.connect(self.mission_clock.toggle)
        self.mission_controls.reset_requested.connect(self._on_mission_reset)

        # Operator control column
        self.action_dispatcher.ssh_runner = self.jetson_terminal.run_command
        self.controls.add_system_buttons([
            (n, b.label or n, b.is_mock)
            for n, b in self.rover_config.mission_actions.items()
            if b.is_system
        ])
        self.controls.system_requested.connect(self._on_system_requested)
        self.controls.action_requested.connect(self._on_action_requested)
        self.controls.estop_requested.connect(self._on_estop_requested)
        self.jetson_terminal.status_changed.connect(self._render_jetson_status)

        self.controls.set_controls_enabled(self.comms_client.is_connected)
        self.controls.set_mock_mode(
            not any(
                not b.is_mock
                for n, b in self.action_dispatcher.bindings.items()
                if not b.is_system
            )
        )
        # The panel settles its status during construction, before this
        # connection existed, so seed the badge from where it ended up.
        self._render_jetson_status(self.jetson_terminal.status_text())

    # -- Operator control handlers -------------------------------------------

    def _on_action_requested(self, action: MissionAction) -> None:
        """Operator dispatched a one-shot mission action.

        Where it goes is decided by the `mission_actions` bindings in
        rover_config.yaml, not here — see comms/mission_actions.py.
        """
        accepted = self.action_dispatcher.dispatch(action.value)
        if not accepted:
            self.command_status.record_command(action.value, False)

    def _on_system_requested(self, name: str) -> None:
        """Operator brought a rover subsystem up (LOCALIZATION, etc.)."""
        accepted = self.action_dispatcher.dispatch(name)
        if not accepted:
            self.command_status.record_command(name, False)

    def _on_command_feedback(self, command: str, success: bool) -> None:
        """Renders a command acknowledgement from the comms client."""
        self.command_status.record_command(command, success)
        self.run_logger.record_command(command, success)

    #: Jetson shell states that are not yet a working link but are not failures
    #: either: auto-connect is still working on it.
    JETSON_PENDING_STATES = ("WAITING", "CONNECTING")

    def _render_jetson_status(self, status: str) -> None:
        """Mirrors the terminal panel's status onto the header badge."""
        if status == "CONNECTED":
            badge_class = "Badge BadgeConnected"
        elif status in self.JETSON_PENDING_STATES:
            badge_class = "Badge BadgeWarning"
        else:
            badge_class = "Badge BadgeDisconnected"

        self.jetson_badge.setText(status)
        self.jetson_badge.setProperty("class", badge_class)
        self.jetson_badge.style().unpolish(self.jetson_badge)
        self.jetson_badge.style().polish(self.jetson_badge)

    def _on_clock_running_changed(self, running: bool) -> None:
        """Starts recording on the first START. Pausing does NOT end the run."""
        if running:
            self.run_logger.start()

    def _on_mission_finished(self) -> None:
        """Run duration elapsed: show MISSION COMPLETE and close out the run."""
        self.mission_status.set_complete(True)
        self.state_manager.set_mission_phase(MissionPhase.POST_RUN)
        self.run_logger.record_event("mission clock reached zero")
        folder = self.run_logger.finish("25-minute clock elapsed")
        if folder is not None:
            self.command_status.record_command(f"RUN LOG -> {folder.name}", True)
            logger.info(f"Run log saved to {folder}")
        else:
            # Say so on the panel: a run that produced no log is worth knowing
            # about now, not when someone goes looking for it afterwards.
            self.command_status.record_command("RUN LOG NOT SAVED", False)
            logger.warning("Run finished but no log was written.")

    def _on_mission_reset(self) -> None:
        # Reset means "that was not the run" -- throw the recording away rather
        # than leaving a half-run folder behind to be mistaken for a real one.
        self.run_logger.discard()
        """Returns the clock to full duration and the run to PRE-CHECK."""
        self.mission_clock.reset()
        self.mission_status.set_complete(False)
        self.mission_status.set_remaining(self.mission_clock.remaining_seconds)
        self.state_manager.set_mission_phase(MissionPhase.PRE_CHECK)

    def _on_estop_requested(self, engage: bool) -> None:
        """Operator pressed E-STOP. Nothing is wired behind it yet.

        The attempt is made anyway so the refusal is REAL and visible: the
        comms client has no publishers, so this fails and CONTROL STATUS shows
        it as a failed command. The button is deliberately NOT latched locally
        -- the engaged state shown in the UI only ever reflects what the rover
        reports on /safety/estop, so it can never claim a stop that did not
        happen.
        """
        logger.warning(
            "E-STOP pressed (engage=%s) but Mission Control publishes nothing; "
            "the physical stop on the rover is the one that acts.", engage)
        self.comms_client.send_estop(engage)

    def _apply_estop_presentation(self, engaged: bool) -> None:
        """Reflects an E-Stop the ROVER reports on /safety/estop.

        There is no longer an operator button for this: the stop that counts
        is the physical one on the rover. The UI still reads the state back
        and disables the action buttons while it is latched.
        """
        self.controls.set_estop_engaged(engaged)

    def _on_connection_changed(self, state_str: str) -> None:
        """Updates UI elements when connection state changes.

        The header badge is gone, but this still matters: on a dropped link the
        panels must fall back to NO DATA rather than leaving the last values on
        screen, where they would read as current.
        """
        connected = state_str in (
            ConnectionState.MOCK_MODE.value, ConnectionState.CONNECTED.value)
        if not connected:
            self.telemetry_panel.show_no_data()
            self.command_status.show_no_data()

        # Commands are meaningless without a link, so the buttons say so.
        self.controls.set_controls_enabled(
            state_str in (ConnectionState.CONNECTED.value, ConnectionState.MOCK_MODE.value)
        )

    def _on_telemetry_snapshot(self, state_snapshot) -> None:
        """Renders a rover state snapshot published by the TelemetryBridge."""
        if not isinstance(state_snapshot, RoverState):
            self.telemetry_panel.show_no_data()
            self.command_status.show_no_data()
            self.video_player.set_localized(None)
            return
        # Grey while there is no telemetry at all: an operator must not read a
        # dead link as a confident "not localized".
        self.video_player.set_localized(
            state_snapshot.is_localized() if state_snapshot.has_telemetry else None)
        self.run_logger.record_state(state_snapshot)
        self.telemetry_panel.update_from_state(state_snapshot)
        self.command_status.update_from_state(state_snapshot)
        self.controls.set_estop_engaged(state_snapshot.is_estopped)

    def note_viewer(self, name: str, ok: bool, detail: str) -> None:
        """Reports an external viewer launch in the control status block."""
        self.command_status.record_command(f"VIEWER {name}", ok)
        (logger.info if ok else logger.warning)(f"Viewer {name}: {detail}")

    def closeEvent(self, event) -> None:
        """Closes the Jetson SSH session before the window goes away."""
        self.control_sidebar.shutdown()
        super().closeEvent(event)
