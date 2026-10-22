"""Tests for BoundedFrameBuffer and stale frame dropping behavior."""

import pytest
from PySide6.QtGui import QImage

from mission_control.video.frame_buffer import BoundedFrameBuffer, FrameMetadata


def test_frame_buffer_initialization():
    """Verifies buffer capacity and default values."""
    buf = BoundedFrameBuffer(max_capacity=3)
    assert buf.capacity == 3
    assert buf.size() == 0
    assert buf.dropped_frames_count == 0
    assert buf.total_pushed_count == 0

    with pytest.raises(ValueError):
        BoundedFrameBuffer(max_capacity=0)


def test_frame_buffer_push_and_pop():
    """Verifies FIFO push and pop operations when within capacity."""
    buf = BoundedFrameBuffer(max_capacity=2)
    img1 = QImage(64, 64, QImage.Format.Format_RGB888)
    meta1 = FrameMetadata(frame_index=1, timestamp_captured=100.0, resolution_width=64, resolution_height=64)

    dropped = buf.push(img1, meta1)
    assert dropped is False
    assert buf.size() == 1
    assert buf.total_pushed_count == 1
    assert buf.dropped_frames_count == 0

    item = buf.pop(timeout=0.1)
    assert item is not None
    frame_out, meta_out = item
    assert meta_out.frame_index == 1
    assert buf.size() == 0


def test_frame_buffer_stale_dropping():
    """Verifies that oldest frames are dropped when capacity is exceeded to minimize latency."""
    buf = BoundedFrameBuffer(max_capacity=2)
    img = QImage(32, 32, QImage.Format.Format_RGB888)

    meta1 = FrameMetadata(frame_index=1, timestamp_captured=1.0, resolution_width=32, resolution_height=32)
    meta2 = FrameMetadata(frame_index=2, timestamp_captured=2.0, resolution_width=32, resolution_height=32)
    meta3 = FrameMetadata(frame_index=3, timestamp_captured=3.0, resolution_width=32, resolution_height=32)

    buf.push(img, meta1)
    buf.push(img, meta2)
    assert buf.size() == 2
    assert buf.dropped_frames_count == 0

    # Pushing 3rd frame must drop 1st frame
    dropped = buf.push(img, meta3)
    assert dropped is True
    assert buf.size() == 2
    assert buf.dropped_frames_count == 1
    assert buf.total_pushed_count == 3

    # Next pop must be frame 2, then frame 3 (frame 1 was dropped)
    _, out_meta2 = buf.pop(timeout=0.1)
    assert out_meta2.frame_index == 2

    _, out_meta3 = buf.pop(timeout=0.1)
    assert out_meta3.frame_index == 3

    assert buf.size() == 0


def test_frame_buffer_clear():
    """Verifies clearing the buffer resets size."""
    buf = BoundedFrameBuffer(max_capacity=2)
    img = QImage(16, 16, QImage.Format.Format_RGB888)
    meta = FrameMetadata(frame_index=1, timestamp_captured=1.0, resolution_width=16, resolution_height=16)

    buf.push(img, meta)
    assert buf.size() == 1
    buf.clear()
    assert buf.size() == 0
