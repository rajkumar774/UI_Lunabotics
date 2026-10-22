"""Background rclpy executor thread.

The Qt UI thread must never block, and rclpy's executor spins forever, so the
ROS 2 node lives entirely inside this QThread. Nothing here touches a widget:
inbound messages are folded into a private RoverState and published outward as
Qt signals, which cross back to the UI thread as queued connections.

rclpy and the generated message packages are imported inside ``run()`` rather
than at module scope, so importing this module on a machine with no ROS 2
installed stays harmless — that is what keeps standalone Windows development
working.

Outbound commands are queued rather than published directly from the UI thread,
so every DDS write happens on the thread that owns the node.
"""

import copy
import os
import threading
import time
from collections import deque
from typing import Any, Deque, Optional, Tuple

from PySide6.QtCore import QThread, Signal

from mission_control.core.config import ROSTopicsConfig
from mission_control.core.logger import get_logger
from mission_control.comms.ros2 import conversions as cv
from mission_control.telemetry.state import (
    ConnectionState,
    DriveData,
    ExcavationData,
    RoverState,
    SensorStatus,
)

logger = get_logger("ros2_worker")

#: Node name this station registers on the ROS graph.
NODE_NAME = "mission_control_station"

#: How often the command queue is drained and the heartbeat watchdog evaluated.
HOUSEKEEPING_HZ = 10.0

#: Executor spin slice. Short enough to stay responsive to interruption.
SPIN_TIMEOUT_S = 0.05


