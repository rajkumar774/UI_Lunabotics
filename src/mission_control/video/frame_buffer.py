"""Bounded thread-safe frame buffer with newest-frame preference and stale frame dropping."""

from dataclasses import dataclass
import threading
import time
from typing import Any, Optional, Tuple
from PySide6.QtGui import QImage


@dataclass
class FrameMetadata:
    """Metadata attached to an ingested frame."""
    frame_index: int
    timestamp_captured: float
    resolution_width: int
    resolution_height: int
    source_type: str = "unknown"
    estimated_latency_ms: float = 0.0


class BoundedFrameBuffer:
    """Thread-safe bounded ring buffer designed for low-latency live video streaming.
    
    Prefers newest frames: if the buffer reaches maximum capacity when a new frame
    arrives, the oldest frame is dropped immediately to prevent operator lag.
    """

    def __init__(self, max_capacity: int = 2):
        if max_capacity < 1:
            raise ValueError("max_capacity must be at least 1")
        self._max_capacity = max_capacity
        self._buffer: list[Tuple[QImage, FrameMetadata]] = []
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._dropped_frames: int = 0
        self._total_pushed: int = 0

    @property
    def capacity(self) -> int:
        return self._max_capacity

    @property
    def dropped_frames_count(self) -> int:
        with self._lock:
            return self._dropped_frames

    @property
    def total_pushed_count(self) -> int:
        with self._lock:
            return self._total_pushed

    def size(self) -> int:
        with self._lock:
            return len(self._buffer)

    def push(self, image: QImage, metadata: FrameMetadata) -> bool:
        """Pushes a new frame into the buffer.
        
        If buffer is full, drops the oldest frame. Returns True if a frame was dropped.
        """
        was_dropped = False
        with self._lock:
            self._total_pushed += 1
            if len(self._buffer) >= self._max_capacity:
                self._buffer.pop(0)  # Drop oldest frame
                self._dropped_frames += 1
                was_dropped = True

            self._buffer.append((image, metadata))
            self._not_empty.notify()
        return was_dropped

    def pop(self, timeout: float = 0.1) -> Optional[Tuple[QImage, FrameMetadata]]:
        """Pulls the next available frame from the buffer, waiting up to timeout seconds."""
        with self._not_empty:
            start_time = time.time()
            while not self._buffer:
                remaining = timeout - (time.time() - start_time)
                if remaining <= 0:
                    return None
                self._not_empty.wait(timeout=remaining)
            return self._buffer.pop(0)

    def clear(self) -> None:
        """Clears all buffered frames and resets queue state."""
        with self._lock:
            self._buffer.clear()
