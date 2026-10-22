"""Operator action surface: discrete mission actions and the emergency stop.

The widget owns no rover state. It emits requests; MainWindow forwards them to
the comms client, and whatever the rover reports back is rendered by
CommandStatusWidget. Nothing here assumes a command succeeded because a button
was pressed.

Actions are one-shot dispatches (EXCAVATE, DUMP, ONE CYCLE AUTO). E-Stop is not
an action: it is a latching safety override, so it sits apart with its own
styling and locks out the actions while engaged.
"""

from enum import Enum
from typing import Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class MissionAction(str, Enum):
    """Discrete one-shot actions dispatched to the rover."""
    EXCAVATE = "EXCAVATE"
    DUMP = "DUMP"
    ONE_CYCLE_AUTO = "ONE CYCLE AUTO"


class AutonomyControlWidget(QFrame):
    """Mission action buttons and the emergency stop."""

    action_requested = Signal(object)
    estop_requested = Signal(bool)       # True = engage    # MissionAction
    system_requested = Signal(str)       # subsystem binding name

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ControlCard")
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

        self._estop_engaged = False
        self.action_buttons: Dict[MissionAction, QPushButton] = {}
        #: Subsystem bring-up buttons, built from the config bindings.
        self.system_buttons: Dict[str, QPushButton] = {}

        self._build_ui()

    # -- Construction --------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        # Subsystem bring-up sits above the mission actions because that is
        # the order it happens in: bring the stack up during setup, then run
        # tasks against it during the run.
        self.systems_caption = QLabel("SYSTEMS")
        self.systems_caption.setObjectName("TelemetryCardTitle")
        self.systems_caption.hide()
        layout.addWidget(self.systems_caption)

        self.systems_grid = QGridLayout()
        self.systems_grid.setContentsMargins(0, 0, 0, 0)
        self.systems_grid.setSpacing(5)
        layout.addLayout(self.systems_grid)

        self.actions_caption = QLabel("ACTIONS")
        self.actions_caption.setObjectName("TelemetryCardTitle")
        layout.addWidget(self.actions_caption)
        layout.addLayout(self._build_action_grid())

        layout.addSpacing(4)
        self.estop_btn = QPushButton("EMERGENCY STOP")
        self.estop_btn.setObjectName("EStopButton")
        self.estop_btn.setMinimumHeight(44)
        self.estop_btn.clicked.connect(self._on_estop_clicked)
        layout.addWidget(self.estop_btn)

        # The button is back, but nothing is wired behind it yet: this station
        # publishes no topics at all, so a press cannot reach the rover. The
        # caption says so, and a press is reported as FAILED in CONTROL STATUS
        # rather than silently doing nothing -- an operator must never believe
        # a stop was commanded when it was not.
        self.estop_caption = QLabel("NOT WIRED \u2014 does not reach the rover yet")
        self.estop_caption.setObjectName("TelemetryCardTitle")
        self.estop_caption.setProperty("severity", "WARNING")
        self.estop_caption.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.estop_caption)

    def _build_action_grid(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(5)

        placements = {
            MissionAction.EXCAVATE: (0, 0, 1, 1),
            MissionAction.DUMP: (0, 1, 1, 1),
            MissionAction.ONE_CYCLE_AUTO: (1, 0, 1, 2),
        }
        for action, (row, col, row_span, col_span) in placements.items():
            button = QPushButton(action.value)
            button.setObjectName("ActionButton")
            button.setMinimumHeight(34)
            button.clicked.connect(lambda _checked=False, a=action: self.action_requested.emit(a))
            grid.addWidget(button, row, col, row_span, col_span)
            self.action_buttons[action] = button

        return grid

    # -- State rendering (driven by MainWindow, never self-set) --------------

    def add_system_buttons(self, entries) -> None:
        """Builds the SYSTEMS row from the config bindings marked is_system.

        ``entries`` is a sequence of (name, label, is_placeholder). A
        placeholder is styled distinctly and suffixed, because a button that
        does nothing must never look like one that brings a subsystem up --
        that is the difference between "the camera is running" and "I pressed
        a button and assumed it was".

        Built from config rather than hard-coded, so adding LIDAR, SLAM or
        PERCEPTION later is a YAML edit and not a UI change.
        """
        for index, (name, label, is_placeholder) in enumerate(entries):
            button = QPushButton(f"{label}  ·  TODO" if is_placeholder else label)
            button.setObjectName("SystemButton")
            button.setMinimumHeight(30)
            button.setProperty("placeholder", bool(is_placeholder))
            if is_placeholder:
                button.setToolTip("No command configured yet — see mission_actions in rover_config.yaml")
            button.clicked.connect(
                lambda _checked=False, n=name: self.system_requested.emit(n)
            )
            self.systems_grid.addWidget(button, index // 2, index % 2)
            self.system_buttons[name] = button
        if self.system_buttons:
            self.systems_caption.show()

    def set_mock_mode(self, is_mock: bool) -> None:
        """Marks the action block when no button commands the rover yet.

        The buttons stay live on purpose -- the operator needs to rehearse the
        real sequence. But a mock acknowledgement must never be mistakable for
        the rover having acted, so the state is stated on the panel as well as
        in the status line.
        """
        self.actions_caption.setText("ACTIONS  ·  MOCK — NO ROVER COMMAND" if is_mock else "ACTIONS")
        self.actions_caption.setProperty("severity", "WARNING" if is_mock else None)
        self.actions_caption.style().unpolish(self.actions_caption)
        self.actions_caption.style().polish(self.actions_caption)

    def set_estop_engaged(self, engaged: bool) -> None:
        """Still tracked (the rover reports it on /safety/estop) but no longer
        has a button -- it only gates the action buttons now."""
        """Renders the latched E-Stop state reported by the rover."""
        self._estop_engaged = bool(engaged)
        self.estop_btn.setText("RELEASE E-STOP" if engaged else "EMERGENCY STOP")
        self.estop_btn.setProperty("engaged", self._estop_engaged)
        self.estop_btn.style().unpolish(self.estop_btn)
        self.estop_btn.style().polish(self.estop_btn)

        # Nothing may be commanded while the stop is latched.
        for button in self._commandable_buttons():
            button.setEnabled(not self._estop_engaged)

    def set_controls_enabled(self, enabled: bool) -> None:
        """Greys out action buttons when there is no link.

        E-Stop stays live on purpose: an operator hitting it during a dropout
        should still queue the command rather than find a dead button.
        """
        allowed = enabled and not self._estop_engaged
        for button in self._commandable_buttons():
            button.setEnabled(allowed)

    def _commandable_buttons(self):
        return list(self.action_buttons.values()) + list(self.system_buttons.values())

    @property
    def estop_engaged(self) -> bool:
        return self._estop_engaged

    def _on_estop_clicked(self) -> None:
        self.estop_requested.emit(not self._estop_engaged)

