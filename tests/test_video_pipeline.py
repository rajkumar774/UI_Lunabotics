"""Tests for video sources and background VideoWorker thread execution."""

import time
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage

from mission_control.video.source import TestPatternVideoSource
from mission_control.video.worker import VideoWorker
from mission_control.video.stats import StreamState


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_test_pattern_video_source():
    """Verifies synthetic test pattern generation dimensions and metadata."""
    source = TestPatternVideoSource(width=1280, height=720, target_fps=30)
    assert source.open() is True
    assert source.is_opened() is True

    result = source.read_frame()
    assert result is not None
    frame, metadata = result

    assert frame.shape == (720, 1280, 3)
    assert metadata.resolution_width == 1280
    assert metadata.resolution_height == 720
    assert metadata.frame_index == 1
    assert metadata.estimated_latency_ms > 0.0

    source.close()
    assert source.is_opened() is False


def test_video_worker_lifecycle(qapp):
    """Verifies VideoWorker QThread lifecycle, signal dispatch, and clean termination."""
    source = TestPatternVideoSource(width=640, height=360, target_fps=60)
    worker = VideoWorker(video_source=source, buffer_capacity=2)

    received_frames = []
    received_states = []

    worker.frame_ready.connect(lambda frame, meta: received_frames.append((frame, meta)))
    worker.state_changed.connect(lambda state: received_states.append(state))

    # Start stream
    worker.start_stream()
    assert worker.is_streaming is True

    # Process events while worker emits frames
    for _ in range(30):
        qapp.processEvents()
        time.sleep(0.01)

    # Clean shutdown
    worker.stop_stream()
    assert worker.is_streaming is False
    assert worker.isRunning() is False

    # Flush any remaining delivered signals
    qapp.processEvents()

    # Check signal reception
    assert len(received_frames) > 0
    first_frame, first_meta = received_frames[0]
    assert isinstance(first_frame, QImage)
    assert first_meta.resolution_width == 640

    assert StreamState.CONNECTING in received_states
    assert StreamState.LIVE in received_states
