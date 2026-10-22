"""Background Video Ingestion and Decoding Worker Thread."""

import time
from typing import Optional
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from mission_control.core.logger import get_logger
from mission_control.video.frame_buffer import BoundedFrameBuffer, FrameMetadata
from mission_control.video.source import BaseVideoSource
from mission_control.video.stats import StreamState, StreamStats, StreamStatisticsTracker

logger = get_logger("video_worker")


class VideoWorker(QThread):
    """Dedicated background worker thread for non-blocking video ingestion,
    decoding, frame buffer management, and automatic stream recovery.
    """

    frame_ready = Signal(QImage, object)       # (QImage frame, FrameMetadata)
    stats_updated = Signal(object)              # (StreamStats snapshot)
    state_changed = Signal(object)              # (StreamState enum)
    error_occurred = Signal(str)                # (error_message)

    def __init__(
        self,
        video_source: BaseVideoSource,
        buffer_capacity: int = 2,
        parent=None,
    ):
        super().__init__(parent)
        self.video_source = video_source
        self.frame_buffer = BoundedFrameBuffer(max_capacity=buffer_capacity)
        self.stats_tracker = StreamStatisticsTracker()

        self._is_running = False
        self._reconnect_backoff_base = 1.0
        self._max_backoff = 8.0
        self._stats_emit_interval = 0.2  # 5 Hz stats updates
        self._last_stats_emit_time = 0.0

    @property
    def is_streaming(self) -> bool:
        return self._is_running

    def start_stream(self) -> None:
        """Starts the background video decoding thread."""
        if not self.isRunning():
            self._is_running = True
            self.start()
            logger.info("VideoWorker thread started.")

    def stop_stream(self) -> None:
        """Stops the thread gracefully and closes the video source."""
        logger.info("Stopping VideoWorker thread...")
        self._is_running = False
        self.requestInterruption()
        self.wait(2000)
        
        try:
            self.video_source.close()
        except Exception as e:
            logger.warning(f"Error closing video source: {e}")

        self.stats_tracker.set_state(StreamState.DISCONNECTED)
        self.state_changed.emit(StreamState.DISCONNECTED)
        logger.info("VideoWorker thread stopped cleanly.")

    def run(self) -> None:
        """Main execution loop for frame capture and decoding."""
        backoff = self._reconnect_backoff_base

        while self._is_running and not self.isInterruptionRequested():
            # 1. Connecting Phase
            self.stats_tracker.set_state(StreamState.CONNECTING)
            self.state_changed.emit(StreamState.CONNECTING)
            logger.info("VideoWorker: Attempting to open video source...")

            if not self.video_source.open():
                error_msg = f"Failed to connect to video source. Retrying in {backoff:.1f}s..."
                logger.warning(f"VideoWorker: {error_msg}")
                self.stats_tracker.set_state(StreamState.ERROR, error_msg)
                self.stats_tracker.record_reconnect_attempt()
                self.state_changed.emit(StreamState.ERROR)
                self.error_occurred.emit(error_msg)

                # Sleep with monotonic interruption check
                sleep_start = time.monotonic()
                while time.monotonic() - sleep_start < backoff:
                    if not self._is_running or self.isInterruptionRequested():
                        return
                    time.sleep(0.1)

                backoff = min(backoff * 1.5, self._max_backoff)
                continue

            # Connected successfully
            backoff = self._reconnect_backoff_base
            self.stats_tracker.set_state(StreamState.LIVE)
            self.state_changed.emit(StreamState.LIVE)
            logger.info("VideoWorker: Stream connected and LIVE.")

            consecutive_failures = 0
            self._last_stats_emit_time = time.monotonic()

            # 2. Continuous Ingestion Loop
            while self._is_running and not self.isInterruptionRequested():
                try:
                    result = self.video_source.read_frame()
                    if result is None:
                        consecutive_failures += 1
                        if consecutive_failures > 15:  # ~0.5s of consecutive missing frames
                            logger.warning("VideoWorker: Stream signal lost or timeout.")
                            self.stats_tracker.set_state(StreamState.NO_SIGNAL)
                            self.state_changed.emit(StreamState.NO_SIGNAL)
                            break
                        time.sleep(0.01)
                        continue

                    consecutive_failures = 0
                    bgr_frame, metadata = result

                    # Convert BGR NumPy array to QImage safely
                    height, width, channels = bgr_frame.shape
                    bytes_per_line = channels * width
                    
                    # Create deep copy of image bytes into QImage
                    qimage = QImage(
                        bgr_frame.data,
                        width,
                        height,
                        bytes_per_line,
                        QImage.Format.Format_BGR888,
                    ).copy()

                    # Push to Bounded Frame Buffer (stale frame dropping)
                    was_dropped = self.frame_buffer.push(qimage, metadata)
                    if was_dropped:
                        self.stats_tracker.record_frame_dropped()

                    # Record statistics
                    byte_size = bgr_frame.nbytes
                    self.stats_tracker.record_frame_received(
                        width=width,
                        height=height,
                        byte_size=byte_size,
                        latency_ms=metadata.estimated_latency_ms,
                        is_simulated_latency=(metadata.source_type == "test_pattern"),
                    )

                    # Emit frame to UI
                    self.stats_tracker.record_frame_displayed()
                    self.frame_ready.emit(qimage, metadata)

                    # Periodic stats emission using monotonic clock
                    now_mono = time.monotonic()
                    if now_mono - self._last_stats_emit_time >= self._stats_emit_interval:
                        self._last_stats_emit_time = now_mono
                        stats = self.stats_tracker.get_stats()
                        self.stats_updated.emit(stats)

                except Exception as e:
                    logger.error(f"VideoWorker exception in frame decode loop: {e}")
                    self.error_occurred.emit(str(e))
                    break

            # Close source before reconnect attempt
            try:
                self.video_source.close()
            except Exception as e:
                logger.warning(f"Error closing video source on reconnect: {e}")

            if self._is_running and not self.isInterruptionRequested():
                self.stats_tracker.record_reconnect_attempt()
                time.sleep(1.0)