class ROS2WorkerThread(QThread):
    """Owns the rclpy context, node, subscriptions and publishers."""

    telemetry_ready = Signal(object)    # RoverState (independent copy)
    heartbeat_received = Signal(float)  # latency in milliseconds
    link_state_changed = Signal(str)    # ConnectionState value
    command_result = Signal(str, bool)  # (command_name, success)
    error_occurred = Signal(str)
    node_ready = Signal()

    # Control-plane state reported by the rover. Emitted separately from
    # telemetry so the bridge can route it through StateManager's dedicated
    # setters, which a telemetry packet is not allowed to touch.
    mission_mode_reported = Signal(str)   # MissionMode value
    mission_phase_reported = Signal(str)  # MissionPhase value
    estop_reported = Signal(bool)

    def __init__(
        self,
        topics: ROSTopicsConfig,
        publish_rate_hz: int = 20,
        heartbeat_timeout_s: float = 1.0,
        domain_id: Optional[int] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.topics = topics
        self._publish_period = 1.0 / max(1, publish_rate_hz)
        self._heartbeat_timeout_s = max(0.1, heartbeat_timeout_s)
        self._domain_id = domain_id

        # Guards _state and _command_queue, both written from two threads.
        self._lock = threading.Lock()
        self._state = RoverState(connection_state=ConnectionState.CONNECTING)
        # (publisher kind, payload, operator-facing name)
        self._command_queue: Deque[Tuple[str, Any, str]] = deque(maxlen=256)

        self._running = False
        self._last_heartbeat_mono = 0.0
        #: Last time each sensor group produced a MESSAGE, by monotonic clock.
        #: The SENSORS card used to read /diagnostics alone, so a perfectly
        #: healthy IMU publishing at 19 Hz still showed NO DATA simply because
        #: nothing on the rover publishes an IMU DiagnosticStatus. Liveness
        #: fills that gap.
        self._sensor_seen: dict[str, float] = {}
        #: Groups the rover DOES report on /diagnostics. Those win: an explicit
        #: DEGRADED means the rover knows something is wrong, and arriving
        #: messages must not paper over it.
        self._diag_reported: set[str] = set()
        self._link_up = False
        self._node = None
        self._context = None

    # -- Public API (called from the Qt UI thread) ---------------------------

    @property
    def is_link_up(self) -> bool:
        """True while heartbeats are arriving inside the watchdog window."""
        return self._link_up

    def enqueue_command(self, kind: str, payload: Any, feedback_name: str = "") -> None:
        """Queues an outbound command for publication on the ROS thread.

        ``feedback_name`` is the operator-facing label reported back in
        command_result; it defaults to ``kind`` when not given.
        """
        with self._lock:
            self._command_queue.append((kind, payload, feedback_name or kind))

    def shutdown(self) -> None:
        """Signals the spin loop to exit and waits for the thread to finish."""
        self._running = False
        self.requestInterruption()
        if self.isRunning():
            self.wait(3000)

    # -- Thread body ---------------------------------------------------------

    def run(self) -> None:
        """Creates the node, then spins until interrupted."""
        try:
            import rclpy
            from rclpy.executors import SingleThreadedExecutor
        except Exception as exc:  # pragma: no cover - requires a ROS-less host
            self.error_occurred.emit(f"rclpy unavailable: {exc}")
            return

        # ROS_DOMAIN_ID is read when the context initialises, so it has to be in
        # the environment before init -- setting it afterwards has no effect.
        if self._domain_id is not None:
            os.environ["ROS_DOMAIN_ID"] = str(self._domain_id)

        context = None
        node = None
        executor = None
        try:
            # A private context keeps this node from colliding with any other
            # rclpy usage in the same process (notably during tests).
            context = rclpy.Context()
            context.init()
            self._context = context

            node = rclpy.create_node(NODE_NAME, context=context)
            self._node = node
            self._setup_interfaces(node)

            executor = SingleThreadedExecutor(context=context)
            executor.add_node(node)

            self._running = True
            self._last_heartbeat_mono = 0.0
            logger.info(
                f"ROS 2 node '{NODE_NAME}' up on domain "
                f"{os.environ.get('ROS_DOMAIN_ID', '0')}; subscriptions active."
            )
            self.node_ready.emit()

            self._spin_loop(executor)

        except Exception as exc:
            logger.error(f"ROS 2 worker failed: {exc}")
            self.error_occurred.emit(str(exc))
        finally:
            self._running = False
            self._node = None
            self._context = None
            try:
                if executor is not None:
                    executor.shutdown()
                if node is not None:
                    node.destroy_node()
                if context is not None and context.ok():
                    context.try_shutdown()
            except Exception as exc:
                logger.warning(f"ROS 2 worker shutdown warning: {exc}")
            logger.info("ROS 2 worker thread stopped.")

    def _spin_loop(self, executor) -> None:
        """Interleaves executor spins with telemetry publication and housekeeping."""
        next_publish = time.monotonic()
        next_housekeeping = time.monotonic()
        housekeeping_period = 1.0 / HOUSEKEEPING_HZ

        while self._running and not self.isInterruptionRequested():
            try:
                executor.spin_once(timeout_sec=SPIN_TIMEOUT_S)
            except Exception as exc:
                if not self._running:
                    break
                logger.warning(f"ROS 2 spin error: {exc}")
                self.error_occurred.emit(str(exc))
                continue

            now = time.monotonic()
            if now >= next_publish:
                next_publish = now + self._publish_period
                self._publish_telemetry()
            if now >= next_housekeeping:
                next_housekeeping = now + housekeeping_period
                self._drain_commands()
                self._check_heartbeat_watchdog()
                self._check_sensor_liveness()

    # -- Node wiring ---------------------------------------------------------

    def _setup_interfaces(self, node) -> None:
        """Creates every subscription and publisher this station needs.

        Subscriptions are BEST_EFFORT and publishers RELIABLE on purpose: a
        BEST_EFFORT subscriber matches publishers of either reliability, and a
        RELIABLE publisher matches subscribers of either. That combination
        avoids silent QoS-incompatibility, which presents as a topic that lists
        in `ros2 topic list` but never delivers a message.
        """
        from rclpy.qos import qos_profile_sensor_data, QoSProfile, ReliabilityPolicy
        from diagnostic_msgs.msg import DiagnosticArray
        from geometry_msgs.msg import Twist
        from nav_msgs.msg import Odometry
        from geometry_msgs.msg import PoseWithCovarianceStamped
        from sensor_msgs.msg import BatteryState, Imu, JointState
        from std_msgs.msg import Bool, Header, String

        sub_qos = qos_profile_sensor_data
        pub_qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE)

        t = self.topics
        node.create_subscription(Imu, t.imu, self._on_imu, sub_qos)
        node.create_subscription(Odometry, t.odometry, self._on_odometry, sub_qos)
        node.create_subscription(JointState, t.joint_states, self._on_joint_states, sub_qos)
        node.create_subscription(BatteryState, t.battery, self._on_battery, sub_qos)
        node.create_subscription(DiagnosticArray, t.diagnostics, self._on_diagnostics, sub_qos)
        node.create_subscription(Header, t.heartbeat, self._on_heartbeat, sub_qos)
        node.create_subscription(String, t.mission_state, self._on_mission_state, sub_qos)
        node.create_subscription(String, t.autonomy_mode, self._on_autonomy_mode, sub_qos)
        node.create_subscription(Bool, t.estop, self._on_estop_feedback, sub_qos)
        node.create_subscription(
            PoseWithCovarianceStamped, t.tag_pose, self._on_tag_pose, sub_qos)

        # This station is SUBSCRIBE-ONLY. It previously published /cmd_vel,
        # /safety/estop, /mission/cmd and /mission/autonomy_mode; all four were
        # removed deliberately, so nothing the UI does can write to the rover's
        # DDS graph. Commands now leave by other routes (the SSH session for
        # mission actions, the joystick's own joy_node for driving).
        #
        # _publish_command() below refuses every kind rather than silently
        # dropping it: a command that vanishes without a trace is worse than
        # one that is reported as refused.

    # -- Subscription callbacks (ROS thread) ---------------------------------

    def _on_imu(self, msg) -> None:
        now = time.time()
        with self._lock:
            self._state.imu = cv.imu_from_msg(msg, now)
            self._state.last_telemetry_time = now
            self._note_sensor_alive("imu")

    def _on_odometry(self, msg) -> None:
        now = time.time()
        with self._lock:
            self._state.drive = cv.drive_from_odometry(msg, self._state.drive, now)
            self._state.last_telemetry_time = now

    def _on_joint_states(self, msg) -> None:
        now = time.time()
        with self._lock:
            self._note_sensor_alive("encoders")
        left, right = cv.wheel_rpm_from_joint_states(msg)
        excavation = cv.excavation_from_joint_states(msg)
        with self._lock:
            drive = self._state.drive
            self._state.drive = DriveData(
                linear_velocity=drive.linear_velocity,
                angular_velocity=drive.angular_velocity,
                left_wheel_rpm=drive.left_wheel_rpm if left is None else left,
                right_wheel_rpm=drive.right_wheel_rpm if right is None else right,
                motor_currents=list(drive.motor_currents),
                timestamp=now,
            )
            if excavation:
                current = self._state.excavation
                self._state.excavation = ExcavationData(
                    drum_rpm=excavation.get("drum_rpm", current.drum_rpm),
                    drum_current_amps=current.drum_current_amps,
                    arm_extension_pct=excavation.get(
                        "arm_extension_pct", current.arm_extension_pct
                    ),
                    hopper_weight_kg=current.hopper_weight_kg,
                    timestamp=now,
                )
            self._state.last_telemetry_time = now

    def _on_battery(self, msg) -> None:
        now = time.time()
        with self._lock:
            self._state.battery = cv.battery_from_msg(msg, now)
            self._state.last_telemetry_time = now

    def _on_diagnostics(self, msg) -> None:
        now = time.time()
        health = cv.sensor_health_from_diagnostics(msg)
        power = cv.power_from_diagnostics(msg)
        with self._lock:
            self._diag_reported.update(health)
            self._state.diagnostics = cv.diagnostics_from_msg(msg, self._state.diagnostics)
            for field_name, value in power.items():
                setattr(self._state.battery, field_name, value)
            for field_name, status in health.items():
                setattr(self._state.sensors, field_name, status)
            self._state.sensors.timestamp = now
            self._state.last_telemetry_time = now

    def _on_tag_pose(self, msg) -> None:
        """An AprilTag fix arrived, so localization is producing output.

        Only the arrival is recorded, not the pose: the operator indicator
        answers 'is localization working', and the pose itself is consumed
        by the EKF on the rover, not here.
        """
        now = time.time()
        with self._lock:
            self._state.last_tag_pose_time = now
            self._state.last_telemetry_time = now

    def _on_heartbeat(self, msg) -> None:
        now_wall = time.time()
        stamp = getattr(msg, "stamp", None)
        stamp_seconds = 0.0
        if stamp is not None:
            stamp_seconds = cv._f(getattr(stamp, "sec", 0)) + cv._f(
                getattr(stamp, "nanosec", 0)
            ) / 1e9

        latency = cv.heartbeat_latency_ms(stamp_seconds, now_wall)

        self._last_heartbeat_mono = time.monotonic()
        with self._lock:
            self._state.heartbeat_latency_ms = latency
            self._state.last_heartbeat_time = now_wall

        if not self._link_up:
            self._link_up = True
            self.link_state_changed.emit(ConnectionState.CONNECTED.value)
            logger.info("ROS 2 link established (heartbeat received).")

        self.heartbeat_received.emit(latency)

    def _on_mission_state(self, msg) -> None:
        phase = cv.mission_phase_from_string(str(getattr(msg, "data", "")))
        if phase is None:
            return
        with self._lock:
            changed = self._state.mission_phase != phase
            self._state.mission_phase = phase
        if changed:
            self.mission_phase_reported.emit(phase.value)

    def _on_autonomy_mode(self, msg) -> None:
        """The rover's reported control mode, which the UI renders verbatim."""
        mode = cv.mission_mode_from_string(str(getattr(msg, "data", "")))
        if mode is None:
            return
        with self._lock:
            changed = self._state.mission_mode != mode
            self._state.mission_mode = mode
        if changed:
            self.mission_mode_reported.emit(mode.value)

    def _on_estop_feedback(self, msg) -> None:
        """The rover's own E-Stop state is authoritative over the local button."""
        engaged = bool(getattr(msg, "data", False))
        with self._lock:
            changed = self._state.is_estopped != engaged
            self._state.is_estopped = engaged
        if changed:
            self.estop_reported.emit(engaged)

    # -- Periodic work (ROS thread) ------------------------------------------

    def _publish_telemetry(self) -> None:
        """Emits an independent copy of the accumulated state."""
        with self._lock:
            if self._state.last_telemetry_time <= 0.0:
                return
            snapshot = copy.deepcopy(self._state)
        snapshot.connection_state = (
            ConnectionState.CONNECTED if self._link_up else ConnectionState.LOST_LINK
        )
        self.telemetry_ready.emit(snapshot)

    #: A sensor group counts as ONLINE for this long after its last message.
    SENSOR_LIVENESS_TIMEOUT_S = 3.0

    def _note_sensor_alive(self, group: str) -> None:
        """Marks a group ONLINE on message arrival. Caller holds the lock.

        Skipped when the rover publishes a diagnostic for this group: an
        explicit health report beats inferring health from traffic.
        """
        self._sensor_seen[group] = time.monotonic()
        if group not in self._diag_reported:
            setattr(self._state.sensors, group, SensorStatus.ONLINE)

    def _check_sensor_liveness(self) -> None:
        """Drops a group back to OFFLINE once its messages stop.

        Without this a sensor would stay ONLINE forever on the strength of one
        message received minutes ago.
        """
        now = time.monotonic()
        with self._lock:
            for group, seen in self._sensor_seen.items():
                if group in self._diag_reported:
                    continue
                if now - seen > self.SENSOR_LIVENESS_TIMEOUT_S:
                    setattr(self._state.sensors, group, SensorStatus.OFFLINE)

    def _check_heartbeat_watchdog(self) -> None:
        """Declares LOST LINK when heartbeats stop arriving."""
        if not self._link_up or self._last_heartbeat_mono <= 0.0:
            return
        if time.monotonic() - self._last_heartbeat_mono <= self._heartbeat_timeout_s:
            return

        self._link_up = False
        with self._lock:
            self._state.sensors.imu = SensorStatus.UNKNOWN
            self._state.sensors.encoders = SensorStatus.UNKNOWN
            self._state.sensors.lidar = SensorStatus.UNKNOWN
        logger.warning(
            f"ROS 2 heartbeat watchdog expired after {self._heartbeat_timeout_s:.1f}s — LOST LINK."
        )
        self.link_state_changed.emit(ConnectionState.LOST_LINK.value)

    def _drain_commands(self) -> None:
        """Publishes everything the UI thread queued since the last pass."""
        while True:
            with self._lock:
                if not self._command_queue:
                    return
                kind, payload, name = self._command_queue.popleft()
            try:
                self._publish_command(kind, payload)
                self.command_result.emit(name, True)
            except Exception as exc:
                logger.error(f"Failed to publish '{kind}': {exc}")
                self.command_result.emit(name, False)
                self.error_occurred.emit(f"{name}: {exc}")

    def _publish_command(self, kind: str, payload: Any) -> None:
        """Refuses every command: this station no longer publishes to the rover.

        Raising (rather than returning quietly) is deliberate -- the caller
        turns it into a failed command_result, so the operator sees the refusal
        in CONTROL STATUS instead of assuming the rover acted.
        """
        raise RuntimeError(
            f"'{kind}' not sent: Mission Control is subscribe-only and publishes "
            f"no topics to the rover")
