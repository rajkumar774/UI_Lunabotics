"""Right-hand operator column: controls, status, and the Jetson terminal.

Stacked top to bottom in the order an operator works down them: pick a mode,
dispatch an action, read what the rover came back with, and drop into a shell
when something needs to be started or inspected on the Jetson directly.

The terminal takes the remaining vertical space because it is the only element
whose useful size is unbounded; the control and status blocks size to content.
"""

from typing import Optional

from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from mission_control.core.config import JetsonSSHConfig
from mission_control.ui.components.autonomy_control_widget import AutonomyControlWidget
from mission_control.ui.components.command_status_widget import CommandStatusWidget
from mission_control.ui.components.jetson_terminal_widget import JetsonTerminalWidget


class ControlSidebar(QWidget):
    """Assembles the operator control column."""

    def __init__(
        self,
        ssh_config: Optional[JetsonSSHConfig] = None,
        ssh_host: str = "",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setObjectName("ControlSidebar")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.controls = AutonomyControlWidget(self)
        layout.addWidget(self.controls, stretch=0)

        self.status = CommandStatusWidget(self)
        layout.addWidget(self.status, stretch=0)

        self.terminal = JetsonTerminalWidget(
            config=ssh_config, host=ssh_host, parent=self
        )
        layout.addWidget(self.terminal, stretch=1)

    def shutdown(self) -> None:
        """Closes the SSH session during application teardown."""
        self.terminal.shutdown()
