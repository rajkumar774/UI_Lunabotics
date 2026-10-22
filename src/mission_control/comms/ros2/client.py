"""ROS 2 Humble Client Adapter.

Implements BaseCommsClient against a live ROS 2 graph. The rover (Jetson Orin
Nano) and this operator station run ROS 2 on the same ROS_DOMAIN_ID, so topics
are discovered over DDS with no broker and no address configuration beyond the
domain itself.

All rclpy work happens inside ROS2WorkerThread; this class is a thin Qt-facing
adapter that owns that thread and re-emits its signals on the BaseCommsClient
contract. rclpy is imported lazily so the module stays importable with no ROS 2
installed, which is what keeps MockCommsClient viable on a bare laptop.
"""

from typing import Optional

from PySide6.QtCore import QObject

from mission_control.comms.base_client import BaseCommsClient
from mission_control.core.config import ROSTopicsConfig
from mission_control.core.logger import get_logger
from mission_control.telemetry.state import ConnectionState, MissionMode

logger = get_logger("ros2_client")


def is_ros2_available() -> bool:
    """Checks whether rclpy and the message packages can be imported here."""
    try:
        import rclpy  # noqa: F401
        from geometry_msgs.msg import Twist  # noqa: F401
        from sensor_msgs.msg import Imu  # noqa: F401
        from std_msgs.msg import Bool  # noqa: F401
        return True
    except (ImportError, ModuleNotFoundError):
        return False
    except Exception as e:
        logger.debug(f"ROS 2 environment probe returned: {e}")
        return False


