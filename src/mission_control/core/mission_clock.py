"""Mission run countdown clock for the operator station.

The clock measures the competition run on the operator laptop; it is not rover
state and is deliberately not routed through StateManager. Remaining time is
derived from a monotonic deadline rather than accumulated ticks, so the display
cannot drift if a timer callback is late.
"""

import math
import time
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from mission_control.core.logger import get_logger

logger = get_logger("mission_clock")

#: Remaining-time thresholds used to raise the clock's severity styling.
CLOCK_WARNING_SECONDS = 300  # 5 minutes remaining
CLOCK_ERROR_SECONDS = 60     # 1 minute remaining


def format_mmss(total_seconds: float) -> str:
    """Formats a duration as MM:SS (minutes are not wrapped at 60)."""
    seconds = max(0, int(total_seconds))
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes:02d}:{remainder:02d}"


class MissionClock(QObject):
    """Counts a configured run duration down to zero."""

    tick = Signal(int)            # remaining whole seconds
    finished = Signal()           # emitted once when the run reaches zero
    running_changed = Signal(bool)

    def __init__(
        self,
        duration_seconds: int,
        tick_interval_ms: int = 250,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._duration = max(0, int(duration_seconds))
        self._remaining = float(self._duration)
        self._deadline: Optional[float] = None
        self._is_finished = False
        self._last_emitted: Optional[int] = None

        self._timer = QTimer(self)
        self._timer.setInterval(max(10, tick_interval_ms))
        self._timer.timeout.connect(self._on_timeout)

    # -- State ---------------------------------------------------------------

    @property
    def duration_seconds(self) -> int:
        return self._duration

    @property
    def remaining_seconds(self) -> int:
        """Whole seconds left, rounded up so a fresh clock reads its full duration."""
        return int(math.ceil(self._current_remaining() - 1e-9))

    @property
    def is_running(self) -> bool:
        return self._timer.isActive()

    @property
    def is_finished(self) -> bool:
        return self._is_finished

    def formatted(self) -> str:
        return format_mmss(self.remaining_seconds)

    # -- Controls ------------------------------------------------------------

    def start(self) -> bool:
        """Starts or resumes the countdown. Returns False if already run out."""
        if self._is_finished or self.is_running:
            return False
        if self._remaining <= 0.0:
            return False
        self._deadline = time.monotonic() + self._remaining
        self._timer.start()
        self.running_changed.emit(True)
        logger.info(f"Mission clock started with {self.formatted()} remaining.")
        return True

    def pause(self) -> bool:
        """Pauses the countdown, keeping the remaining time."""
        if not self.is_running:
            return False
        self._remaining = self._current_remaining()
        self._deadline = None
        self._timer.stop()
        self.running_changed.emit(False)
        logger.info(f"Mission clock paused at {self.formatted()}.")
        return True

    def toggle(self) -> None:
        """Starts the clock if stopped, pauses it if running."""
        if self.is_running:
            self.pause()
        else:
            self.start()

    def reset(self) -> None:
        """Returns the clock to its full configured duration."""
        was_running = self.is_running
        self._timer.stop()
        self._remaining = float(self._duration)
        self._deadline = None
        self._is_finished = False
        self._last_emitted = None
        if was_running:
            self.running_changed.emit(False)
        self._emit_tick(self.remaining_seconds)
        logger.info(f"Mission clock reset to {self.formatted()}.")

    def set_remaining_seconds(self, seconds: float) -> None:
        """Jumps the clock to a specific remaining time.

        Provided for development and for tests that need to reach the end of a
        30-minute run without waiting for it.
        """
        seconds = max(0.0, float(seconds))
        self._remaining = seconds
        if self.is_running:
            self._deadline = time.monotonic() + seconds
        if seconds <= 0.0:
            self._complete()
        else:
            self._is_finished = False
            self._emit_tick(self.remaining_seconds)

    # -- Internals -----------------------------------------------------------

    def _current_remaining(self) -> float:
        if self._deadline is not None:
            return max(0.0, self._deadline - time.monotonic())
        return max(0.0, self._remaining)

    def _on_timeout(self) -> None:
        remaining = self._current_remaining()
        if remaining <= 0.0:
            self._complete()
            return
        self._remaining = remaining
        self._emit_tick(self.remaining_seconds)

    def _emit_tick(self, whole_seconds: int) -> None:
        """Emits a tick only when the displayed second actually changes."""
        if whole_seconds != self._last_emitted:
            self._last_emitted = whole_seconds
            self.tick.emit(whole_seconds)

    def _complete(self) -> None:
        already_finished = self._is_finished
        was_running = self.is_running
        self._remaining = 0.0
        self._deadline = None
        self._is_finished = True
        self._timer.stop()
        if was_running:
            self.running_changed.emit(False)
        self._emit_tick(0)
        if not already_finished:
            logger.info("Mission clock reached zero — MISSION COMPLETE.")
            self.finished.emit()
