"""Tests for video source backend selection.

Regression cover for a real segfault: with GStreamer pipelines in both camera
`uri` fields and the rover unreachable, the main and PiP workers both entered
cv2.VideoCapture() at the same time with an auto-detected backend, and the
process died. The UI simply vanished when the operator pressed START STREAM.

Two things prevent it now: the backend is always named explicitly, and capture
construction is serialised process-wide.
"""

import threading

import pytest

from mission_control.video.source import (
    OpenCVVideoSource,
    TestPatternVideoSource,
    looks_like_gstreamer_pipeline,
)

# These pipelines must be UNREACHABLE for the tests below to mean anything.
# They used to carry the rover's real address, so they only passed while the
# rover happened to be switched off -- once the streams were live, open()
# rightly succeeded and three tests failed for no good reason.
#
# A closed port on loopback is the right target: it refuses instantly, so the
# tests are both deterministic and fast. An unroutable address (TEST-NET-1)
# would also be deterministic but makes GStreamer sit through a connect
# timeout, turning a 2-second suite into a 3-minute one.
FRONT_PIPELINE = (
    "rtspsrc location=rtsp://127.0.0.1:65401/stream latency=0 protocols=tcp "
    "! rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! appsink"
)
BACK_PIPELINE = (
    "tcpclientsrc host=127.0.0.1 port=65402 ! multipartdemux "
    "! queue leaky=downstream max-size-buffers=1 ! jpegdec ! videoconvert "
    "! videoflip method=rotate-180 ! appsink"
)


# --- Pipeline detection ---------------------------------------------------


@pytest.mark.parametrize("uri", [FRONT_PIPELINE, BACK_PIPELINE, "videotestsrc ! appsink"])
def test_pipelines_are_detected(uri):
    assert looks_like_gstreamer_pipeline(uri) is True


@pytest.mark.parametrize("uri", [
    "rtsp://192.168.2.20:8554/stream",
    "/dev/video0",
    "/home/op/clip.mp4",
    0,
])
def test_non_pipelines_are_not_detected(uri):
    assert looks_like_gstreamer_pipeline(uri) is False


# --- Backend selection ----------------------------------------------------


def test_pipeline_without_appsink_is_refused_not_opened():
    """OpenCV cannot read from a display sink; refusing beats a hung capture."""
    source = OpenCVVideoSource(
        "rtspsrc location=rtsp://127.0.0.1:65401/stream ! decodebin ! autovideosink"
    )
    assert source.open() is False
    assert source.is_opened() is False


def test_unreachable_pipeline_fails_cleanly():
    """The rover being down must be a False return, never a crash."""
    source = OpenCVVideoSource(FRONT_PIPELINE)
    assert source.open() is False
    assert source.is_opened() is False
    source.close()


def test_unreachable_rtsp_url_fails_cleanly():
    source = OpenCVVideoSource("rtsp://192.0.2.99:8554/nothing")
    assert source.open() is False
    source.close()


def test_failed_open_leaves_no_capture_behind():
    """A half-open capture would be read from on the next tick."""
    source = OpenCVVideoSource(FRONT_PIPELINE)
    source.open()
    assert source._cap is None
    assert source.read_frame() is None


# --- The actual regression ------------------------------------------------


def test_concurrent_opens_do_not_crash():
    """Both cameras opening at once, with the rover down -- the segfault case.

    Reproduces the original failure: main and PiP workers each call open() on
    an unreachable GStreamer pipeline simultaneously. Before the fix this took
    the whole process down; now both must simply return False.
    """
    results = []
    errors = []

    def run(pipeline):
        try:
            results.append(OpenCVVideoSource(pipeline).open())
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(FRONT_PIPELINE,)),
        threading.Thread(target=run, args=(BACK_PIPELINE,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not any(t.is_alive() for t in threads), "open() hung"
    assert errors == []
    assert results == [False, False]


def test_capture_open_is_serialised():
    """Only one cv2.VideoCapture may be constructed at a time."""
    from mission_control.video import source as source_module

    assert isinstance(source_module._CAPTURE_OPEN_LOCK, type(threading.Lock()))


# --- Test pattern is unaffected -------------------------------------------


def test_test_pattern_still_opens():
    """The synthetic fallback must keep working with no rover present."""
    source = TestPatternVideoSource(width=320, height=180, target_fps=30)
    assert source.open() is True
    frame, meta = source.read_frame()
    assert frame.shape == (180, 320, 3)
    source.close()