class ROS2CommsClient(BaseCommsClient):
    """ROS 2 Humble communications adapter backed by a dedicated QThread."""

    def __init__(
        self,
        topics: Optional[ROSTopicsConfig] = None,
        publish_rate_hz: int = 20,
        heartbeat_timeout_ms: int = 1000,
        domain_id: Optional[int] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.topics = topics or ROSTopicsConfig()
        self._publish_rate_hz = publish_rate_hz
        self._heartbeat_timeout_s = max(0.1, heartbeat_timeout_ms / 1000.0)
        self._domain_id = domain_id
        self._ros_available = is_ros2_available()
        self._worker = None
        self._estop_latched = False

    @property
    def ros_available(self) -> bool:
        """Returns True if rclpy is present in the runtime environment."""
        return self._ros_available

    @property
    def worker(self):
        """The background executor thread, or None before start()."""
        return self._worker

    @property
    def is_estopped(self) -> bool:
        """Last E-Stop state this client commanded."""
        return self._estop_latched

    # -- Lifecycle -----------------------------------------------------------

    def start(self) -> bool:
        """Starts the background rclpy executor thread."""
        if not self._ros_available:
            logger.error(
                "ROS 2 environment (rclpy) is not present. Set comms.backend to "
                "'mock' in config/app_config.yaml for standalone development."
            )
            self.error_occurred.emit("ROS 2 (rclpy) is unavailable on this host.")
            return False

        if self._worker is not None and self._worker.isRunning():
            return True

        # Imported here so a ROS-less host never loads the worker module.
        from mission_control.comms.ros2.worker import ROS2WorkerThread

        self._worker = ROS2WorkerThread(
            topics=self.topics,
            publish_rate_hz=self._publish_rate_hz,
            heartbeat_timeout_s=self._heartbeat_timeout_s,
            domain_id=self._domain_id,
        )
        self._worker.telemetry_ready.connect(self.telemetry_received.emit)
        self._worker.heartbeat_received.connect(self.heartbeat_received.emit)
        self._worker.link_state_changed.connect(self._on_link_state_changed)
        self._worker.command_result.connect(self.command_feedback_received.emit)
        self._worker.error_occurred.connect(self.error_occurred.emit)
        self._worker.mission_mode_reported.connect(self.mission_mode_changed.emit)
        self._worker.mission_phase_reported.connect(self.mission_phase_changed.emit)
        self._worker.estop_reported.connect(self._on_estop_reported)

        self._set_state(ConnectionState.CONNECTING)
        self._worker.start()
        logger.info("ROS2CommsClient started; waiting for rover heartbeat.")
        return True

    def stop(self) -> None:
        """Shuts down the node and background executor."""
        logger.info("Stopping ROS 2 client adapter...")
        if self._worker is not None:
            self._worker.shutdown()
            self._worker = None
        self._is_connected = False
        self._set_state(ConnectionState.DISCONNECTED)

    def connect_rover(self) -> bool:
        """Brings the node up and waits for the rover to announce itself.

        There is no connection handshake in DDS: the link is considered up once
        a heartbeat arrives, so this starts the node and reports CONNECTING.
        """
        if not self._ros_available:
            logger.warning("Cannot connect to ROS 2: rclpy is unavailable on this host.")
            self.error_occurred.emit("ROS 2 (rclpy) is unavailable on this host.")
            return False

        if self._worker is None or not self._worker.isRunning():
            if not self.start():
                return False

        if self._worker.is_link_up:
            self._on_link_state_changed(ConnectionState.CONNECTED.value)
        else:
            self._set_state(ConnectionState.CONNECTING)
        return True

    def disconnect_rover(self) -> None:
        """Tears the node down, which removes this station from the graph."""
        if self._worker is not None:
            self._worker.shutdown()
            self._worker = None
        self._is_connected = False
        self._set_state(ConnectionState.DISCONNECTED)

    # -- Outbound commands ---------------------------------------------------

    def send_drive_command(self, linear_x: float, angular_z: float) -> bool:
        """Publishes geometry_msgs/msg/Twist to the cmd_vel topic."""
        return self._enqueue("cmd_vel", (linear_x, angular_z))

    def send_estop(self, enable: bool) -> bool:
        """Publishes std_msgs/msg/Bool to the E-Stop topic.

        Queued like any other command, but the queue is drained at 10 Hz and
        E-Stop is the only command the UI issues without an operator-held
        control, so worst-case dispatch latency is one housekeeping tick.
        """
        self._estop_latched = bool(enable)
        return self._enqueue("estop", bool(enable))

    def send_mission_mode(self, mode_name: str) -> bool:
        """Publishes a mission mode request on the autonomy mode topic."""
        try:
            MissionMode(mode_name)
        except ValueError:
            logger.error(f"ROS2CommsClient: invalid mission mode '{mode_name}'")
            self.command_feedback_received.emit("mission_mode", False)
            return False
        return self._enqueue("autonomy_mode", mode_name, feedback_name="mission_mode")

    def send_mission_command(self, command: str) -> bool:
        """Publishes a discrete mission action (EXCAVATE / DUMP / CYCLE)."""
        return self._enqueue("mission_cmd", command, feedback_name=command)

    def _enqueue(self, kind: str, payload, feedback_name: Optional[str] = None) -> bool:
        """Queues a command for the ROS thread.

        ``kind`` selects the publisher; ``feedback_name`` is what the operator
        sees in the status block, so a discrete action reports as "DUMP" rather
        than as the transport's "mission_cmd". Both backends therefore
        acknowledge commands by the same names.
        """
        name = feedback_name or kind
        if self._worker is None or not self._worker.isRunning():
            logger.warning(f"ROS2CommsClient: dropped '{name}' — node is not running.")
            self.command_feedback_received.emit(name, False)
            return False
        self._worker.enqueue_command(kind, payload, name)
        return True

    # -- Internals -----------------------------------------------------------

    def _on_estop_reported(self, engaged: bool) -> None:
        """The rover's own E-Stop state wins over what this station commanded."""
        self._estop_latched = bool(engaged)
        self.estop_changed.emit(bool(engaged))

    def _on_link_state_changed(self, state_value: str) -> None:
        try:
            state = ConnectionState(state_value)
        except ValueError:
            return
        self._is_connected = state == ConnectionState.CONNECTED
        self._set_state(state)

    def _set_state(self, state: ConnectionState) -> None:
        if self._connection_state == state:
            return
        self._connection_state = state
        self.connection_state_changed.emit(state.value)
