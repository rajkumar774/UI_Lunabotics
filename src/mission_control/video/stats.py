"""Stream metrics and statistics tracking module using monotonic timing."""

from collections import deque
from dataclasses import dataclass
from enum import Enum
import threading
import time
from typing import Optional


class StreamState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    LIVE = "LIVE"
    DEGRADED = "DEGRADED"
    NO_SIGNAL = "NO SIGNAL"
    ERROR = "STREAM ERROR"


@dataclass
class StreamStats:
    """Snapshot container for stream health, performance, and metrics."""
    state: StreamState = StreamState.DISCONNECTED
    received_fps: float = 0.0
    displayed_fps: float = 0.0
    dropped_frames: int = 0
    resolution_width: int = 0
    resolution_height: int = 0
    bitrate_kbps: float = 0.0
    estimated_latency_ms: float = 0.0
    latency_is_simulated: bool = False
    uptime_seconds: float = 0.0
    reconnect_count: int = 0
    error_message: str = ""

    @property
    def resolution_str(self) -> str:
        if self.resolution_width > 0 and self.resolution_height > 0:
            return f"{self.resolution_width}×{self.resolution_height}"
        return "--×--"

    @property
    def fps_str(self) -> str:
        if self.displayed_fps > 0:
            return f"{self.displayed_fps:.0f} FPS"
        return "-- FPS"

    @property
    def latency_str(self) -> str:
        if self.estimated_latency_ms > 0:
            if self.latency_is_simulated:
                return f"~{self.estimated_latency_ms:.0f} ms (SIM)"
            return f"~{self.estimated_latency_ms:.0f} ms"
        return "LATENCY: UNMEASURED"


class StreamStatisticsTracker:
    """Thread-safe sliding-window tracker for stream throughput, latency, and health.
    
    Uses time.monotonic() exclusively for jitter-free FPS and duration calculations.
    """

    def __init__(self, window_duration_seconds: float = 1.0):
        self._window_duration = window_duration_seconds
        self._lock = threading.Lock()
        
        self._received_timestamps: deque[float] = deque()
        self._displayed_timestamps: deque[float] = deque()
        self._byte_samples: deque[tuple[float, int]] = deque()
        
        self._state: StreamState = StreamState.DISCONNECTED
        self._dropped_frames_count: int = 0
        self._reconnect_count: int = 0
        self._res_w: int = 0
        self._res_h: int = 0
        self._latest_latency_ms: float = 0.0
        self._latency_is_simulated: bool = False
        self._session_start_time: Optional[float] = None
        self._error_message: str = ""

    def set_state(self, state: StreamState, error_msg: str = "") -> None:
        with self._lock:
            self._state = state
            self._error_message = error_msg
            now = time.monotonic()
            if state == StreamState.LIVE and self._session_start_time is None:
                self._session_start_time = now
            elif state in (StreamState.DISCONNECTED, StreamState.ERROR):
                self._session_start_time = None

    def record_reconnect_attempt(self) -> None:
        with self._lock:
            self._reconnect_count += 1

    def record_frame_received(
        self,
        width: int,
        height: int,
        byte_size: int = 0,
        latency_ms: float = 0.0,
        is_simulated_latency: bool = False,
    ) -> None:
        now = time.monotonic()
        with self._lock:
            self._received_timestamps.append(now)
            if byte_size > 0:
                self._byte_samples.append((now, byte_size))
            self._res_w = width
            self._res_h = height
            if latency_ms > 0:
                self._latest_latency_ms = latency_ms
                self._latency_is_simulated = is_simulated_latency
            self._prune(now)

    def record_frame_displayed(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._displayed_timestamps.append(now)
            self._prune(now)

    def record_frame_dropped(self) -> None:
        with self._lock:
            self._dropped_frames_count += 1

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_duration
        while self._received_timestamps and self._received_timestamps[0] < cutoff:
            self._received_timestamps.popleft()
        while self._displayed_timestamps and self._displayed_timestamps[0] < cutoff:
            self._displayed_timestamps.popleft()
        while self._byte_samples and self._byte_samples[0][0] < cutoff:
            self._byte_samples.popleft()

    def get_stats(self) -> StreamStats:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            
            # FPS calculations
            rec_fps = len(self._received_timestamps) / self._window_duration
            disp_fps = len(self._displayed_timestamps) / self._window_duration
            
            # Bitrate (kbps)
            total_bytes = sum(b for _, b in self._byte_samples)
            bitrate_kbps = (total_bytes * 8 / 1000.0) / self._window_duration if total_bytes > 0 else 0.0
            
            uptime = (now - self._session_start_time) if self._session_start_time else 0.0

            return StreamStats(
                state=self._state,
                received_fps=rec_fps,
                displayed_fps=disp_fps,
                dropped_frames=self._dropped_frames_count,
                resolution_width=self._res_w,
                resolution_height=self._res_h,
                bitrate_kbps=bitrate_kbps,
                estimated_latency_ms=self._latest_latency_ms,
                latency_is_simulated=self._latency_is_simulated,
                uptime_seconds=uptime,
                reconnect_count=self._reconnect_count,
                error_message=self._error_message,
            )

    def reset(self) -> None:
        with self._lock:
            self._received_timestamps.clear()
            self._displayed_timestamps.clear()
            self._byte_samples.clear()
            self._dropped_frames_count = 0
            self._reconnect_count = 0
            self._res_w = 0
            self._res_h = 0
            self._latest_latency_ms = 0.0
            self._latency_is_simulated = False
            self._session_start_time = None
            self._error_message = ""
            self._state = StreamState.DISCONNECTED
