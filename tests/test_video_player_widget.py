"""Tests for LiveVideoPlayerWidget layout, HUD updating, and state handling."""

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage

from mission_control.video.player_widget import LiveVideoPlayerWidget
from mission_control.video.frame_buffer import FrameMetadata
from mission_control.video.stats import StreamState, StreamStats


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_video_player_widget_init(qapp):
    """Verifies default widget layout, labels, and state."""
    player = LiveVideoPlayerWidget()

    assert player.camera_title.text() == "ROVER FRONT CAMERA"
    assert player.state_label.text() == "DISCONNECTED"
    assert player.fps_label.text() == "-- FPS"
    assert player.latency_label.text() == "~-- ms"
    assert player.toggle_stream_btn.text() == "START STREAM"
    assert player.fullscreen_btn.text() == "⛶ FULLSCREEN"


def test_video_player_handle_stats(qapp):
    """Verifies HUD label updates upon receiving StreamStats snapshot."""
    player = LiveVideoPlayerWidget()

    stats = StreamStats(
        state=StreamState.LIVE,
        received_fps=30.0,
        displayed_fps=29.8,
        dropped_frames=0,
        resolution_width=1280,
        resolution_height=720,
        estimated_latency_ms=17.5,
    )

    player.handle_stats(stats)
    assert player.fps_label.text() == "30 FPS"
    assert "~18 ms" in player.latency_label.text()
    assert "1280×720 HD" in player.resolution_label.text()


def test_video_player_state_transitions(qapp):
    """Verifies button text and badge updates across StreamState changes."""
    player = LiveVideoPlayerWidget()

    # Transition to CONNECTING
    player.handle_state_changed(StreamState.CONNECTING)
    assert player.state_label.text() == StreamState.CONNECTING.value
    assert player.toggle_stream_btn.text() == "CONNECTING..."

    # Transition to LIVE
    player.handle_state_changed(StreamState.LIVE)
    assert player.state_label.text() == StreamState.LIVE.value
    assert player.toggle_stream_btn.text() == "STOP STREAM"

    # Transition to DISCONNECTED
    player.handle_state_changed(StreamState.DISCONNECTED)
    assert player.state_label.text() == StreamState.DISCONNECTED.value
    assert player.toggle_stream_btn.text() == "START STREAM"


def test_video_player_handle_frame(qapp):
    """Verifies canvas frame assignment without errors."""
    player = LiveVideoPlayerWidget()
    test_img = QImage(320, 180, QImage.Format.Format_RGB888)
    meta = FrameMetadata(frame_index=1, timestamp_captured=1.0, resolution_width=320, resolution_height=180)

    player.handle_frame(test_img, meta)
    assert player.canvas._current_frame is not None


def test_video_player_fullscreen_toggle(qapp):
    """Verifies fullscreen toggle and signal emission."""
    player = LiveVideoPlayerWidget()
    toggled_states = []
    player.fullscreen_toggled.connect(lambda fs: toggled_states.append(fs))

    player.toggle_fullscreen()
    assert player._is_fullscreen is True
    assert player.fullscreen_btn.text() == "✕ EXIT FULLSCREEN"
    assert True in toggled_states

    player.toggle_fullscreen()
    assert player._is_fullscreen is False
    assert player.fullscreen_btn.text() == "⛶ FULLSCREEN"
    assert False in toggled_states


def test_fullscreen_detaches_from_its_splitter_and_comes_back(qapp):
    """The real window keeps the player in a QSplitter, and that is where
    fullscreen used to freeze.

    Flipping the window flags while the splitter still held the widget left
    the splitter geometry-managing a top-level window; the two relayout each
    other forever and the UI locks up. The standalone-widget test above never
    caught it because a parentless widget has no layout to fight with.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QSplitter, QWidget

    splitter = QSplitter(Qt.Horizontal)
    player = LiveVideoPlayerWidget()
    sidebar = QWidget()
    splitter.addWidget(player)
    splitter.addWidget(sidebar)
    splitter.setSizes([800, 400])
    splitter.resize(1200, 600)
    splitter.show()

    assert splitter.indexOf(player) == 0

    player.toggle_fullscreen()
    # It must have LEFT the splitter, not merely changed its flags.
    assert player._is_fullscreen is True
    assert player.parentWidget() is None
    assert splitter.indexOf(player) == -1
    assert splitter.count() == 1

    player.toggle_fullscreen()
    # ...and come back to the same slot, visible, with the sidebar intact.
    assert player._is_fullscreen is False
    assert splitter.indexOf(player) == 0
    assert splitter.count() == 2
    assert not player.isHidden()
    assert player.windowFlags() & Qt.Window != Qt.Window

    player.close()
    splitter.close()


def test_fullscreen_without_a_splitter_still_restores_its_parent(qapp):
    """A player held by a plain parent widget must also come back."""
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    host = QWidget()
    layout = QVBoxLayout(host)
    player = LiveVideoPlayerWidget()
    layout.addWidget(player)
    host.show()

    player.toggle_fullscreen()
    assert player.parentWidget() is None

    player.toggle_fullscreen()
    assert player._is_fullscreen is False
    assert player.parentWidget() is host
    assert not player.isHidden()

    player.close()
    host.close()


def test_stop_button_returns_to_start_without_a_stats_update(qapp):
    """STOP must stop, even if no StreamStats ever arrived.

    State transitions and statistics reach the player on two different
    signals. The toggle used to branch on self._stats.state while the button
    label came from handle_state_changed(), so a stream that went LIVE without
    a stats update left the button reading STOP STREAM while the toggle still
    believed it was stopped -- pressing it restarted the stream instead of
    stopping it, and the label never went back to START STREAM.
    """
    from mission_control.video.stats import StreamState

    player = LiveVideoPlayerWidget()
    started, stopped = [], []
    player.stream_start_one.connect(started.append)
    player.stream_stop_one.connect(stopped.append)

    player.toggle_stream_btn.click()
    assert started == ["main"]

    # The worker reports LIVE, but no stats are published.
    player.handle_state_changed(StreamState.LIVE)
    assert player.toggle_stream_btn.text() == "STOP STREAM"

    player.toggle_stream_btn.click()
    assert stopped == ["main"], "second press must stop, not start again"
    assert started == ["main"]

    player.handle_state_changed(StreamState.DISCONNECTED)
    assert player.toggle_stream_btn.text() == "START STREAM"
    player.close()


def test_localization_led_colours(qapp):
    """Green only while fixes arrive; grey for unknown, never a confident red."""
    import re

    def colour(w):
        return re.search(r"color: (#[0-9a-fA-F]+)", w.localization_led.styleSheet()).group(1)

    player = LiveVideoPlayerWidget()
    assert colour(player) == "#8a7079", "must start grey (unknown), not red"

    player.set_localized(True)
    assert colour(player) == "#34d399"

    player.set_localized(False)
    assert colour(player) == "#ff5f5f"

    # A dead link is 'unknown', not 'not localized' -- back to grey.
    player.set_localized(None)
    assert colour(player) == "#8a7079"
    player.close()
