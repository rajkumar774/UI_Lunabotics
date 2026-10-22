"""Edge case, stress, and error recovery tests for video streaming subsystem."""

import time
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage

from mission_control.video.frame_buffer import BoundedFrameBuffer, FrameMetadata
from mission_control.video.source import BaseVideoSource, TestPatternVideoSource
from mission_control.video.worker import VideoWorker
from mission_control.video.stats import StreamState, StreamStats, StreamStatisticsTracker
from mission_control.video.player_widget import LiveVideoPlayerWidget


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class FlakyFailingVideoSource(BaseVideoSource):
    """Simulates a failing/reconnecting camera source for edge-case testing."""
    __test__ = False

    def __init__(self, fail_open_count: int = 1, fail_read_after: int = 5):
        self.fail_open_count = fail_open_count
        self.fail_read_after = fail_read_after
        self.open_attempts = 0
        self.frames_read = 0
        self._is_open = False

    def open(self) -> bool:
        self.open_attempts += 1
        if self.open_attempts <= self.fail_open_count:
            return False
        self._is_open = True
        return True

    def is_opened(self) -> bool:
        return self._is_open

    def close(self) -> None:
        self._is_open = False

    def read_frame(self):
        if not self._is_open:
            return None
        self.frames_read += 1
        if self.frames_read > self.fail_read_after:
            return None  # Simulate stream dropout
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        meta = FrameMetadata(
            frame_index=self.frames_read,
            timestamp_captured=time.monotonic(),
            resolution_width=640,
            resolution_height=360,
        )
        return frame, meta


def test_worker_repeated_start_stop_cycles(qapp):
    """Verifies that VideoWorker can start, stop, restart, and terminate repeatedly without hanging or leaking threads."""
    source = TestPatternVideoSource(width=320, height=180, target_fps=60)
    worker = VideoWorker(video_source=source, buffer_capacity=2)

    for cycle in range(3):
        worker.start_stream()
        assert worker.is_streaming is True
        for _ in range(15):
            qapp.processEvents()
            time.sleep(0.01)

        worker.stop_stream()
        assert worker.is_streaming is False
        assert worker.isRunning() is False
        qapp.processEvents()


def test_worker_reconnection_on_source_failure(qapp):
    """Verifies that VideoWorker handles source open failures and enters ERROR/CONNECTING recovery."""
    failing_source = FlakyFailingVideoSource(fail_open_count=1, fail_read_after=3)
    worker = VideoWorker(video_source=failing_source, buffer_capacity=2)
    worker._reconnect_backoff_base = 0.05  # Fast backoff for test speed

    states_recorded = []
    worker.state_changed.connect(lambda s: states_recorded.append(s))

    worker.start_stream()
    
    # Wait for failure and retry cycle
    for _ in range(25):
        qapp.processEvents()
        time.sleep(0.02)

    worker.stop_stream()
    assert worker.isRunning() is False

    # Verify state progression includes ERROR and recovery to LIVE
    assert StreamState.ERROR in states_recorded
    assert StreamState.LIVE in states_recorded


def test_frame_buffer_burst_dropping():
    """Simulates a fast 60 FPS producer and a slow consumer, verifying strict capacity enforcement."""
    buf = BoundedFrameBuffer(max_capacity=2)
    test_img = QImage(64, 64, QImage.Format.Format_RGB888)

    # Push 50 frames in a burst
    for i in range(50):
        meta = FrameMetadata(frame_index=i, timestamp_captured=time.monotonic(), resolution_width=64, resolution_height=64)
        buf.push(test_img, meta)

    # Max capacity must still be 2, and exactly 48 frames were dropped
    assert buf.size() == 2
    assert buf.dropped_frames_count == 48
    assert buf.total_pushed_count == 50

    # Next pop must be frame 48, then 49
    _, meta_48 = buf.pop(timeout=0.1)
    assert meta_48.frame_index == 48

    _, meta_49 = buf.pop(timeout=0.1)
    assert meta_49.frame_index == 49

    assert buf.size() == 0


def test_player_widget_rapid_resizing_and_states(qapp):
    """Verifies that LiveVideoPlayerWidget handles rapid resizing and state toggling smoothly."""
    player = LiveVideoPlayerWidget()
    player.show()

    # Simulate rapid resizes
    for size in [(800, 600), (1280, 720), (640, 480), (1920, 1080)]:
        player.resize(*size)
        qapp.processEvents()

    # Verify states render without exception
    for state in [StreamState.CONNECTING, StreamState.LIVE, StreamState.NO_SIGNAL, StreamState.ERROR, StreamState.DISCONNECTED]:
        player.handle_state_changed(state)
        qapp.processEvents()

    player.close()
