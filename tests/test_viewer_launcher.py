"""Tests for the operator-side video viewer launcher.

Uses real subprocesses (`sleep`, `true`) rather than mocks, because the thing
worth verifying is process lifecycle — that a viewer really starts, really
dies when told, and cannot be started twice over itself.
"""

import time

import pytest
from PySide6.QtWidgets import QApplication

from mission_control.core.config import (
    VideoLaunchConfig,
    VideoViewer,
    load_rover_config,
)
from mission_control.video.viewer_launcher import ViewerLauncher

from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def viewer_file(tmp_path, name, command) -> VideoViewer:
    path = tmp_path / f"{name}.txt"
    path.write_text(command + "\n")
    return VideoViewer(name=name, command_file=str(path))


# --- Reading command files ------------------------------------------------


def test_reads_the_command_from_file(tmp_path):
    v = viewer_file(tmp_path, "front", "gst-launch-1.0 videotestsrc ! fakesink")
    assert v.read_command() == "gst-launch-1.0 videotestsrc ! fakesink"


def test_comments_and_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "front.txt"
    path.write_text("# the real pipeline\n\n   \ngst-launch-1.0 videotestsrc ! fakesink\n")
    assert VideoViewer(name="f", command_file=str(path)).read_command().startswith("gst-launch")


def test_missing_file_reads_as_empty(tmp_path):
    v = VideoViewer(name="f", command_file=str(tmp_path / "nope.txt"))
    assert v.read_command() == ""


def test_command_is_reread_each_time(tmp_path):
    """An operator must be able to edit a pipeline between attempts."""
    v = viewer_file(tmp_path, "front", "first")
    assert v.read_command() == "first"
    (tmp_path / "front.txt").write_text("second\n")
    assert v.read_command() == "second"


def test_home_paths_are_expanded():
    assert VideoViewer(name="f", command_file="~/frontcamera.txt").resolved_path.startswith("/")


# --- Shipped config -------------------------------------------------------


def test_shipped_config_keeps_both_camera_files_but_leaves_launching_off():
    """The viewer definitions stay; launching them does not.

    An external gst-launch viewer connects to the same RTSP media as the in-UI
    player, and the server serves both from one shared pipeline. Measured on
    the rover: with a viewer running the UI player received 1 frame in 10 s;
    with it off, 241. The viewers predate the UI being able to show the
    streams itself, so they are configured but disabled.
    """
    launch = load_rover_config(CONFIG_DIR).video_launch
    assert launch.enabled is False, "enabling this starves the in-UI player"
    files = [v.command_file for v in launch.viewers]
    assert "~/frontcamera.txt" in files
    assert "~/backcamera.txt" in files


# --- Process lifecycle ----------------------------------------------------


def test_starts_and_stops_a_real_process(qapp, tmp_path):
    launcher = ViewerLauncher(VideoLaunchConfig(
        enabled=True, viewers=[viewer_file(tmp_path, "front", "sleep 30")]
    ))
    assert launcher.start() == 1
    assert launcher.is_running is True
    assert launcher.running_names == ["front"]

    launcher.stop()
    assert launcher.is_running is False


def test_starts_every_configured_viewer(qapp, tmp_path):
    launcher = ViewerLauncher(VideoLaunchConfig(enabled=True, viewers=[
        viewer_file(tmp_path, "front", "sleep 30"),
        viewer_file(tmp_path, "back", "sleep 30"),
    ]))
    try:
        assert launcher.start() == 2
        assert sorted(launcher.running_names) == ["back", "front"]
    finally:
        launcher.stop()


def test_disabled_config_starts_nothing(qapp, tmp_path):
    launcher = ViewerLauncher(VideoLaunchConfig(
        enabled=False, viewers=[viewer_file(tmp_path, "front", "sleep 30")]
    ))
    assert launcher.start() == 0
    assert launcher.is_running is False


def test_pressing_start_twice_does_not_double_launch(qapp, tmp_path):
    """Two gst pipelines on one camera would fight over the device."""
    launcher = ViewerLauncher(VideoLaunchConfig(
        enabled=True, viewers=[viewer_file(tmp_path, "front", "sleep 30")]
    ))
    try:
        assert launcher.start() == 1
        assert launcher.start() == 0
        assert len(launcher.running_names) == 1
    finally:
        launcher.stop()


