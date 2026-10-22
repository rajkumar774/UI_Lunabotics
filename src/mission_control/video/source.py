"""Video source abstraction and implementations (Synthetic Test Pattern & OpenCV / RTSP)."""

from abc import ABC, abstractmethod
import math
import os
import threading
import time
from typing import Optional, Tuple, Union
import cv2
import numpy as np

from mission_control.core.logger import get_logger
from mission_control.video.frame_buffer import FrameMetadata

logger = get_logger("video_source")


class BaseVideoSource(ABC):
    """Abstract interface for video ingest sources."""

    @abstractmethod
    def open(self) -> bool:
        """Opens and initializes the video source."""
        pass

    @abstractmethod
    def is_opened(self) -> bool:
        """Returns True if the source is active and ready to produce frames."""
        pass

    @abstractmethod
    def read_frame(self) -> Optional[Tuple[np.ndarray, FrameMetadata]]:
        """Reads the next available BGR image frame and its metadata.
        
        Returns None on stream EOF, timeout, or read failure.
        """
        pass

    @abstractmethod
    def close(self) -> None:
        """Closes and releases source resources."""
        pass


class TestPatternVideoSource(BaseVideoSource):
    """Generates synthetic 16:9 test pattern frames with dynamic animations,
    timestamps, and simulated latency for standalone verification.
    """
    __test__ = False

    def __init__(
        self,
        width: int = 1280,
        height: int = 720,
        target_fps: int = 30,
        simulated_latency_base_ms: float = 18.0,
        label: str = "ROVER FRONT CAMERA [SYNTHETIC TEST PATTERN]",
    ):
        self.width = width
        self.height = height
        self.target_fps = target_fps
        self.frame_interval = 1.0 / max(1, target_fps)
        self.simulated_latency_base_ms = simulated_latency_base_ms
        self.label = label
        # Overlay geometry is derived from frame size so the pattern stays
        # legible when rendered small, e.g. as a picture-in-picture inset.
        self._scale = max(0.4, min(1.0, width / 1280.0))

        self._is_open = False
        self._frame_count = 0
        self._last_frame_time = 0.0
        self._start_time = 0.0

    def open(self) -> bool:
        self._is_open = True
        self._frame_count = 0
        self._start_time = time.monotonic()
        self._last_frame_time = time.monotonic()
        logger.info(
            f"TestPatternVideoSource opened: {self.width}x{self.height} @ {self.target_fps} FPS."
        )
        return True

    def is_opened(self) -> bool:
        return self._is_open

    def close(self) -> None:
        self._is_open = False
        logger.info("TestPatternVideoSource closed.")

    def read_frame(self) -> Optional[Tuple[np.ndarray, FrameMetadata]]:
        if not self._is_open:
            return None

        # Rate limiter using monotonic clock
        now_mono = time.monotonic()
        elapsed_since_last = now_mono - self._last_frame_time
        sleep_time = self.frame_interval - elapsed_since_last
        if sleep_time > 0.001:
            time.sleep(sleep_time)

        self._last_frame_time = time.monotonic()
        self._frame_count += 1
        wall_time = time.time()

        # 1. Create dark canvas with grid background
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[:] = (18, 22, 30)  # Dark slate background BGR

        # Draw grid lines
        grid_spacing = max(16, int(64 * self._scale))
        for x in range(0, self.width, grid_spacing):
            cv2.line(frame, (x, 0), (x, self.height), (28, 34, 46), 1)
        for y in range(0, self.height, grid_spacing):
            cv2.line(frame, (0, y), (self.width, y), (28, 34, 46), 1)

        # 2. Draw Color Calibration Bars at top
        bar_height = max(8, int(24 * self._scale))
        bar_width = self.width // 7
        colors = [
            (255, 255, 255),  # White
            (0, 255, 255),    # Yellow
            (255, 255, 0),    # Cyan
            (0, 255, 0),      # Green
            (255, 0, 255),    # Magenta
            (0, 0, 255),      # Red
            (255, 0, 0),      # Blue
        ]
        for i, color in enumerate(colors):
            x1 = i * bar_width
            x2 = (i + 1) * bar_width if i < 6 else self.width
            cv2.rectangle(frame, (x1, 0), (x2, bar_height), color, -1)

        # 3. Draw Center Reticle & Radar Sweep Line (Proves Live Fluid Motion)
        cx, cy = self.width // 2, self.height // 2
        radius = max(30, int(160 * self._scale))
        cv2.circle(frame, (cx, cy), radius, (56, 189, 248), 2)       # Outer circle (Cyan)
        cv2.circle(frame, (cx, cy), radius // 2, (38, 54, 80), 1)  # Inner circle
        cv2.drawMarker(frame, (cx, cy), (56, 189, 248), cv2.MARKER_CROSS, max(8, int(24 * self._scale)), 2)

        # Rotating sweep line
        angle_rad = (self._frame_count * 0.08) % (2 * math.pi)
        sweep_x = int(cx + radius * math.cos(angle_rad))
        sweep_y = int(cy + radius * math.sin(angle_rad))
        cv2.line(frame, (cx, cy), (sweep_x, sweep_y), (52, 211, 153), 2) # Green sweep line

        # 4. Text Information Overlay
        title_scale = 0.65 * self._scale
        (title_w, _), _ = cv2.getTextSize(
            self.label, cv2.FONT_HERSHEY_SIMPLEX, title_scale, 2
        )
        cv2.putText(
            frame,
            self.label,
            (max(4, cx - title_w // 2), cy - radius - int(20 * self._scale)),
            cv2.FONT_HERSHEY_SIMPLEX,
            title_scale,
            (241, 245, 249),
            2,
            cv2.LINE_AA,
        )

        time_str = time.strftime("%H:%M:%S", time.localtime(wall_time)) + f".{int((wall_time % 1) * 1000):03d}"
        info_scale = 0.55 * self._scale
        info_text = (
            f"FRAME: {self._frame_count:06d}  |  TIME: {time_str}  |  RES: {self.width}x{self.height}"
        )
        (info_w, _), _ = cv2.getTextSize(info_text, cv2.FONT_HERSHEY_SIMPLEX, info_scale, 1)
        cv2.putText(
            frame,
            info_text,
            (max(4, cx - info_w // 2), cy + radius + int(35 * self._scale)),
            cv2.FONT_HERSHEY_SIMPLEX,
            info_scale,
            (148, 163, 184),
            1,
            cv2.LINE_AA,
        )

        # Simulated jitter latency (clearly tagged as simulated)
        sim_latency = self.simulated_latency_base_ms + 4.0 * math.sin(self._frame_count * 0.1)

        metadata = FrameMetadata(
            frame_index=self._frame_count,
            timestamp_captured=now_mono,
            resolution_width=self.width,
            resolution_height=self.height,
            source_type="test_pattern",
            estimated_latency_ms=max(1.0, sim_latency),
        )

        return frame, metadata


#: Serialises cv2.VideoCapture construction across every video worker.
#:
#: OpenCV's GStreamer backend is NOT safe to open from two threads at once:
#: with the main and PiP cameras both opening, and especially when the stream
#: is unreachable and both take the failure path, the process segfaults. The
#: cameras still *run* fully in parallel -- only the open is serialised, which
#: costs a few hundred milliseconds once per connection attempt.
_CAPTURE_OPEN_LOCK = threading.Lock()


def looks_like_gstreamer_pipeline(uri) -> bool:
    """True when `uri` is a GStreamer pipeline rather than a URL or device id.

    A pipeline is a chain of elements joined by '!', e.g.
    ``rtspsrc location=... ! rtph264depay ! ... ! appsink``.
    """
    return isinstance(uri, str) and "!" in uri


class OpenCVVideoSource(BaseVideoSource):
    """Captures frames from RTSP streams, GStreamer pipelines, webcams or files.

    Three input forms are supported:
      * ``rtsp://host:port/path``        -> FFmpeg backend, low-latency flags
      * ``src ! ... ! appsink``          -> GStreamer backend (must end in appsink)
      * ``0`` / ``/dev/video0`` / path   -> whatever OpenCV auto-detects
    """

    def __init__(self, source_uri: Union[str, int] = 0, rtsp_transport: str = "udp"):
        self.source_uri = source_uri
        self.rtsp_transport = rtsp_transport
        self._cap: Optional[cv2.VideoCapture] = None
        self._frame_count = 0

    def open(self) -> bool:
        logger.info(f"Opening OpenCV video source: {self.source_uri}...")
        try:
            # One capture opens at a time, process-wide. See _CAPTURE_OPEN_LOCK.
            with _CAPTURE_OPEN_LOCK:
                self._cap = self._create_capture()

            if self._cap is None or not self._cap.isOpened():
                logger.error(f"Failed to open video source: {self.source_uri}")
                self._cap = None
                return False

            logger.info(f"OpenCV video source opened successfully: {self.source_uri}")
            return True
        except Exception as e:
            logger.error(f"Exception opening video source {self.source_uri}: {e}")
            self._cap = None
            return False

    def _create_capture(self) -> Optional[cv2.VideoCapture]:
        """Builds the capture with an explicitly chosen backend.

        The backend is always named. Letting OpenCV auto-detect on a pipeline
        string is what produced the segfault: it silently selected GStreamer
        and then crashed on the failure path.
        """
        uri = self.source_uri

        if looks_like_gstreamer_pipeline(uri):
            if "appsink" not in uri:
                logger.error(
                    "GStreamer pipeline must end in `appsink` for OpenCV to read "
                    f"frames from it -- got: {uri}"
                )
                return None
            if not cv2.getBuildInformation().count("GStreamer:                   YES"):
                logger.error("This OpenCV build has no GStreamer support.")
                return None
            return cv2.VideoCapture(uri, cv2.CAP_GSTREAMER)

        if isinstance(uri, str) and uri.startswith("rtsp://"):
            # Low-latency FFmpeg options; must be set before the capture opens.
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
                f"rtsp_transport;{self.rtsp_transport}|fflags;nobuffer|max_delay;0|flags;low_delay"
            )
            cap = cv2.VideoCapture(uri, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            return cap

        return cv2.VideoCapture(uri, cv2.CAP_ANY)

    def is_opened(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def close(self) -> None:
        if self._cap:
            self._cap.release()
            self._cap = None
        logger.info(f"OpenCV video source closed: {self.source_uri}")

    def read_frame(self) -> Optional[Tuple[np.ndarray, FrameMetadata]]:
        if not self.is_opened():
            return None

        ret, frame = self._cap.read()
        if not ret or frame is None:
            return None

        self._frame_count += 1
        now_mono = time.monotonic()
        h, w = frame.shape[:2]

        metadata = FrameMetadata(
            frame_index=self._frame_count,
            timestamp_captured=now_mono,
            resolution_width=w,
            resolution_height=h,
            source_type=str(self.source_uri),
            estimated_latency_ms=0.0,  # 0.0 indicates unmeasured on real network stream
        )

        return frame, metadata
