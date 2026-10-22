"""Dispatch of operator action buttons to the rover.

===========================================================================
 THIS IS THE ONE FILE TO EDIT WHEN THE JETSON SIDE IS READY.
 The buttons, the UI wiring and the status display are all finished and do
 not need to change. Only the four ``_dispatch_*`` methods below do.
===========================================================================

Each button looks up a binding in ``rover_config.yaml`` under
``mission_actions:`` and this dispatcher executes whichever mode it names:

    mode: mock      -> _dispatch_mock      (acknowledges, commands nothing)
    mode: ssh       -> _dispatch_ssh       (WORKS NOW: runs a launch file)
    mode: topic     -> _dispatch_topic     (works now: std_msgs/String)
    mode: service   -> _dispatch_service   (TODO: needs LaunchSystem.srv)
    mode: action    -> _dispatch_action    (TODO: needs the action servers)

So the migration is: get a launch file working on the Jetson, flip that one
entry from ``mock`` to ``topic``/``service``/``action`` in the YAML, done.

Every path reports back through ``command_feedback_received`` so the CONTROL
STATUS block shows what happened — including, deliberately and visibly, that
a mock did nothing.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from PySide6.QtCore import QObject, Signal

from mission_control.comms.base_client import BaseCommsClient
from mission_control.core.config import ActionMode, MissionActionBinding
from mission_control.core.logger import get_logger

logger = get_logger("mission_actions")


class MissionActionDispatcher(QObject):
    """Routes an operator action to whatever its config binding says."""

    #: (action_name, accepted, human-readable detail)
    dispatched = Signal(str, bool, str)

    def __init__(
        self,
        comms_client: BaseCommsClient,
        bindings: Optional[Dict[str, MissionActionBinding]] = None,
        ssh_runner: Optional[Callable[[str], bool]] = None,
        parent: Optional[QObject] = None,
    ):
        """``ssh_runner`` runs a shell command on the Jetson and reports whether
        it was dispatched -- normally JetsonTerminalWidget.run_command, which
        already opens a fresh shell when the current one is busy."""
        super().__init__(parent)
        self.comms_client = comms_client
        self.bindings: Dict[str, MissionActionBinding] = bindings or {}
        self.ssh_runner = ssh_runner

    def binding_for(self, action_name: str) -> MissionActionBinding:
        """The binding for an action, defaulting to a mock for unknown names.

        Defaulting to mock matters: a typo in the YAML must never fall through
        to something that actually drives the rover.
        """
        existing = self.bindings.get(action_name)
        if existing is not None:
            return existing
        return MissionActionBinding(name=action_name, payload=action_name)

    def is_mock(self, action_name: str) -> bool:
        return self.binding_for(action_name).is_mock

    @property
    def any_real_bindings(self) -> bool:
        """True once at least one action does something on the rover."""
        return any(not b.is_mock for b in self.bindings.values())

    # -- Entry point ---------------------------------------------------------

    def dispatch(self, action_name: str) -> bool:
        """Executes an operator action. Returns False if it was not accepted."""
        binding = self.binding_for(action_name)

        handlers = {
            ActionMode.MOCK: self._dispatch_mock,
            ActionMode.SSH: self._dispatch_ssh,
            ActionMode.TOPIC: self._dispatch_topic,
            ActionMode.SERVICE: self._dispatch_service,
            ActionMode.ACTION: self._dispatch_action,
        }
        accepted, detail = handlers[binding.mode](binding)

        level = logger.info if accepted else logger.warning
        level(f"Action '{action_name}' [{binding.mode.value}] -> {detail}")
        self.dispatched.emit(action_name, accepted, detail)
        return accepted

    # -- Modes ---------------------------------------------------------------

    def _dispatch_mock(self, binding: MissionActionBinding) -> tuple[bool, str]:
        """Current behaviour: acknowledge, command nothing.

        Reported as MOCK rather than OK on purpose. An operator must never be
        able to mistake a mock acknowledgement for the rover having acted.
        """
        self.comms_client.command_feedback_received.emit(f"{binding.name} (MOCK)", True)
        return True, "acknowledged locally — NOTHING was commanded on the rover"

    def _dispatch_ssh(self, binding: MissionActionBinding) -> tuple[bool, str]:
        """Runs the binding's shell command on the Jetson over SSH.

        This is the bridge until LaunchSystem.srv exists: a launch file IS a
        shell command, and the terminal is already a live session. It also
        gives the operator the launch's own output, which a service call would
        hide. The terminal opens a fresh shell if the current one is busy, so
        a long-running launch never blocks the next command.

        The honest limitation: there is no structured result. We know the
        command was typed, not that the nodes came up. Watch the tab, or move
        to `mode: service` once the Jetson side is ready.
        """
        if not binding.command:
            return False, "ssh mode selected but no `command` set in rover_config.yaml"
        if self.ssh_runner is None:
            return False, "no Jetson terminal available to run the command"

        started = self.ssh_runner(binding.command)
        self.comms_client.command_feedback_received.emit(binding.name, started)
        return started, (
            f"running `{binding.command}` in the Jetson terminal"
            if started else "Jetson terminal is not connected"
        )

    def _dispatch_topic(self, binding: MissionActionBinding) -> tuple[bool, str]:
        """Publishes the payload as std_msgs/String. Works today."""
        accepted = self.comms_client.send_mission_command(binding.payload)
        return accepted, (
            f"published '{binding.payload}' on {binding.topic}"
            if accepted else f"publish to {binding.topic} refused (no link?)"
        )

    def _dispatch_service(self, binding: MissionActionBinding) -> tuple[bool, str]:
        """Call a LaunchSystem service on the Jetson.

        >>> PUT YOUR REAL COMMAND HERE (service path) <<<

        When ``lunabot_msgs/srv/LaunchSystem`` exists on the Jetson, implement
        this as a non-blocking client call on the ROS 2 worker thread:

            from lunabot_msgs.srv import LaunchSystem
            request = LaunchSystem.Request()
            request.system_name = binding.payload   # e.g. "autonomy"
            request.use_sim = False
            future = client.call_async(request)     # never call() — it blocks

        Do NOT call the service from this (Qt) thread: a service call blocks
        until the Jetson answers, which would freeze the UI if the link is
        slow or the server is gone. Hand it to ROS2WorkerThread and report the
        result back via a Qt signal, the same way commands already work.
        """
        return False, (
            f"service mode not implemented yet — would call {binding.service} "
            f"with system_name='{binding.payload}'"
        )

    def _dispatch_action(self, binding: MissionActionBinding) -> tuple[bool, str]:
        """Send a goal to an autonomy action server.

        >>> PUT YOUR REAL COMMAND HERE (action path) <<<

        When the action servers exist, implement on the ROS 2 worker thread:

            from rclpy.action import ActionClient
            from lunabot_msgs.action import Excavation
            client = ActionClient(node, Excavation, binding.action)
            goal_future = client.send_goal_async(
                Excavation.Goal(),
                feedback_callback=on_feedback,       # -> CONTROL STATUS line
            )

        Three things this path must do that the others need not:
          1. keep the goal handle, so CANCEL works — guidebook 6.2 requires
             aborting an attempt and announcing failure before resuming manual;
          2. surface feedback live, so the operator can announce completion;
          3. distinguish SUCCEEDED / ABORTED / CANCELED in the status block.
        """
        return False, (
            f"action mode not implemented yet — would send a goal to {binding.action}"
        )
