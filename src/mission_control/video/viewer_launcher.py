"""Operator-side video viewer processes, started by START STREAM.

These are **receiver** pipelines that run on the Mission Control laptop and
connect out to the rover — not on the Jetson. Each opens its own GStreamer
window (``fpsdisplaysink`` / ``autovideosink``), so this is separate from the
in-UI 16:9 player, which pulls its own stream through OpenCV.

The pipelines live in plain text files (``~/frontcamera.txt`` and
``~/backcamera.txt``) and are **re-read on every press**, so an operator can
tune a pipeline between attempts without restarting Mission Control.

Processes are started in their own session so the whole GStreamer process
tree can be killed with one signal — ``gst-launch-1.0`` spawns children, and
signalling only the parent leaves them running and holding the camera.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
from typing import List, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from mission_control.core.config import VideoLaunchConfig, VideoViewer
from mission_control.core.logger import get_logger

logger = get_logger("viewer_launcher")

#: Grace period before escalating from SIGTERM to SIGKILL.
STOP_GRACE_S = 3.0


class ViewerLauncher(QObject):
    """Starts and stops the operator-side viewer pipelines."""

    #: (viewer name, started, detail)
    viewer_started = Signal(str, bool, str)
    #: (viewer name, reason)
    viewer_exited = Signal(str, str)

    def __init__(self, config: Optional[VideoLaunchConfig] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.config = config or VideoLaunchConfig()
        self._processes: List[Tuple[str, subprocess.Popen]] = []

    @property
    def is_running(self) -> bool:
        return bool(self._live_processes())

    @property
    def running_names(self) -> List[str]:
        return [name for name, _proc in self._live_processes()]

    def _live_processes(self) -> List[Tuple[str, subprocess.Popen]]:
        """Prunes anything that has exited and returns what is still alive."""
        alive = []
        for name, proc in self._processes:
            if proc.poll() is None:
                alive.append((name, proc))
            else:
                logger.info(f"Viewer '{name}' exited (code {proc.returncode}).")
                self.viewer_exited.emit(name, f"exited with code {proc.returncode}")
        self._processes = alive
        return alive

    # -- Control -------------------------------------------------------------

    def start(self) -> int:
        """Launches every configured viewer. Returns how many started."""
        if not self.config.enabled:
            logger.info("Viewer launch disabled in config; not starting anything.")
            return 0
        if self.is_running:
            logger.info("Viewers already running; not starting a second set.")
            return 0

        started = 0
        for viewer in self.config.viewers:
            if self._start_one(viewer):
                started += 1
        return started

    def start_one(self, name: str) -> bool:
        """Starts just the viewer called ``name``.

        Per-viewer rather than all-or-nothing, because each camera has its own
        button: starting the main stream must not also open the back camera's
        window, and restarting one must not disturb the other.
        """
        if not self.config.enabled:
            return False
        if name in self.running_names:
            return True   # already up; pressing again is a no-op, not a second window
        for viewer in self.config.viewers:
            if viewer.name == name:
                return self._start_one(viewer)
        logger.warning(f"No viewer named '{name}' in config.")
        return False

    def stop_one(self, name: str) -> None:
        """Stops just the viewer called ``name``, leaving the others running."""
        keep, stop = [], []
        for entry in self._processes:
            (stop if entry[0] == name else keep).append(entry)
        self._processes = keep
        for viewer_name, proc in stop:
            self._terminate(viewer_name, proc)

    def _start_one(self, viewer: VideoViewer) -> bool:
        command = viewer.read_command()
        if not command:
            detail = f"no command in {viewer.resolved_path}"
            logger.warning(f"Viewer '{viewer.name}': {detail}")
            self.viewer_started.emit(viewer.name, False, detail)
            return False

        try:
            # shlex rather than shell=True: these pipelines contain '!' and
            # other characters a shell would try to interpret.
            argv = shlex.split(command)
        except ValueError as exc:
            detail = f"could not parse pipeline: {exc}"
            logger.error(f"Viewer '{viewer.name}': {detail}")
            self.viewer_started.emit(viewer.name, False, detail)
            return False

        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Own session, so stopping kills the whole gst process tree
                # rather than orphaning children that keep the camera open.
                start_new_session=True,
            )
        except (OSError, FileNotFoundError) as exc:
            detail = f"could not start: {exc}"
            logger.error(f"Viewer '{viewer.name}': {detail}")
            self.viewer_started.emit(viewer.name, False, detail)
            return False

        self._processes.append((viewer.name, proc))
        logger.info(f"Viewer '{viewer.name}' started (pid {proc.pid}): {argv[0]} ...")
        self.viewer_started.emit(viewer.name, True, f"pid {proc.pid}")
        return True

    def stop(self) -> None:
        """Stops every viewer, escalating to SIGKILL if needed."""
        processes, self._processes = self._processes, []
        for name, proc in processes:
            self._terminate(name, proc)

    def _terminate(self, name: str, proc: subprocess.Popen) -> None:
        """Signals the viewer's whole process group, then escalates.

        The group, not the process: gst-launch spawns children, and killing
        only the parent leaves them running and holding the camera open.
        """
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=STOP_GRACE_S)
            logger.info(f"Viewer '{name}' stopped.")
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=1.0)
                logger.warning(f"Viewer '{name}' force killed.")
            except Exception as exc:
                logger.error(f"Viewer '{name}' would not die: {exc}")
        except (ProcessLookupError, PermissionError) as exc:
            logger.debug(f"Viewer '{name}' already gone: {exc}")
