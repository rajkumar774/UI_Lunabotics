"""Tests for StreamStatisticsTracker and StreamStats models."""

import time
import pytest

from mission_control.video.stats import StreamState, StreamStats, StreamStatisticsTracker


def test_stream_stats_defaults():
    """Verifies default snapshot values."""
    stats = StreamStats()
    assert stats.state == StreamState.DISCONNECTED
    assert stats.received_fps == 0.0
    assert stats.displayed_fps == 0.0
    assert stats.dropped_frames == 0
    assert stats.resolution_str == "--×--"
    assert stats.fps_str == "-- FPS"
    assert stats.latency_str == "LATENCY: UNMEASURED"


def test_stream_statistics_tracker():
    """Verifies FPS calculation and metric tracking in StreamStatisticsTracker."""
    tracker = StreamStatisticsTracker(window_duration_seconds=1.0)
    tracker.set_state(StreamState.LIVE)

    # Record 10 received and displayed frames
    for i in range(10):
        tracker.record_frame_received(width=1280, height=720, byte_size=50000, latency_ms=18.5)
        tracker.record_frame_displayed()

    tracker.record_frame_dropped()
    tracker.record_reconnect_attempt()

    stats = tracker.get_stats()
    assert stats.state == StreamState.LIVE
    assert stats.received_fps > 0.0
    assert stats.displayed_fps > 0.0
    assert stats.dropped_frames == 1
    assert stats.reconnect_count == 1
    assert stats.resolution_width == 1280
    assert stats.resolution_height == 720
    assert stats.resolution_str == "1280×720"
    assert stats.estimated_latency_ms == 18.5
    assert "~18 ms" in stats.latency_str


def test_stream_statistics_state_transition():
    """Verifies state transitions and error message tracking."""
    tracker = StreamStatisticsTracker()
    tracker.set_state(StreamState.CONNECTING)
    assert tracker.get_stats().state == StreamState.CONNECTING

    tracker.set_state(StreamState.ERROR, "Camera feed timeout")
    stats = tracker.get_stats()
    assert stats.state == StreamState.ERROR
    assert stats.error_message == "Camera feed timeout"
