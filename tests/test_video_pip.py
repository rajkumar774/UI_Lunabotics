"""Tests for the picture-in-picture inset on the main video canvas."""

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from mission_control.video.frame_buffer import FrameMetadata
from mission_control.video.player_widget import LiveVideoPlayerWidget, VideoCanvas
from mission_control.video.source import TestPatternVideoSource
from mission_control.video.stats import StreamState


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def frame(width=320, height=180) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB888)
    image.fill(0)
    return image


# --- Canvas -------------------------------------------------------------

def test_pip_disabled_by_default(qapp):
    canvas = VideoCanvas()
    assert canvas.pip_enabled is False
    assert canvas.pip_is_live is False


def test_pip_enabled_after_configure(qapp):
    canvas = VideoCanvas()
    canvas.configure_pip("FRONT CAM", corner="bottom_left", width_fraction=0.3)
    assert canvas.pip_enabled is True


def test_pip_goes_live_with_a_frame(qapp):
    canvas = VideoCanvas()
    canvas.configure_pip("FRONT CAM")
    canvas.set_pip_frame(frame())
    assert canvas.pip_is_live is True


def test_pip_drops_frame_when_stream_stops(qapp):
    """An offline inset must read NO SIGNAL, never hold the last frame."""
    canvas = VideoCanvas()
    canvas.configure_pip("FRONT CAM")
    canvas.set_pip_frame(frame())
    canvas.set_pip_state(StreamState.DISCONNECTED)
    assert canvas.pip_is_live is False


def test_pip_rect_sits_in_requested_corner(qapp):
    canvas = VideoCanvas()
    viewport = QRect(0, 0, 1000, 562)

    canvas.configure_pip("CAM", corner="top_right", width_fraction=0.25)
    top_right = canvas.pip_rect(viewport)
    canvas.configure_pip("CAM", corner="bottom_left", width_fraction=0.25)
    bottom_left = canvas.pip_rect(viewport)

    assert top_right.right() < viewport.right()
    assert top_right.top() > viewport.top()
    assert bottom_left.left() > viewport.left()
    assert bottom_left.bottom() < viewport.bottom()
    assert top_right.left() > bottom_left.left()
    assert bottom_left.top() > top_right.top()


def test_pip_rect_is_16_by_9(qapp):
    canvas = VideoCanvas()
    canvas.configure_pip("CAM", width_fraction=0.25)
    rect = canvas.pip_rect(QRect(0, 0, 1600, 900))
    assert rect.width() / rect.height() == pytest.approx(16 / 9, abs=0.05)


def test_pip_width_fraction_is_clamped(qapp):
    """An inset may not grow large enough to obscure the main view."""
    canvas = VideoCanvas()
    viewport = QRect(0, 0, 1000, 562)

    canvas.configure_pip("CAM", width_fraction=5.0)
    assert canvas.pip_rect(viewport).width() <= viewport.width() * 0.5

    canvas.configure_pip("CAM", width_fraction=0.001)
    assert canvas.pip_rect(viewport).width() >= viewport.width() * 0.12


def test_unknown_corner_falls_back(qapp):
    canvas = VideoCanvas()
    canvas.configure_pip("CAM", corner="middle_of_nowhere")
    assert canvas._pip_corner == "top_right"


# --- Player widget ------------------------------------------------------

def test_player_default_title_unchanged(qapp):
    """The no-argument default stays as it was, so existing callers are safe."""
    assert LiveVideoPlayerWidget().camera_title.text() == "ROVER FRONT CAMERA"


def test_player_title_is_configurable(qapp):
    player = LiveVideoPlayerWidget(camera_title="D456 DEPTH CAMERA")
    assert player.camera_title.text() == "D456 DEPTH CAMERA"
    assert player.canvas._title == "D456 DEPTH CAMERA"


def test_player_routes_pip_frames_to_canvas(qapp):
    player = LiveVideoPlayerWidget(camera_title="D456 DEPTH CAMERA")
    player.configure_pip("FRONT CAM")
    meta = FrameMetadata(frame_index=1, timestamp_captured=1.0, resolution_width=320, resolution_height=180)

    player.handle_pip_frame(frame(), meta)
    assert player.canvas.pip_is_live is True

    player.handle_pip_state_changed(StreamState.NO_SIGNAL)
    assert player.canvas.pip_is_live is False


def test_main_and_pip_streams_are_independent(qapp):
    """A dead inset must not blank the primary camera."""
    player = LiveVideoPlayerWidget()
    player.configure_pip("FRONT CAM")
    meta = FrameMetadata(frame_index=1, timestamp_captured=1.0, resolution_width=320, resolution_height=180)

    player.handle_frame(frame(), meta)
    player.handle_state_changed(StreamState.LIVE)
    player.handle_pip_state_changed(StreamState.ERROR)

    assert player.canvas._current_frame is not None
    assert player.canvas.pip_is_live is False


# --- Test pattern source at inset size ----------------------------------

def test_test_pattern_label_is_configurable(qapp):
    source = TestPatternVideoSource(width=640, height=360, target_fps=15, label="FRONT CAM [TEST]")
    assert source.label == "FRONT CAM [TEST]"


def test_test_pattern_renders_at_inset_resolution(qapp):
    """Overlay geometry must stay inside a small frame, not run off the edge."""
    source = TestPatternVideoSource(width=640, height=360, target_fps=30, label="FRONT CAM")
    assert source.open() is True
    result = source.read_frame()
    source.close()

    assert result is not None
    image, metadata = result
    assert image.shape == (360, 640, 3)
    assert metadata.resolution_width == 640
    assert metadata.resolution_height == 360


# --- Independent per-camera control ---------------------------------------


def test_each_camera_has_its_own_toggle(qapp):
    player = LiveVideoPlayerWidget(camera_title="D456")
    player.set_pip_title("BACK CAM")
    assert player.toggle_stream_btn is not None
    assert "BACK CAM" in player.pip_toggle_btn.text()


def test_pip_toggle_only_reports_the_pip_stream(qapp):
    """Starting the inset must not start the main camera."""
    player = LiveVideoPlayerWidget()
    started, stopped = [], []
    player.stream_start_one.connect(started.append)
    player.stream_stop_one.connect(stopped.append)

    player.pip_toggle_btn.click()
    assert started == ["pip"]

    player.pip_toggle_btn.click()
    assert stopped == ["pip"]


def test_main_toggle_reports_the_main_stream(qapp):
    player = LiveVideoPlayerWidget()
    started = []
    player.stream_start_one.connect(started.append)

    player.toggle_stream_btn.click()
    assert started == ["main"]


def test_main_toggle_still_emits_the_legacy_signal(qapp):
    """Existing callers wired to 'start everything' must keep working."""
    player = LiveVideoPlayerWidget()
    legacy = []
    player.stream_start_requested.connect(lambda: legacy.append(1))

    player.toggle_stream_btn.click()
    assert legacy == [1]


def test_pip_button_label_tracks_its_state(qapp):
    player = LiveVideoPlayerWidget()
    player.set_pip_title("BACK CAM")
    assert player.pip_toggle_btn.text().startswith("▶")

    player.pip_toggle_btn.click()
    assert player.pip_toggle_btn.text().startswith("■")


def test_pip_button_resets_when_its_stream_dies(qapp):
    """A stream that drops on its own must not leave the button reading stop."""
    player = LiveVideoPlayerWidget()
    player.pip_toggle_btn.click()
    assert player._pip_running is True

    player.handle_pip_state_changed(StreamState.ERROR)
    assert player._pip_running is False
    assert player.pip_toggle_btn.text().startswith("▶")
