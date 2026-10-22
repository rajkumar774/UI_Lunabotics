"""Configuration loading and data models for application and rover settings."""

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml

from mission_control.core.logger import get_logger

logger = get_logger("config")


@dataclass
class UIConfig:
    window_title: str = "Rover Mission Control"
    default_width: int = 1440
    default_height: int = 900
    min_width: int = 1100
    min_height: int = 700
    theme: str = "dark"
    fps_limit: int = 30


@dataclass
class MissionConfig:
    competition_name: str = "Lunabotics Competition"
    run_duration_seconds: int = 1500  # 25 minutes (Guidebook v3, section 1.3)
    teleoperation_timeout_ms: int = 500
    heartbeat_timeout_ms: int = 1000
    #:每 run gets its own folder under here, named run_<start timestamp>.
    run_log_dir: str = "logs/runs"


@dataclass
class TelemetryConfig:
    """Telemetry pipeline rates.

    mock_rate_hz drives the mock publish rate and ros_publish_rate_hz the rate at
    which the ROS 2 client folds inbound topics into a packet; ui_refresh_fps caps
    how often the telemetry widgets repaint, independently of either.
    """
    mock_rate_hz: int = 20
    ros_publish_rate_hz: int = 20
    ui_refresh_fps: int = 30
    stale_timeout_ms: int = 2000


class CommsBackend(str, Enum):
    """Which communication client the application should construct."""
    AUTO = "auto"
    MOCK = "mock"
    ROS2 = "ros2"


@dataclass
class CommsConfig:
    backend: CommsBackend = CommsBackend.AUTO