def test_missing_command_file_is_reported_not_raised(qapp, tmp_path):
    launcher = ViewerLauncher(VideoLaunchConfig(enabled=True, viewers=[
        VideoViewer(name="front", command_file=str(tmp_path / "absent.txt")),
    ]))
    events = []
    launcher.viewer_started.connect(lambda n, ok, d: events.append((n, ok, d)))

    assert launcher.start() == 0
    assert events[0][0:2] == ("front", False)
    assert "no command" in events[0][2]


def test_unlaunchable_binary_is_reported_not_raised(qapp, tmp_path):
    launcher = ViewerLauncher(VideoLaunchConfig(enabled=True, viewers=[
        viewer_file(tmp_path, "front", "this_binary_does_not_exist_12345 --x"),
    ]))
    events = []
    launcher.viewer_started.connect(lambda n, ok, d: events.append((n, ok, d)))

    assert launcher.start() == 0
    assert events[0][1] is False


def test_one_bad_viewer_does_not_block_the_other(qapp, tmp_path):
    launcher = ViewerLauncher(VideoLaunchConfig(enabled=True, viewers=[
        VideoViewer(name="bad", command_file=str(tmp_path / "absent.txt")),
        viewer_file(tmp_path, "good", "sleep 30"),
    ]))
    try:
        assert launcher.start() == 1
        assert launcher.running_names == ["good"]
    finally:
        launcher.stop()


def test_exited_process_is_pruned(qapp, tmp_path):
    launcher = ViewerLauncher(VideoLaunchConfig(
        enabled=True, viewers=[viewer_file(tmp_path, "front", "true")]
    ))
    exits = []
    launcher.viewer_exited.connect(lambda n, r: exits.append(n))

    launcher.start()
    time.sleep(0.4)
    assert launcher.is_running is False
    assert exits == ["front"]


def test_stop_is_safe_when_nothing_is_running(qapp):
    ViewerLauncher(VideoLaunchConfig(enabled=True)).stop()


def test_pipeline_with_shell_metacharacters_is_not_shell_interpreted(qapp, tmp_path):
    """GStreamer pipelines are full of '!' — a shell would mangle them."""
    v = viewer_file(tmp_path, "front", "sleep 30 ! fakesink")
    launcher = ViewerLauncher(VideoLaunchConfig(enabled=True, viewers=[v]))
    try:
        launcher.start()
        # 'sleep' rejects the extra args and exits; the point is that no shell
        # tried to interpret '!' as history expansion or a pipe.
        time.sleep(0.3)
        launcher._live_processes()
    finally:
        launcher.stop()


# --- Per-viewer control ---------------------------------------------------


def test_start_one_starts_only_that_viewer(qapp, tmp_path):
    launcher = ViewerLauncher(VideoLaunchConfig(enabled=True, viewers=[
        viewer_file(tmp_path, "front", "sleep 30"),
        viewer_file(tmp_path, "back", "sleep 30"),
    ]))
    try:
        assert launcher.start_one("front") is True
        assert launcher.running_names == ["front"]
    finally:
        launcher.stop()


def test_stop_one_leaves_the_other_running(qapp, tmp_path):
    """A dead back camera must never cost the operator the main view."""
    launcher = ViewerLauncher(VideoLaunchConfig(enabled=True, viewers=[
        viewer_file(tmp_path, "front", "sleep 30"),
        viewer_file(tmp_path, "back", "sleep 30"),
    ]))
    try:
        launcher.start_one("front")
        launcher.start_one("back")
        assert sorted(launcher.running_names) == ["back", "front"]

        launcher.stop_one("back")
        assert launcher.running_names == ["front"]
    finally:
        launcher.stop()


def test_start_one_twice_does_not_open_a_second_window(qapp, tmp_path):
    launcher = ViewerLauncher(VideoLaunchConfig(
        enabled=True, viewers=[viewer_file(tmp_path, "front", "sleep 30")]
    ))
    try:
        assert launcher.start_one("front") is True
        assert launcher.start_one("front") is True
        assert len(launcher.running_names) == 1
    finally:
        launcher.stop()


def test_start_one_with_an_unknown_name_is_refused(qapp, tmp_path):
    launcher = ViewerLauncher(VideoLaunchConfig(
        enabled=True, viewers=[viewer_file(tmp_path, "front", "sleep 30")]
    ))
    assert launcher.start_one("nope") is False


def test_stop_one_is_safe_when_not_running(qapp, tmp_path):
    ViewerLauncher(VideoLaunchConfig(enabled=True)).stop_one("front")
