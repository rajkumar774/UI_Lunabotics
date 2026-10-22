"""Application lifecycle and runtime initialization manager."""

import sys
from typing import Optional
from PySide6.QtWidgets import QApplication

from mission_control.core.config import (
    AppConfig,
    CameraStreamConfig,
    CommsBackend,
    RoverConfig,
    load_config,
)
from mission_control.core.event_bus import EventBus, get_event_bus
from mission_control.core.logger import setup_logger, get_logger
from mission_control.core.telemetry_bridge import TelemetryBridge
from mission_control.comms.base_client import BaseCommsClient
from mission_control.comms.mock_client import MockCommsClient
from mission_control.comms.ros2.client import ROS2CommsClient, is_ros2_available
from mission_control.telemetry.state import StateManager
from mission_control.video.viewer_launcher import ViewerLauncher
from mission_control.video.worker import VideoWorker
from mission_control.video.source import (
    BaseVideoSource,
    TestPatternVideoSource,
    OpenCVVideoSource,
)
from mission_control.ui.main_window import MainWindow
from mission_control.ui.styles import load_stylesheet

logger = get_logger("app")


class MissionControlApp:
    """Encapsulates the complete Mission Control runtime lifecycle."""

    def __init__(self, sys_argv: Optional[list[str]] = None):
        self.argv = sys_argv if sys_argv is not None else sys.argv
        self.app_config: AppConfig
        self.rover_config: RoverConfig
        self.qapp: Optional[QApplication] = None
        self.comms_client: Optional[BaseCommsClient] = None
        self.state_manager: Optional[StateManager] = None
        self.event_bus: Optional[EventBus] = None
        self.telemetry_bridge: Optional[TelemetryBridge] = None
        self.video_worker: Optional[VideoWorker] = None
        self.pip_video_worker: Optional[VideoWorker] = None
        self.viewer_launcher: Optional[ViewerLauncher] = None
        self.main_window: Optional[MainWindow] = None

    def initialize(self) -> bool:
        """Initializes configuration, logging, Qt application, comms, and video pipeline."""
        # 1. Load Configurations
        self.app_config, self.rover_config = load_config()

        # 2. Setup Logging
        setup_logger(level=self.app_config.log_level)
        logger.info(f"Initializing {self.app_config.name} v{self.app_config.version}...")

        # 3. Create QApplication
        self.qapp = QApplication.instance()
        if self.qapp is None:
            self.qapp = QApplication(self.argv)
        self.qapp.setApplicationName(self.app_config.name)

        # 4. Apply Dark Theme Stylesheet
        stylesheet = load_stylesheet()
        if stylesheet:
            self.qapp.setStyleSheet(stylesheet)

        # 5. Initialize Communication Backend
        telemetry_cfg = self.app_config.telemetry
        self.comms_client = self._create_comms_client()
        self.comms_client.start()

        # 5b. Telemetry Pipeline: comms -> StateManager -> EventBus -> UI widgets
        self.event_bus = get_event_bus()
        self.state_manager = StateManager(
            stale_timeout_seconds=telemetry_cfg.stale_timeout_ms / 1000.0,
        )
        self.telemetry_bridge = TelemetryBridge(
            comms_client=self.comms_client,
            state_manager=self.state_manager,
            event_bus=self.event_bus,
            refresh_fps=telemetry_cfg.ui_refresh_fps,
        )

        # 6. Initialize Video Subsystem — two independent ingest pipelines, each
        #    with its own worker thread and bounded buffer so a stall on one
        #    camera cannot hold up the other.
        cameras = self.rover_config.cameras
        self.video_worker = VideoWorker(
            video_source=self._create_video_source(cameras.main),
            buffer_capacity=2,
        )
        self.pip_video_worker = VideoWorker(
            video_source=self._create_video_source(cameras.pip),
            buffer_capacity=2,
        )

        # Operator-side GStreamer viewers, launched alongside the in-UI player
        # from the pipelines in ~/frontcamera.txt and ~/backcamera.txt.
        self.viewer_launcher = ViewerLauncher(
            config=self.rover_config.video_launch,
        )

        # 7. Instantiate Main Window
        self.main_window = MainWindow(
            app_config=self.app_config,
            rover_config=self.rover_config,
            comms_client=self.comms_client,
            event_bus=self.event_bus,
            state_manager=self.state_manager,
        )

        # 7b. Start throttled telemetry publication now that the UI is listening
        self.telemetry_bridge.start()

        # 8. Bind Video Worker Signals to Video Player Widget
        player = self.main_window.video_player
        if player:
            player.set_pip_title(cameras.pip.name)

        if self.video_worker and player:
            self.video_worker.frame_ready.connect(player.handle_frame)
            self.video_worker.stats_updated.connect(player.handle_stats)
            self.video_worker.state_changed.connect(player.handle_state_changed)

        if self.pip_video_worker and player:
            self.pip_video_worker.frame_ready.connect(player.handle_pip_frame)
            self.pip_video_worker.state_changed.connect(player.handle_pip_state_changed)

        if player:
            # Each camera starts and stops on its own, so a dead back camera
            # never costs the operator the main view.
            player.stream_start_one.connect(self._start_stream)
            player.stream_stop_one.connect(self._stop_stream)

        if self.viewer_launcher:
            self.viewer_launcher.viewer_started.connect(self._on_viewer_started)

        return True

    #: Which external viewer each stream id owns. The index matches the order
    #: of `video_launch.viewers` in rover_config.yaml.
    _STREAM_VIEWER_INDEX = {"main": 0, "pip": 1}

    def _worker_for(self, stream_id: str) -> Optional[VideoWorker]:
        return self.pip_video_worker if stream_id == "pip" else self.video_worker

    def _viewer_name(self, stream_id: str) -> Optional[str]:
        viewers = self.rover_config.video_launch.viewers
        index = self._STREAM_VIEWER_INDEX.get(stream_id)
        if index is None or index >= len(viewers):
            return None
        return viewers[index].name

    def _start_stream(self, stream_id: str) -> None:
        """Starts one camera: its in-UI worker and its external viewer."""
        worker = self._worker_for(stream_id)
        if worker is not None:
            worker.start_stream()

        name = self._viewer_name(stream_id)
        if self.viewer_launcher and name and not self.viewer_launcher.start_one(name):
            logger.warning(
                f"Viewer '{name}' did not start — check its command file "
                "named in rover_config.yaml."
            )

    def _stop_stream(self, stream_id: str) -> None:
        """Stops one camera without touching the other."""
        worker = self._worker_for(stream_id)
        if worker is not None:
            worker.stop_stream()

        name = self._viewer_name(stream_id)
        if (
            self.viewer_launcher
            and name
            and self.rover_config.video_launch.stop_on_stream_stop
        ):
            self.viewer_launcher.stop_one(name)

    def _on_viewer_started(self, name: str, ok: bool, detail: str) -> None:
        if self.main_window is not None:
            self.main_window.note_viewer(name, ok, detail)

    # -- Construction helpers ------------------------------------------------

    def _create_comms_client(self) -> BaseCommsClient:
        """Chooses the comms backend from configuration and host capability.

        comms.backend wins when it names a backend outright; 'auto' keeps the
        older app.mock_mode switch working, and falls back to the mock client
        with a warning rather than starting a ROS 2 client that cannot run.
        """
        telemetry_cfg = self.app_config.telemetry
        backend = self.app_config.comms.backend
        ros_available = is_ros2_available()

        if backend == CommsBackend.AUTO:
            use_ros2 = ros_available and not self.app_config.mock_mode
        elif backend == CommsBackend.ROS2:
            use_ros2 = True
            if not ros_available:
                logger.error(
                    "comms.backend is 'ros2' but rclpy is unavailable on this host. "
                    "Falling back to MOCK MODE — source your ROS 2 setup.bash and restart."
                )
                use_ros2 = False
        else:
            use_ros2 = False

        if not use_ros2:
            reason = "configured" if backend == CommsBackend.MOCK else "no ROS 2 link selected"
            logger.info(f"Operating in MOCK MODE ({reason}) — telemetry is NOT real.")
            return MockCommsClient(
                telemetry_interval_ms=int(1000 / max(1, telemetry_cfg.mock_rate_hz)),
            )

        logger.info(
            f"Initializing ROS2CommsClient on ROS_DOMAIN_ID={self.rover_config.ros_domain_id} "
            f"({self.rover_config.ros_distro})."
        )
        return ROS2CommsClient(
            topics=self.rover_config.topics,
            publish_rate_hz=telemetry_cfg.ros_publish_rate_hz,
            heartbeat_timeout_ms=self.app_config.mission.heartbeat_timeout_ms,
            domain_id=self.rover_config.ros_domain_id,
        )

    def _create_video_source(self, camera: CameraStreamConfig) -> BaseVideoSource:
        """Builds an ingest source, falling back to the test pattern with no URI."""
        if camera.is_test_pattern:
            logger.info(f"Camera '{camera.name}': no URI configured — using test pattern.")
            return TestPatternVideoSource(
                width=camera.resolution.width,
                height=camera.resolution.height,
                target_fps=camera.target_fps,
                label=f"{camera.name} [SYNTHETIC TEST PATTERN]",
            )

        logger.info(f"Camera '{camera.name}': ingesting {camera.uri}")
        return OpenCVVideoSource(
            source_uri=camera.uri,
            rtsp_transport=camera.rtsp_transport,
        )

    def run(self) -> int:
        """Launches the Qt event loop."""
        if self.main_window is None:
            if not self.initialize():
                return 1
        
        self.main_window.show()
        logger.info("Mission Control Window displayed. Starting Qt event loop.")
        exit_code = self.qapp.exec()
        
        self.cleanup()
        return exit_code

    def cleanup(self) -> None:
        """Performs graceful shutdown of background comms, video workers, and resources."""
        logger.info("Shutting down Mission Control application...")
        for worker in (self.video_worker, self.pip_video_worker):
            if worker:
                worker.stop_stream()
        if self.viewer_launcher:
            self.viewer_launcher.stop()
        if self.main_window is not None:
            self.main_window.control_sidebar.shutdown()
        if self.telemetry_bridge:
            self.telemetry_bridge.stop()
        if self.comms_client:
            self.comms_client.stop()