@dataclass
class AppConfig:
    name: str = "Rover Mission Control"
    version: str = "0.1.0"
    mock_mode: bool = True
    log_level: str = "INFO"
    ui: UIConfig = field(default_factory=UIConfig)
    mission: MissionConfig = field(default_factory=MissionConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    comms: CommsConfig = field(default_factory=CommsConfig)


@dataclass
class ROSTopicsConfig:
    cmd_vel: str = "/cmd_vel"
    heartbeat: str = "/rover/heartbeat"
    imu: str = "/imu/data"
    odometry: str = "/odom"
    joint_states: str = "/joint_states"
    battery: str = "/battery_state"
    #: Livox MID-360 pointcloud (sensor_msgs/PointCloud2 or livox CustomMsg,
    #: depending on the driver's xfer_format).
    lidar: str = "/livox/lidar"
    #: The MID-360 has a built-in IMU, published separately by the driver.
    #: This is distinct from the standalone witmotion IMU on `imu`.
    lidar_imu: str = "/livox/imu"
    camera_image: str = "/camera/image_raw"
    camera_compressed: str = "/camera/image_raw/compressed"
    mission_state: str = "/mission/state"
    mission_cmd: str = "/mission/cmd"
    autonomy_mode: str = "/mission/autonomy_mode"
    diagnostics: str = "/diagnostics"
    estop: str = "/safety/estop"
    joy: str = "/joy"
    teleop_status: str = "/rover/teleop_status"
    #: geometry_msgs/PoseWithCovarianceStamped from rover_localization's
    #: tag_pose_node, published whenever an AprilTag fix is computed. Its
    #: arrival is what the LOC indicator in the stream footer reports.
    tag_pose: str = "/tag/rover_pose"


@dataclass
class ResolutionConfig:
    width: int = 1280
    height: int = 720


@dataclass
class CameraSensorConfig:
    mount: str = "front"
    aspect_ratio: str = "16:9"
    target_fps: int = 30
    resolution: ResolutionConfig = field(default_factory=ResolutionConfig)


@dataclass
class SensorsConfig:
    #: One Livox MID-360, not four perimeter scanners.
    lidar_count: int = 1
    lidar_model: str = "Livox MID-360"
    camera: CameraSensorConfig = field(default_factory=CameraSensorConfig)


@dataclass
class CameraStreamConfig:
    """One ingest stream. An empty ``uri`` falls back to the test pattern, so the
    player stays usable with no rover attached."""
    name: str = "CAMERA"
    uri: str = ""
    rtsp_transport: str = "udp"
    target_fps: int = 30
    resolution: ResolutionConfig = field(default_factory=ResolutionConfig)
    # Picture-in-picture placement; ignored for the main stream.
    corner: str = "top_right"
    width_fraction: float = 0.26

    @property
    def is_test_pattern(self) -> bool:
        """True when no real URI is configured and the synthetic source is used."""
        return not self.uri.strip()


@dataclass
class VideoViewer:
    """One operator-side viewer pipeline, read from a file at press time."""
    name: str = ""
    command_file: str = ""

    @property
    def resolved_path(self) -> str:
        return os.path.expanduser(self.command_file.strip())

    def read_command(self) -> str:
        """Returns the pipeline, or '' if the file is missing or empty.

        Read on every press rather than cached, so the operator can edit the
        file between attempts without restarting Mission Control.
        """
        path = self.resolved_path
        if not path or not os.path.isfile(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = [
                    line.strip() for line in handle
                    if line.strip() and not line.strip().startswith("#")
                ]
            return lines[0] if lines else ""
        except OSError as exc:
            logger.error(f"Could not read viewer command {path}: {exc}")
            return ""


@dataclass
class VideoLaunchConfig:
    enabled: bool = False
    stop_on_stream_stop: bool = True
    viewers: List[VideoViewer] = field(default_factory=list)


@dataclass
class CamerasConfig:
    main: CameraStreamConfig = field(
        default_factory=lambda: CameraStreamConfig(name="D456 DEPTH CAMERA")
    )
    pip: CameraStreamConfig = field(
        default_factory=lambda: CameraStreamConfig(
            name="FRONT CAM",
            target_fps=15,
            resolution=ResolutionConfig(width=640, height=360),
        )
    )


class ActionMode(str, Enum):
    """How an operator action button reaches the rover."""
    MOCK = "mock"        # acknowledge locally, command nothing
    SSH = "ssh"          # run a shell command in the Jetson terminal
    TOPIC = "topic"      # publish payload as std_msgs/String
    SERVICE = "service"  # call LaunchSystem
    ACTION = "action"    # send an action goal


@dataclass
class MissionActionBinding:
    """What one operator button actually does.

    Lives in config so the real command can be filled in without touching UI
    code: see the `mission_actions` block in rover_config.yaml.
    """
    name: str = ""
    mode: ActionMode = ActionMode.MOCK
    #: Shell command for mode: ssh. Works today with no Jetson-side changes,
    #: because the terminal panel is already a live SSH session.
    command: str = ""
    topic: str = "/mission/cmd"
    payload: str = ""
    service: str = ""
    action: str = ""
    #: Shown on the button; defaults to the binding name.
    label: str = ""
    #: True for subsystem bring-up (LOCALIZATION), False for mission tasks
    #: (EXCAVATE). Bring-up happens once during setup; tasks run repeatedly.
    is_system: bool = False

    @property
    def is_mock(self) -> bool:
        return self.mode is ActionMode.MOCK


@dataclass
class QuickCommand:
    """A one-click shell command offered above the Jetson terminal prompt."""
    label: str
    command: str


@dataclass
class JetsonSSHConfig:
    """SSH session for the in-UI Jetson terminal.

    This carries shell commands only. Telemetry and control topics travel over
    DDS between the two ROS 2 hosts and never pass through this session.
    """
    enabled: bool = True
    host: str = ""
    port: int = 22
    username: str = "jetson"
    key_path: str = "~/.ssh/id_rsa"
    startup_commands: List[str] = field(default_factory=list)
    connect_timeout_s: float = 8.0
    keepalive_s: int = 15
    quick_commands: List[QuickCommand] = field(default_factory=list)
    #: Watch for the Jetson coming online and attach without operator action.
    auto_connect: bool = True
    #: Seconds between reachability probes / reconnect attempts.
    reconnect_interval_s: int = 5
    #: Clear each new shell after its startup commands have run.
    clear_on_open: bool = True


@dataclass
class RoverConfig:
    name: str = "Lunabotics Rover"
    computer: str = "NVIDIA Jetson Orin Nano"
    os: str = "Ubuntu 22.04 LTS"
    ros_distro: str = "Humble"
    ros_domain_id: int = 0
    rover_ip: str = "192.168.1.100"
    mission_control_ip: str = "192.168.1.50"
    topics: ROSTopicsConfig = field(default_factory=ROSTopicsConfig)
    sensors: SensorsConfig = field(default_factory=SensorsConfig)
    cameras: CamerasConfig = field(default_factory=CamerasConfig)
    video_launch: VideoLaunchConfig = field(default_factory=VideoLaunchConfig)
    jetson_ssh: JetsonSSHConfig = field(default_factory=JetsonSSHConfig)
    mission_actions: Dict[str, MissionActionBinding] = field(default_factory=dict)

    @property
    def ssh_host(self) -> str:
        """Jetson SSH target, falling back to the rover's network address."""
        return self.jetson_ssh.host.strip() or self.rover_ip


def load_yaml_file(path: Path) -> Dict[str, Any]:
    """Safely loads a YAML file from disk, returning an empty dict on failure."""
    if not path.exists():
        logger.warning(f"Configuration file not found: {path}")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = yaml.safe_load(f)
            return content if isinstance(content, dict) else {}
    except Exception as e:
        logger.error(f"Failed to parse configuration file {path}: {e}")
        return {}


def load_app_config(config_dir: Optional[Path] = None) -> AppConfig:
    """Loads AppConfig from YAML or returns defaults."""
    if config_dir is None:
        # Default config directory relative to repo root
        config_dir = Path(__file__).resolve().parents[3] / "config"

    app_path = config_dir / "app_config.yaml"
    data = load_yaml_file(app_path)

    app_section = data.get("app", {})
    ui_section = data.get("ui", {})
    mission_section = data.get("mission", {})
    telemetry_section = data.get("telemetry", {})
    comms_section = data.get("comms", {})

    ui_config = UIConfig(
        window_title=ui_section.get("window_title", "Rover Mission Control"),
        default_width=ui_section.get("default_width", 1440),
        default_height=ui_section.get("default_height", 900),
        min_width=ui_section.get("min_width", 1100),
        min_height=ui_section.get("min_height", 700),
        theme=ui_section.get("theme", "dark"),
        fps_limit=ui_section.get("fps_limit", 30),
    )

    mission_config = MissionConfig(
        competition_name=mission_section.get("competition_name", "Lunabotics Competition"),
        run_duration_seconds=mission_section.get("run_duration_seconds", 1500),
        teleoperation_timeout_ms=mission_section.get("teleoperation_timeout_ms", 500),
        heartbeat_timeout_ms=mission_section.get("heartbeat_timeout_ms", 1000),
    )

    telemetry_config = TelemetryConfig(
        mock_rate_hz=telemetry_section.get("mock_rate_hz", 20),
        ros_publish_rate_hz=telemetry_section.get("ros_publish_rate_hz", 20),
        # Falls back to the shared UI frame cap when not explicitly configured.
        ui_refresh_fps=telemetry_section.get("ui_refresh_fps", ui_config.fps_limit),
        stale_timeout_ms=telemetry_section.get("stale_timeout_ms", 2000),
    )

    backend_raw = str(comms_section.get("backend", "auto")).strip().lower()
    try:
        backend = CommsBackend(backend_raw)
    except ValueError:
        logger.warning(
            f"Unknown comms.backend '{backend_raw}'; falling back to 'auto'."
        )
        backend = CommsBackend.AUTO

    return AppConfig(
        name=app_section.get("name", "Rover Mission Control"),
        version=app_section.get("version", "0.1.0"),
        mock_mode=app_section.get("mock_mode", True),
        log_level=app_section.get("log_level", "INFO"),
        ui=ui_config,
        mission=mission_config,
        telemetry=telemetry_config,
        comms=CommsConfig(backend=backend),
    )


def load_rover_config(config_dir: Optional[Path] = None) -> RoverConfig:
    """Loads RoverConfig from YAML or returns defaults."""
    if config_dir is None:
        config_dir = Path(__file__).resolve().parents[3] / "config"

    rover_path = config_dir / "rover_config.yaml"
    data = load_yaml_file(rover_path)

    rover_section = data.get("rover", {})
    network_section = data.get("network", {})
    topics_section = data.get("ros_topics", {})

    topics_config = ROSTopicsConfig(
        cmd_vel=topics_section.get("cmd_vel", "/cmd_vel"),
        heartbeat=topics_section.get("heartbeat", "/rover/heartbeat"),
        imu=topics_section.get("imu", "/imu/data"),
        odometry=topics_section.get("odometry", "/odom"),
        joint_states=topics_section.get("joint_states", "/joint_states"),
        battery=topics_section.get("battery", "/battery_state"),
        lidar=topics_section.get("lidar", "/livox/lidar"),
        lidar_imu=topics_section.get("lidar_imu", "/livox/imu"),
        camera_image=topics_section.get("camera_image", "/camera/image_raw"),
        camera_compressed=topics_section.get("camera_compressed", "/camera/image_raw/compressed"),
        mission_state=topics_section.get("mission_state", "/mission/state"),
        mission_cmd=topics_section.get("mission_cmd", "/mission/cmd"),
        autonomy_mode=topics_section.get("autonomy_mode", "/mission/autonomy_mode"),
        diagnostics=topics_section.get("diagnostics", "/diagnostics"),
        estop=topics_section.get("estop", "/safety/estop"),
        joy=topics_section.get("joy", "/joy"),
        teleop_status=topics_section.get("teleop_status", "/rover/teleop_status"),
    )

    sensors_section = data.get("sensors", {})
    camera_data = sensors_section.get("camera", {})
    res_data = camera_data.get("resolution", {})

    camera_config = CameraSensorConfig(
        mount=camera_data.get("mount", "front"),
        aspect_ratio=camera_data.get("aspect_ratio", "16:9"),
        target_fps=camera_data.get("target_fps", 30),
        resolution=ResolutionConfig(
            width=res_data.get("width", 1280),
            height=res_data.get("height", 720),
        ),
    )

    sensors_config = SensorsConfig(
        lidar_count=sensors_section.get("lidar_count", 1),
        lidar_model=sensors_section.get("lidar_model", "Livox MID-360"),
        camera=camera_config,
    )

    cameras_section = data.get("cameras", {})
    cameras_config = CamerasConfig(
        main=_parse_camera_stream(
            cameras_section.get("main", {}),
            default_name="D456 DEPTH CAMERA",
            default_fps=camera_config.target_fps,
            default_resolution=camera_config.resolution,
        ),
        pip=_parse_camera_stream(
            cameras_section.get("pip", {}),
            default_name="FRONT CAM",
            default_fps=15,
            default_resolution=ResolutionConfig(width=640, height=360),
        ),
    )

    return RoverConfig(
        name=rover_section.get("name", "Lunabotics Rover"),
        computer=rover_section.get("computer", "NVIDIA Jetson Orin Nano"),
        os=rover_section.get("os", "Ubuntu 22.04 LTS"),
        ros_distro=rover_section.get("ros_distro", "Humble"),
        ros_domain_id=rover_section.get("ros_domain_id", 0),
        rover_ip=network_section.get("rover_ip", "192.168.1.100"),
        mission_control_ip=network_section.get("mission_control_ip", "192.168.1.50"),
        topics=topics_config,
        sensors=sensors_config,
        cameras=cameras_config,
        video_launch=_parse_video_launch(data.get("video_launch", {})),
        jetson_ssh=_parse_jetson_ssh(data.get("jetson_ssh", {})),
        mission_actions=_parse_mission_actions(data.get("mission_actions", {})),
    )


def _parse_video_launch(section: Dict[str, Any]) -> VideoLaunchConfig:
    """Builds the operator-side viewer list, skipping malformed entries."""
    if not isinstance(section, dict):
        return VideoLaunchConfig()

    viewers = []
    for entry in section.get("viewers", []) or []:
        if isinstance(entry, dict) and entry.get("command_file"):
            viewers.append(
                VideoViewer(
                    name=str(entry.get("name", entry["command_file"])),
                    command_file=str(entry["command_file"]),
                )
            )
    return VideoLaunchConfig(
        enabled=bool(section.get("enabled", False)),
        stop_on_stream_stop=bool(section.get("stop_on_stream_stop", True)),
        viewers=viewers,
    )


def _parse_mission_actions(section: Dict[str, Any]) -> Dict[str, MissionActionBinding]:
    """Builds the button -> command table, defaulting anything odd to mock.

    An unreadable binding must never silently become a real rover command, so
    every fallback here lands on MOCK.
    """
    if not isinstance(section, dict):
        return {}

    bindings: Dict[str, MissionActionBinding] = {}
    for name, entry in section.items():
        if not isinstance(entry, dict):
            entry = {}
        raw_mode = str(entry.get("mode", "mock")).strip().lower()
        try:
            mode = ActionMode(raw_mode)
        except ValueError:
            logger.warning(
                f"Mission action '{name}' has unknown mode '{raw_mode}'; using mock."
            )
            mode = ActionMode.MOCK
        bindings[str(name)] = MissionActionBinding(
            name=str(name),
            mode=mode,
            command=str(entry.get("command", "")),
            topic=str(entry.get("topic", "/mission/cmd")),
            payload=str(entry.get("payload", name)),
            service=str(entry.get("service", "")),
            action=str(entry.get("action", "")),
            label=str(entry.get("label", name)),
            is_system=bool(entry.get("is_system", False)),
        )
    return bindings


def _parse_camera_stream(
    section: Dict[str, Any],
    default_name: str,
    default_fps: int,
    default_resolution: ResolutionConfig,
) -> CameraStreamConfig:
    """Builds one ingest stream definition, tolerating a missing/!dict section."""
    if not isinstance(section, dict):
        section = {}

    res_section = section.get("resolution", {})
    if not isinstance(res_section, dict):
        res_section = {}

    return CameraStreamConfig(
        name=section.get("name", default_name),
        uri=str(section.get("uri", "") or ""),
        rtsp_transport=section.get("rtsp_transport", "udp"),
        target_fps=section.get("target_fps", default_fps),
        resolution=ResolutionConfig(
            width=res_section.get("width", default_resolution.width),
            height=res_section.get("height", default_resolution.height),
        ),
        corner=section.get("corner", "top_right"),
        width_fraction=float(section.get("width_fraction", 0.26)),
    )


def _parse_jetson_ssh(section: Dict[str, Any]) -> JetsonSSHConfig:
    """Builds the Jetson SSH session config, skipping malformed quick commands."""
    if not isinstance(section, dict):
        section = {}

    startup = section.get("startup_commands", [])
    if not isinstance(startup, list):
        startup = []

    quick_commands = []
    for entry in section.get("quick_commands", []) or []:
        if isinstance(entry, dict) and entry.get("command"):
            quick_commands.append(
                QuickCommand(
                    label=str(entry.get("label", entry["command"]))[:24],
                    command=str(entry["command"]),
                )
            )

    return JetsonSSHConfig(
        enabled=bool(section.get("enabled", True)),
        host=str(section.get("host", "") or ""),
        port=int(section.get("port", 22)),
        username=str(section.get("username", "jetson")),
        key_path=str(section.get("key_path", "") or ""),
        startup_commands=[str(c) for c in startup],
        connect_timeout_s=float(section.get("connect_timeout_s", 8.0)),
        keepalive_s=int(section.get("keepalive_s", 15)),
        quick_commands=quick_commands,
        auto_connect=bool(section.get("auto_connect", True)),
        reconnect_interval_s=max(2, int(section.get("reconnect_interval_s", 5))),
        clear_on_open=bool(section.get("clear_on_open", True)),
    )


def load_config(config_dir: Optional[Path] = None) -> tuple[AppConfig, RoverConfig]:
    """Convenience function to load both AppConfig and RoverConfig."""
    return load_app_config(config_dir), load_rover_config(config_dir)
